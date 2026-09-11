"""Submit one 311 request through the City's own guest wizard.

This is the only place in the codebase that can create a real request. The
wizard driver in `alex311.wizard` navigates and fills but refuses to press
Submit; the single click that files a request lives here, behind two gates that
must both be open:

    live=True                       passed by the caller, per run
    ALEX311_ALLOW_LIVE_SUBMIT=1     set in the environment, deliberately

With either missing the run is a **dry run**: it drives the real wizard exactly
as a live run would, stops on the review step with the Submit button in view,
and reports what it would have sent. That is the rehearsal — the same code
path, one click short.

Answers come from `docs/data/form-registry.json`, so a run fills the questions
the City actually asks for that service and never offers an answer the wizard
rejects.

When DATABASE_URL is set, a live run is recorded before the click and its case
number written back after, because we are the sender of record for anything we
relay and "who filed what, when" has to be answerable. The anti-abuse policy is
consulted and reported at the same time, as advice rather than a veto: this CLI
is run by an operator on a request they have already decided to file, not by the
public, and the policy exists to govern the public path.

Playwright is imported at module scope. Nothing the web service imports may
import this: a real submission runs as its own job, on its own machine.

CLI:
    python -m alex311.submit_browser --service TESMISCO --address "100 King St" \\
        --description "..." --answers '{"01PL-...": "Recycling"}'          # dry run
    ALEX311_ALLOW_LIVE_SUBMIT=1 python -m alex311.submit_browser ... --live  # files it
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from . import wizard
from .wizard import BASE, answered, choose, classify, dismiss_alerts, enabled, fill_free, press, scan

log = logging.getLogger("alex311.submit_browser")

CASE_RE = re.compile(r"\b\d{2}-\d{8}\b")
# The City's search box carries "examples: pothole, trash, noise, 23-00000100"
# as its placeholder, and it is on every page. The first real filing was
# recorded under that number because the matcher read the whole page.
NOT_A_CASE = {"23-00000100"}


def case_number_in(*texts: str, seen_before: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The first case number in the texts given that was not on the page before
    Submit was pressed.

    The page behind the wizard lists other residents' recent requests, and the
    second real filing was recorded under one of theirs — a Tall Grass complaint
    on Wolfe Street — because a fallback read the whole page. A number the page
    already showed before the click cannot be ours, whatever text it is in.
    """
    for text in texts:
        for m in CASE_RE.finditer(text or ""):
            n = m.group(0)
            if n not in NOT_A_CASE and n not in seen_before:
                return n
    return None
ENV_GATE = "ALEX311_ALLOW_LIVE_SUBMIT"


@dataclass
class SubmitResult:
    ok: bool
    live: bool
    stage: str
    service_code: str
    service_name: str = ""
    answered: dict = field(default_factory=dict)
    unanswered: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    case_number: str | None = None
    attempt_id: int | None = None
    policy: dict = field(default_factory=dict)
    review_text: str = ""
    screenshot: str | None = None
    contact_required: bool | None = None
    # what the wizard showed after Submit, so a filing whose number we could not
    # read can still be verified by a person
    confirmation_text: str = ""
    note: str = ""

    @property
    def submitted(self) -> bool:
        return self.stage == "submitted"


def gates_open(live: bool) -> tuple[bool, str]:
    """Both gates, and why not when they are shut."""
    if not live:
        return False, "dry run (no --live)"
    if os.environ.get(ENV_GATE) != "1":
        return False, f"--live given but {ENV_GATE} is not 1"
    return True, "both gates open"


