"""Option C proof-of-concept: submit a 311 request via browser automation.

Why a browser and not a plain HTTP POST like the read path? The guest
submission flow is a multi-step Incap311 SPA (Home -> category tile ->
"Request This Service" -> form) and the site carries reCAPTCHA. Driving the
real page is the robust way to satisfy whatever anti-bot exists and to avoid
brittle payload/token reverse-engineering — it's the Playwright fallback the
project always reserved, here promoted to the submit mechanism.

SAFETY: dry_run is the default and it STOPS before the final submit, so it
never creates a real work order. An actual submission requires BOTH
live=True AND env ALEX311_ALLOW_LIVE_SUBMIT=1 — a deliberate double gate,
because every real submit dispatches city staff.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from playwright.sync_api import sync_playwright

BASE = "https://alex311.alexandriava.gov/customer/s/"

# scenario -> the exact category tile text on the portal home
SCENARIO_CATEGORY = {
    "missed": "Missed Collection",
    "bulk": "Bulk Yard Waste Pickup",
    "leaf": "Missed Leaf Collection",
}


@dataclass
class SubmitResult:
    ok: bool
    dry_run: bool
    stage: str                       # how far the flow got
    category: str
    steps: list = field(default_factory=list)   # per-wizard-step {heading, fields, action}
    recaptcha_seen: bool = False
    case_number: str | None = None   # only on a real (live) submit
    screenshot_path: str | None = None
    note: str = ""


def prepare_submission(
    *,
    scenario: str,
    description: str,
    address: str,
    dry_run: bool = True,
    live: bool = False,
    headless: bool = True,
    screenshot_path: str | None = None,
) -> SubmitResult:
    category = SCENARIO_CATEGORY.get(scenario)
    if not category:
        raise ValueError(f"unknown scenario {scenario!r}")

    # hard double-gate on real submission
    allow_env = os.environ.get("ALEX311_ALLOW_LIVE_SUBMIT") == "1"
    really_submit = live and not dry_run and allow_env
    if live and not dry_run and not allow_env:
        return SubmitResult(False, dry_run, "blocked", category,
                            note="live submit requested but ALEX311_ALLOW_LIVE_SUBMIT != 1")

    steps: list = []
    recaptcha_seen = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        pg = browser.new_page(viewport={"width": 1100, "height": 1500})
        try:
            pg.goto(BASE, wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(3500)

            # 1) pick the category tile
            tile = pg.locator("[data-service-code]", has_text=category).first
            tile.scroll_into_view_if_needed()
            tile.click(timeout=8000, force=True)
            pg.wait_for_timeout(2500)

            # 2) advance into the request wizard
            pg.get_by_role("button", name="Request This Service").first.click(timeout=8000)
            pg.wait_for_timeout(4500)

            # 3) walk the multi-step wizard. On each step: record structure,
            #    fill what we recognize, then either Continue, or (final step)
            #    Submit — but ONLY when really submitting.
            for _ in range(8):  # hard cap; the wizard is 5 steps
                pg.wait_for_timeout(1200)
                heading = _modal_heading(pg)
                fields = pg.eval_on_selector_all(
                    ".slds-modal input:not([type=hidden]), .slds-modal textarea, "
                    "[role=dialog] input:not([type=hidden]), [role=dialog] textarea",
                    """els => els.map(e => ({t:e.type||e.tagName, ph:e.placeholder||'',
                        aria:e.getAttribute('aria-label')||''}))""")
                _fill_first(pg, ["[role=dialog] textarea", ".slds-modal textarea"], description)
                _fill_first(pg, ['[role=dialog] input[placeholder*="ddress" i]',
                                 '[role=dialog] input[aria-label*="ddress" i]',
                                 '[role=dialog] input[placeholder*="ocation" i]'], address)
                recaptcha_seen = recaptcha_seen or pg.eval_on_selector_all(
                    "iframe[src*=recaptcha], .grecaptcha-badge", "e=>e.length") > 0

                cont = pg.get_by_role("button", name="Continue").first
                has_continue = cont.count() > 0
                cont_disabled = has_continue and cont.is_disabled()
                # A disabled Continue means a required selection is pending — on
                # the Esri "Location" step, drive the search/pick to enable it.
                if cont_disabled:
                    _handle_location(pg, address)
                    pg.wait_for_timeout(1500)
                    cont_disabled = cont.is_disabled()

                has_submit = pg.get_by_role("button", name="Submit").count() > 0
                action = "submit" if (has_submit and not has_continue) else "continue"
                steps.append({"heading": heading or "(step %d)" % (len(steps) + 1),
                              "fields": fields, "action": action, "blocked": cont_disabled})

                if cont_disabled:  # can't pass this gate automatically yet
                    if screenshot_path:
                        pg.screenshot(path=screenshot_path, full_page=True)
                    return SubmitResult(True, dry_run, "blocked_at_location", category, steps,
                                        recaptcha_seen, None, screenshot_path,
                                        "reached the Esri location step; automatic point "
                                        "selection needs selector tuning")

                if action == "submit":
                    if screenshot_path:
                        pg.screenshot(path=screenshot_path, full_page=True)
                    if really_submit:
                        pg.get_by_role("button", name="Submit").first.click(timeout=8000)
                        pg.wait_for_timeout(10000)
                        return SubmitResult(True, False, "submitted", category, steps,
                                            recaptcha_seen, _extract_case_number(pg),
                                            screenshot_path, "REAL submission created")
                    return SubmitResult(True, True, "reached_submit_step_dry_run", category,
                                        steps, recaptcha_seen, None, screenshot_path,
                                        "walked full wizard; stopped at Submit (dry run)")
                cont.click(timeout=8000)

            if screenshot_path:
                pg.screenshot(path=screenshot_path, full_page=True)
            return SubmitResult(False, dry_run, "wizard_not_completed", category, steps,
                                recaptcha_seen, None, screenshot_path,
                                "did not reach a Submit step within step cap")
        except Exception as e:
            shot = screenshot_path
            try:
                if shot:
                    pg.screenshot(path=shot, full_page=True)
            except Exception:
                pass
            return SubmitResult(False, dry_run, "error", category, steps,
                                recaptcha_seen, None, shot, f"{type(e).__name__}: {e}")
        finally:
            browser.close()


def _handle_location(pg, address: str) -> bool:
    """Best-effort: type the address into the Location step's Esri search box
    and click the first suggestion so Continue enables. Selectors here are the
    least stable part of the flow (third-party map widget) — tune with a real run."""
    try:
        box = pg.get_by_placeholder("Search").last
        box.fill(address, timeout=4000)
        pg.wait_for_timeout(2500)
        for sel in ["[role=option]", ".slds-listbox__option",
                    ".esri-search__suggestions-list li", "ul li[role=option]"]:
            opt = pg.locator(sel).first
            if opt.count() and opt.is_visible():
                opt.click(timeout=3000)
                pg.wait_for_timeout(2500)
                return True
        box.press("Enter")
        pg.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def _modal_heading(pg) -> str:
    for sel in ["[role=dialog] h1", "[role=dialog] h2", ".slds-modal__header",
                ".slds-modal h2"]:
        try:
            t = (pg.inner_text(sel, timeout=600)).strip()
            if t:
                return " ".join(t.split())[:80]
        except Exception:
            continue
    return ""


def _fill_first(pg, selectors, value):
    for sel in selectors:
        try:
            loc = pg.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.fill(value, timeout=3000)
                return True
        except Exception:
            continue
    return False


def _extract_case_number(pg) -> str | None:
    import re
    txt = pg.inner_text("body")
    m = re.search(r"\b\d{2}-\d{8}\b", txt)
    return m.group(0) if m else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import dataclasses

    p = argparse.ArgumentParser(
        prog="alex311.submit_browser",
        description="Option C PoC: drive the guest 311 submission flow. "
                    "Dry-run by default (never submits).")
    p.add_argument("--scenario", default="missed", choices=list(SCENARIO_CATEGORY))
    p.add_argument("--description", required=True)
    p.add_argument("--address", required=True)
    p.add_argument("--screenshot")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--live", action="store_true",
                   help="ACTUALLY submit (also needs ALEX311_ALLOW_LIVE_SUBMIT=1). "
                        "Creates a REAL city work order.")
    a = p.parse_args(argv)
    r = prepare_submission(
        scenario=a.scenario, description=a.description, address=a.address,
        dry_run=not a.live, live=a.live, headless=not a.headed,
        screenshot_path=a.screenshot,
    )
    print(f"stage={r.stage} ok={r.ok} recaptcha_seen={r.recaptcha_seen}")
    print(f"note: {r.note}")
    for i, s in enumerate(r.steps, 1):
        print(f"  step {i}: action={s['action']} blocked={s.get('blocked')}")
    if r.case_number:
        print(f"CASE NUMBER: {r.case_number}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