async def _fill_from_registry(pg, service: dict, answers: dict, description: str,
                              result: SubmitResult, *, invent: bool) -> None:
    """Answer the wizard's questions from `answers`, question by question.

    The wizard reveals questions progressively, so this rescans after each one
    rather than assuming the shape of the page.

    `invent` is the difference between a rehearsal and a filing. A dry run may
    make up a plausible date so the walk can continue and prove the path works.
    A live run may not: every value that goes to the City in someone's name has
    to have been supplied deliberately, so an unanswered question stops the run
    instead.
    """
    by_code = {q.get("code") or f"q{q['order']}": q for q in service["questions"]}
    done: set = set()

    for _ in range(18):
        heads = await scan(pg)
        pending = [h for h in heads if h["k"] not in done and not answered(h, classify(h))]
        if not pending:
            break
        head = pending[0]
        meta = classify(head)
        done.add(head["k"])

        # match the rendered question back to the registry by order, then code
        q = next((x for x in service["questions"] if x["order"] == head["k"]), None)
        key = (q.get("code") or f"q{head['k']}") if q else f"q{head['k']}"
        want = answers.get(key)
        if want is None and q:
            want = answers.get(str(q["order"]))

        if meta["kind"] in ("radio", "select", "checkbox", "radio-role", "dropdown") and meta["options"]:
            if want is None:
                result.unanswered.append({"order": head["k"], "question": head["text"],
                                          "options": meta["options"]})
                break
            wanted = want if isinstance(want, list) else [want]
            for value in wanted:
                if value not in meta["options"]:
                    result.unanswered.append({"order": head["k"], "question": head["text"],
                                              "rejected": value, "options": meta["options"]})
                    return
                await choose(pg, meta, value)
                await wizard.settle(pg)
                await dismiss_alerts(pg)
            result.answered[key] = want
        elif isinstance(want, dict):                         # date/time composite
            await fill_free(pg, meta, head["text"], values=want)
            result.answered[key] = want
        elif isinstance(want, str) and want:
            await fill_free(pg, meta, want, values=want)
            result.answered[key] = want
        elif meta["kind"] == "text" and description:
            # a free-text question with nothing specific asked for: the
            # description is what the resident wrote, so it belongs here
            await fill_free(pg, meta, description, values=description)
            result.answered[key] = description
        elif invent:
            # rehearsal: let the driver pick something shaped like the field
            await fill_free(pg, meta, head["text"])
            result.answered[key] = "(auto-filled for the rehearsal)"
        else:
            result.unanswered.append({"order": head["k"], "question": head["text"],
                                      "note": "needs an explicit answer for a live run"})
            return
        await press(pg, "Next", last=True)
        await pg.wait_for_timeout(700)


#: The wizard's own buttons live inside its modal. The page behind it has a
#: "Submit General Request" button of its own, and a bare name match finds that
#: one — a dry run reported "at Submit" while sitting on the contact step, and a
#: live run would have clicked the wrong thing entirely.
IN_MODAL = "[class*=modal], [role=dialog], .slds-modal"


def _within_modal(sel: str) -> str:
    """Scope a selector to inside the wizard modal.

    IN_MODAL is a selector *list*, so appending a descendant to the string
    attaches it to the last alternative only — the first version of this
    matched a hidden `#auraError` dialog instead of the consent tick.
    """
    return ", ".join(f"{part.strip()} {sel}" for part in IN_MODAL.split(","))

CONTACT_FIELDS = ("First Name", "Last Name", "Email", "Phone Number")

# The contact step says: "The only special character allowed in the contact
# name is a period (.)". A hyphenated surname reaches the review step as typed,
# and then Submit creates nothing — twice, for the first real request. Sending
# what the form says it accepts is the only way to find out, and the name as
# the person typed it stays on our record.
_NAME_OK = re.compile(r"[^A-Za-z0-9 .\u00C0-\u024F]")


def city_safe_name(name: str) -> str:
    """The name with anything the City's form says it will not take replaced
    by a space: Orrin-Brown becomes Orrin Brown, O'Neil becomes O Neil."""
    return " ".join(_NAME_OK.sub(" ", name or "").split())


async def contact_state(pg) -> dict:
    """Is there a contact step in front of us, and does this service demand it?

    The City marks it per service, not globally: on Missed Collection the four
    fields carry aria-labels ending "Required" and Continue is disabled, while
    on Fire Department Comments the same step is optional and Continue is live.
    Nothing in our registry records this, because the rule-discovery probe stops
    at the details step and never reaches contact.
    """
    labels = await pg.evaluate(
        """() => { const out=[]; const walk=n=>{ if(!n) return;
             if (['INPUT'].includes(n.tagName) && n.getClientRects().length)
               out.push({name: n.name || '', aria: n.getAttribute('aria-label') || ''});
             for (const c of n.children||[]) walk(c); if (n.shadowRoot) walk(n.shadowRoot); };
           walk(document.body); return out; }""")
    present = {f for f in CONTACT_FIELDS if any(x["name"] == f for x in labels)}
    required = {f for f in CONTACT_FIELDS
                if any(x["name"] == f and x["aria"].strip().endswith("Required") for x in labels)}
    return {"on_contact_step": len(present) >= 3,
            "required": bool(required), "fields": sorted(present)}


async def fill_contact(pg, contact: dict) -> list[str]:
    """Type the contact details the caller supplied. Returns the fields filled.

    Never invents a name, an email or a phone number. These are attached to a
    real request in a real person's name, so they come from the caller or the
    run stops.
    """
    filled = []
    # The consent tick comes first. On services where contact is optional the
    # four inputs arrive disabled and the box — "Providing your contact
    # information is optional ... please check the box to confirm your consent"
    # — is what enables them. Filling before ticking waits thirty seconds on a
    # disabled field and fails, which is how the first real request failed
    # three times over. On services where contact is required the fields are
    # live from the start and the tick is plain consent; ticking first is right
    # there too.
    box = pg.locator(_within_modal("[role=checkbox]")).locator("visible=true").first
    if await box.count() and (await box.get_attribute("aria-checked")) != "true":
        await box.click(force=True)
        await pg.wait_for_timeout(400)
        filled.append("consent")
    for field, value in (("First Name", city_safe_name(contact.get("first_name"))),
                         ("Last Name", city_safe_name(contact.get("last_name"))),
                         ("Email", contact.get("email")),
                         ("Phone Number", contact.get("phone"))):
        if not value:
            continue
        given = contact.get(field.split()[0].lower() + "_name") if "Name" in field else value
        if given and given != value:
            log.warning("%s sent as %r rather than %r: the City's form allows only a period "
                        "as a special character", field, value, given)
            filled.append(f"{field} as {value!r}")
        loc = pg.locator(f'input[name="{field}"]').first
        if await loc.count():
            # a short wait, so a field that is still disabled fails with a
            # message that says so rather than a thirty-second stall
            await loc.fill(str(value), timeout=8000)
            filled.append(field)
            await pg.wait_for_timeout(250)
    return filled


async def _run(*, service_code: str, address: str, description: str, answers: dict,
               contact: dict, lat: float | None, long: float | None, live: bool,
               headless: bool, screenshot: str | None) -> SubmitResult:
    from . import registry_drift as rd            # reuse the registry loader

    reg = rd.load_registry()
    service = next((s for s in reg["services"] if s["service_code"] == service_code), None)
    if service is None:
        return SubmitResult(False, live, "unknown_service", service_code,
                            note=f"{service_code} is not in the registry")

    allowed, why = gates_open(live)
    result = SubmitResult(True, allowed, "starting", service_code, service["service_name"],
                          note=why)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        pg = await browser.new_page(viewport={"width": 1100, "height": 1600})
        try:
            try:
                await wizard.open_category(pg, service["service_name"], service_code,
                                           service.get("groups") or [], address=address or None)
            except wizard.LocationRejected as e:
                result.stage = "address_not_serviceable"
                result.note = f"the City does not recognise that address: {e}"
                return result
            result.stage = "in_wizard"
            await _fill_from_registry(pg, service, answers, description, result,
                                      invent=not allowed)

            if result.unanswered:
                result.stage = "needs_answers"
                result.note = ("the wizard asked something this run had no answer for; "
                               "a live run will not invent one")
                return result

            # description box, then on to contact and review
            ta = pg.locator('textarea[aria-label="Additional Information"], '
                            'textarea[placeholder="Additional Information"]').first
            if await ta.count():
                await ta.fill(description)
                await pg.wait_for_timeout(600)

            for _ in range(5):                     # details -> contact -> review
                await dismiss_alerts(pg)
                state = await contact_state(pg)
                if state["on_contact_step"]:
                    result.contact_required = state["required"]
                    filled = await fill_contact(pg, contact)
                    if filled:
                        result.answered["contact"] = filled
                    await pg.wait_for_timeout(600)
                    if state["required"] and not await enabled(pg, "Continue"):
                        result.stage = "needs_contact"
                        result.note = (
                            "This service requires contact details (name, email, phone) "
                            "and the City will not accept the request without them. "
                            "Supply --first-name/--last-name/--email/--phone.")
                        return result
                if not await enabled(pg, "Continue"):
                    break
                await press(pg, "Continue")
                await pg.wait_for_timeout(2200)

            result.alerts = await wizard.msgs(pg)
            body = await pg.inner_text("body")
            result.review_text = " ".join(body.split())[:1500]

            submit = pg.locator(_within_modal("button")).locator("visible=true").filter(
                has_text=re.compile(r"^\s*Submit", re.I))
            if not await submit.count():
                result.stage = "no_submit_button"
                result.note = ("did not reach a review step with a Submit button "
                               "inside the wizard")
                return result

            result.stage = "at_submit"
            if allowed:
                _record(result, address=address, description=description, live=True)
            if screenshot:
                await pg.screenshot(path=screenshot, full_page=True)
                result.screenshot = screenshot

            if not allowed:
                result.stage = "ready_not_submitted"
                result.note = (f"{why} — stopped with Submit in view. "
                               "Nothing was sent to the City.")
                return result

            # Every case number already on the page — the search placeholder and
            # other residents' recent requests behind the wizard — so that none
            # of them can be mistaken for ours afterwards.
            before = frozenset(CASE_RE.findall(await pg.inner_text("body")))

            # ---- the only click in this project that files a request ----
            log.warning("SUBMITTING a real request to the City: %s at %s",
                        service["service_name"], address)
            await submit.first.click(timeout=10000)
            await pg.wait_for_timeout(12000)
            modal = pg.locator(IN_MODAL).locator("visible=true")
            inside = " ".join([await m.inner_text() for m in await modal.all()])
            after = await pg.inner_text("body")
            result.case_number = case_number_in(inside, after, seen_before=before)
            result.alerts = await wizard.msgs(pg)
            result.confirmation_text = " ".join((inside or after).split())[:900]
            log.warning("after Submit the wizard showed: %s | alerts: %s",
                        result.confirmation_text[:400], result.alerts)
            result.stage = "submitted"
            result.note = ("REAL submission created" if result.case_number
                           else "submit pressed but no case number was shown — verify manually")
            _close_out(result)
            if screenshot:
                await pg.screenshot(path=screenshot, full_page=True)
            return result
        except Exception as e:
            result.ok = False
            result.stage = result.stage or "error"
            result.note = f"{type(e).__name__}: {str(e)[:200]}"
            try:
                if screenshot:
                    await pg.screenshot(path=screenshot, full_page=True)
                    result.screenshot = screenshot
            except Exception:
                pass
            return result
        finally:
            await browser.close()


def _record(result: SubmitResult, *, address: str, description: str, live: bool) -> None:
    """Write the attempt down, and note what the abuse policy makes of it.

    Best-effort: a database that is unreachable must not stop an operator from
    filing a request they have decided to file, so this logs and moves on.
    """
    try:
        from . import abuse, db as adb
    except Exception:
        return
    if not os.environ.get("DATABASE_URL"):
        log.warning("DATABASE_URL is not set; this submission will not be recorded")
        return
    try:
        key = abuse.normalize_address(address)
        with adb.connect() as conn:
            history = []
            if key:
                for r in adb.abuse_history(conn, address_key=key,
                                           sql_prefix=abuse.sql_prefix(address),
                                           submitter_id=None):
                    if r["source"] == "city" and abuse.normalize_address(r["address"]) != key:
                        continue
                    history.append(abuse.Event(
                        at=r["at"], address=r["address"] or "", category=r["category"] or "",
                        submitter_id=r["submitter_id"], description=r["description"] or "",
                        closed_at=r["closed_at"], source=r["source"]))
            proposed = abuse.Event(at=datetime.now(timezone.utc), address=address,
                                   category=result.service_name, description=description,
                                   source="ours")
            decision = abuse.evaluate(proposed, history)
            result.policy = {"outcome": decision.outcome, "reasons": decision.reasons}
            if decision.outcome != abuse.ALLOW:
                log.warning("anti-abuse policy says %s: %s",
                            decision.outcome, "; ".join(decision.reasons))
            result.attempt_id = adb.record_attempt(
                conn, submitter_id=None, service_code=result.service_code,
                service_name=result.service_name, address=address, address_key=key,
                lat=None, long=None, description=description, answers=result.answered,
                outcome="relayed" if live else decision.outcome,
                findings=[{"rule": f.rule, "outcome": f.outcome, "message": f.message}
                          for f in decision.findings])
    except Exception as e:                       # never block a filing on bookkeeping
        log.warning("could not record this attempt: %s: %s", type(e).__name__, e)


def _close_out(result: SubmitResult) -> None:
    """Write the City's case number back onto the recorded attempt."""
    if not result.attempt_id or not os.environ.get("DATABASE_URL"):
        return
    try:
        from . import db as adb
        with adb.connect() as conn:
            conn.execute(
                "UPDATE submission_attempts SET relayed_at = now(), city_case_number = %s "
                "WHERE attempt_id = %s", (result.case_number, result.attempt_id))
            conn.commit()
    except Exception as e:
        log.warning("filed %s but could not record it: %s", result.case_number, e)


def prepare_submission(*, service_code: str, address: str = "", description: str = "",
                       answers: dict | None = None, contact: dict | None = None,
                       lat: float | None = None, long: float | None = None,
                       live: bool = False, headless: bool = True,
                       screenshot: str | None = None) -> SubmitResult:
    """Drive one request. Dry run unless both gates are open."""
    return asyncio.run(_run(service_code=service_code, address=address, description=description,
                            answers=answers or {}, contact=contact or {}, lat=lat, long=long,
                            live=live, headless=headless, screenshot=screenshot))


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="alex311.submit_browser",
        description="Drive the City's request wizard. Dry run unless --live AND "
                    f"{ENV_GATE}=1; a dry run stops with Submit in view and sends nothing.")
    p.add_argument("--service", required=True, help="service_code, e.g. TESMISCO")
    p.add_argument("--address", default="")
    p.add_argument("--description", default="")
    p.add_argument("--answers", default="{}",
                   help='JSON of question code -> answer, e.g. \'{"01PL-X": "Recycling"}\'')
    p.add_argument("--lat", type=float)
    p.add_argument("--long", type=float)
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--email")
    p.add_argument("--phone", help="some services require all four; the City rejects the "
                                   "request without them")
    p.add_argument("--screenshot")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--live", action="store_true",
                   help=f"actually file it. Also needs {ENV_GATE}=1. Creates a REAL city record.")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    allowed, why = gates_open(a.live)
    if a.live and not allowed:
        print(f"refusing: {why}")
        return 2
    if allowed:
        log.warning("live submission is ARMED — this run will create a real city record")

    r = prepare_submission(service_code=a.service, address=a.address,
                           description=a.description, answers=json.loads(a.answers),
                           contact={"first_name": a.first_name, "last_name": a.last_name,
                                    "email": a.email, "phone": a.phone},
                           lat=a.lat, long=a.long, live=a.live, headless=not a.headed,
                           screenshot=a.screenshot)

    if a.json:
        print(json.dumps(asdict(r), indent=1, default=str))
    else:
        print(f"stage={r.stage} ok={r.ok} live={r.live}")
        print(f"service: {r.service_name} ({r.service_code})")
        if r.answered:
            print("answered:")
            for k, v in r.answered.items():
                print(f"   {k}: {v}")
        for u in r.unanswered:
            print(f"NEEDS AN ANSWER: Q{u['order']} {u['question'][:70]!r}")
            if u.get("options"):
                print(f"   options: {u['options']}")
        if r.alerts:
            print("alerts on the review step:")
            for m in r.alerts[:6]:
                print(f"   {m[:120]}")
        if r.policy:
            print(f"anti-abuse policy: {r.policy['outcome']}")
            for why in r.policy.get("reasons", []):
                print(f"   {why}")
        if r.attempt_id:
            print(f"recorded as attempt {r.attempt_id}")
        if r.contact_required is not None:
            print(f"contact details required by this service: "
                  f"{'yes' if r.contact_required else 'no'}")
        if r.case_number:
            print(f"CASE NUMBER: {r.case_number}")
        print(f"note: {r.note}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
