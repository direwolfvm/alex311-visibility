"""The nightly walk: keep the registry true to the City's form.

The City's wizard is the only place its questions and rules are written
down, and it changes without notice. This walks it — a slice of request
types a night, the least recently walked first, so the whole catalog is
re-read about once a week — records what each walk saw (`wizard_walks`),
and offers a new registry (`alex311.registry_store`) when something moved.

What it does, per run:

  1. One catalog call. A request type the City has retired leaves the
     registry in use at once; nothing else is changed without a person.
  2. Walks up to `--limit` request types, oldest walk first. For every list
     question it tries each option in place and records the alert, whether
     it is a hard stop, and what it reveals. It NEVER presses Submit.
  3. Compares each walk with the one in use and stores the difference.
  4. Builds the registry those walks describe and, if it differs from the
     one in use, stores it as a proposal for the admin page.

Manners, because this is somebody else's server: one browser, one request
type at a time, a pause between them, a nightly cap, and it stops when the
portal starts refusing. A filing always goes first — the walk holds the
same floor lock as the filing worker (`submit_worker.FLOOR_LOCK`) only while
it has a page open, and waits while anything is queued.

A walk that suddenly sees *less* than the one before (no questions where
there were three) is more often a slow page than a changed form. It is
recorded as suspect, walked again first the next night, and believed only
when a second walk agrees.

Usage: python -m alex311.walk [--limit 24] [--minutes 45] [--codes A,B] [--no-propose]
Exits 1 when a new proposal is waiting or the walk had to stop early, so
the job's failure alert is the notification.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from psycopg.types.json import Jsonb

from . import db, registry_build, registry_store, rules_diff, wizard
from .wizard import (answered, choose, classify, dismiss_alerts, dropdown_checked,
                     dropdown_options, enabled, fill_free, is_selected, keep_current_visible,
                     msgs, open_category, press, scan, settle, suggestion_text)

log = logging.getLogger("alex311.walk")

WALK_LOCK = 311_2028            # one walk at a time; also how the filing worker knows a walk is on
PER_CATEGORY_SECONDS = 600
PAUSE_SECONDS = 2.5
QUEUE_WAIT_SECONDS = 600        # how long to stand aside for a queued filing before giving up the night
PERMANENT = ("view-only",)      # a request type the public cannot file is a finding, not a failure
MAX_CONSECUTIVE_ERRORS = 2      # the portal is refusing; stop rather than lean on it


async def probe_category(browser, name, code, groups):
    rec = {"service_name": name, "service_code": code, "questions": [], "error": None}

    async def fresh():
        return await browser.new_page(viewport={"width": 1100, "height": 1600})

    pg = await fresh()
    try:
        await open_category(pg, name, code, groups)
        rec["banners"] = await msgs(pg)
        try:
            pg = await _walk(pg, fresh, rec, name, code, groups)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"   # questions gathered so far are kept
        rec.setdefault("continue_enabled_at_end", None)
    finally:
        try:
            await pg.close()
        except Exception:
            pass
    return rec


async def _walk(pg, fresh, rec, name, code, groups):
    """Probe questions in order; returns the page in use at the end."""
    done_ks, path, restarts = set(), [], 0
    for _ in range(18):
        heads = await scan(pg)
        pend = [h for h in heads if h["k"] not in done_ks and not answered(h, classify(h))]
        if not pend:
            await dismiss_alerts(pg)
            cont = await enabled(pg, "Continue")
            if not cont and not rec["questions"]:
                # No questions at all: for some services the free-text box is
                # the required field. Fill it and see whether Continue enables.
                ta = pg.locator('textarea[aria-label="Additional Information"], textarea[placeholder="Additional Information"]').first
                if await ta.count():
                    await ta.fill("research dry run - not a real request")
                    await pg.wait_for_timeout(1200)
                    cont = await enabled(pg, "Continue")
                    rec["additional_information_required"] = bool(cont)
            last_had_hard = bool(rec["questions"]) and any(
                p.get("hard_stop") for p in rec["questions"][-1]["probes"].values())
            if not cont and last_had_hard and restarts < 3:
                # Some hard stops alter the wizard flow so the next question never
                # reveals. Rebuild from a FRESH page (the abandoned wizard's stored
                # state otherwise hides "Request This Service") and replay the path.
                restarts += 1
                try:
                    old, pg = pg, await fresh()
                    await old.close()
                    await open_category(pg, name, code, groups)
                    for kind, m, ch in path:
                        if kind == "list":
                            await choose(pg, m, ch)
                            await settle(pg)
                            await dismiss_alerts(pg)
                        else:
                            await fill_free(pg, m)
                        await press(pg, "Next", last=True)
                except Exception as e:
                    rec["note"] = f"restart {restarts} failed: {type(e).__name__}: {str(e)[:120]}"
                    rec["continue_enabled_at_end"] = False
                    break
                continue
            rec["continue_enabled_at_end"] = cont
            if not cont and wizard.SHOTS:
                try:
                    await pg.screenshot(path=str(wizard.SHOTS / f"rules_{code}_stuck.png"), full_page=True)
                except Exception:
                    pass
            break
        h = pend[0]
        meta = classify(h)
        if meta["kind"] == "dropdown":
            try:
                meta["options"] = await dropdown_options(pg, meta)
            except Exception as e:
                print(f"  dropdown enumeration failed: {e}", flush=True)
        done_ks.add(h["k"])
        entry = {"order": h["k"], "question": h["text"], "required": h["required"], "input": meta["kind"],
                 "code": meta.get("code") or "", "options": meta["options"], "probes": {}, "chosen": None}
        if meta["kind"] == "dropdown":                   # "Select all applicable" = multi-select picklist
            entry["multi"] = meta.get("trigger", "").lower().startswith("select all")
        if meta["kind"] in ("radio", "select", "checkbox", "radio-role", "dropdown") and meta["options"]:
            base_msgs = set(await msgs(pg))
            base_ks = {x["k"] for x in heads}
            for opt in meta["options"]:
                try:
                    await dismiss_alerts(pg)                 # leftovers from the previous probe
                    base_msgs = set(await msgs(pg))          # re-baseline so old alert text isn't "new"
                    await choose(pg, meta, opt)
                    await settle(pg)
                    await pg.wait_for_timeout(900)
                    new_msgs = [m for m in await msgs(pg) if m not in base_msgs]
                    had_ok = await enabled(pg, "OK")
                    suggests = await suggestion_text(pg) if await keep_current_visible(pg) else ""
                    if had_ok or suggests:
                        await dismiss_alerts(pg)
                    after = await scan(pg)
                    h2 = next((x for x in after if x["k"] == h["k"]), None)
                    still = await dropdown_checked(pg, opt) if meta["kind"] == "dropdown" else bool(h2 and is_selected(h2, meta, opt))
                    entry["probes"][opt] = {"alert": new_msgs, "hard_stop": bool(had_ok and not still),
                                            "advisory": bool(had_ok and still), "suggests": suggests,
                                            "reveals": sorted({x["k"] for x in after} - base_ks),
                                            "extra_controls": max(0, len(h2["controls"]) - len(h["controls"])) if h2 else 0}
                    if meta["kind"] in ("checkbox", "dropdown") and still:
                        await choose(pg, meta, opt)          # toggle back off to isolate probes
                        await settle(pg)
                except Exception as e:
                    entry["probes"][opt] = {"error": f"{type(e).__name__}: {str(e)[:140]}"}
            chosen = next((o for o in meta["options"] if not entry["probes"].get(o, {}).get("hard_stop")
                           and "error" not in entry["probes"].get(o, {})), None)
            if chosen:
                # after a hard-stop probe the widget can drop the next click; verify and retry
                for _attempt in range(3):
                    try:
                        await choose(pg, meta, chosen)
                        await settle(pg)
                        await dismiss_alerts(pg)
                        h3 = next((x for x in await scan(pg) if x["k"] == h["k"]), None)
                        if h3 and is_selected(h3, meta, chosen):
                            break
                    except Exception:
                        pass
                    await pg.wait_for_timeout(900)
            entry["chosen"] = chosen
            if chosen:
                path.append(("list", meta, chosen))
        else:
            await fill_free(pg, meta, h["text"])
            entry["chosen"] = "filled"
            path.append(("free", meta, None))
            entry["parts"] = [{"tag": t, "type": ty, "placeholder": ph, "aria": a} for t, ty, ph, a, _ in meta.get("parts", [])]
        await press(pg, "Next", last=True)
        rec["questions"].append(entry)
        if wizard.SHOTS:
            try:
                await pg.screenshot(path=str(wizard.SHOTS / f"rules_{code}_q{h['k']}.png"), full_page=True)
            except Exception:
                pass
    rec.setdefault("continue_enabled_at_end", await enabled(pg, "Continue"))
    rec["restarts"] = restarts
    return pg


def summarize(rec):
    qs = rec["questions"]
    hard = sum(1 for e in qs for p in e["probes"].values() if p.get("hard_stop"))
    adv = sum(1 for e in qs for p in e["probes"].values() if p.get("advisory"))
    branch = any(len({tuple(p.get("reveals", [])) for p in e["probes"].values() if "reveals" in p}) > 1 for e in qs)
    kinds = ",".join(e["input"] for e in qs)
    return f"{len(qs)} q [{kinds}] · {hard} hard-stop · {adv} advisory · branching={'yes' if branch else 'no'} · continue={rec.get('continue_enabled_at_end')}"


# ------------------------------------------------------------ what to believe

def ok(rec: dict) -> bool:
    """The walk ran to the end of the questions (or met a permanent answer)."""
    err = rec.get("error") or ""
    return (not err and rec.get("continue_enabled_at_end") is not None) or any(k in err for k in PERMANENT)


def degraded(prev: dict | None, rec: dict) -> bool:
    """Saw less than the walk before it: questions gone entirely, or Continue
    no longer reachable. Usually a slow page; sometimes the City."""
    if not prev:
        return False
    lost_questions = bool(prev.get("questions")) and not rec.get("questions")
    lost_continue = prev.get("continue_enabled_at_end") is True and rec.get("continue_enabled_at_end") is False
    return lost_questions or lost_continue


def shape(rec: dict) -> tuple:
    return (len(rec.get("questions") or []), rec.get("continue_enabled_at_end"))


# ------------------------------------------------------------------ the store

def bundled_rules() -> dict[str, dict]:
    """The walk shipped in the image, by service code — the baseline for a
    request type this job has not walked yet."""
    p = registry_store.data_file(registry_store.RULES_NAME)
    return {c["service_code"]: c for c in json.loads(p.read_text())["categories"]}


def latest_walks(conn) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT DISTINCT ON (service_code) service_code, walked_at, ok, suspect, result
             FROM wizard_walks ORDER BY service_code, walked_at DESC""").fetchall()
    return {r["service_code"]: r for r in rows}


def believed_walks(conn) -> dict[str, dict]:
    """Per request type, the newest walk that is believed: it ran, and it is
    not waiting for a second walk to confirm it."""
    rows = conn.execute(
        """SELECT DISTINCT ON (service_code) service_code, result
             FROM wizard_walks WHERE ok AND NOT suspect
            ORDER BY service_code, walked_at DESC""").fetchall()
    return {r["service_code"]: r["result"] for r in rows}


def rules_in_use(conn) -> dict[str, dict]:
    return bundled_rules() | believed_walks(conn)


def record(conn, rec: dict, *, in_use: dict | None, last: dict | None) -> dict:
    """Store one walk. `in_use` is the walk the registry is built from today,
    `last` the newest row for this request type, believed or not."""
    good = ok(rec)
    suspect = False
    if good and degraded(in_use, rec):
        # believed only when the walk before it lost the same things
        confirmed = bool(last and last["suspect"] and shape(last["result"]) == shape(rec))
        suspect = not confirmed
    changes = rules_diff.diff_one(in_use, rec) if good else []
    conn.execute(
        """INSERT INTO wizard_walks (service_code, service_name, ok, suspect, changes, result)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (rec["service_code"], rec["service_name"], good, suspect, Jsonb(changes), Jsonb(rec)))
    conn.commit()
    return {"ok": good, "suspect": suspect, "changes": changes}


# ------------------------------------------------------------- what to walk

def entry(t: dict) -> tuple[str, str, list[str]]:
    groups = [c["name"] for c in (t.get("definitions") or {}).get("service_categories", [])]
    return (t["service_name"], t["service_code"], groups)


def pick(catalog: list[dict], latest: dict[str, dict], limit: int,
         codes: list[str] | None = None) -> list[tuple[str, str, list[str]]]:
    """Tonight's slice: anything waiting for a second look, then never
    walked, then the oldest walk. `codes` overrides the order, not the cap."""
    by_code = {t["service_code"]: t for t in catalog}
    if codes:
        return [entry(by_code[c]) for c in codes if c in by_code][:limit]

    def rank(t):
        row = latest.get(t["service_code"])
        if row is None:
            return (1, 0.0)
        return (0 if row["suspect"] else 2, row["walked_at"].timestamp())
    return [entry(t) for t in sorted(catalog, key=rank)][:limit]


def candidate(conn, catalog: list[dict]) -> dict:
    """The registry the believed walks describe, for today's catalog."""
    mined = json.loads(registry_store.data_file(registry_store.MINED_NAME).read_text())
    rules = rules_in_use(conn)
    live = {t["service_code"] for t in catalog}
    return registry_build.build(catalog, mined, {"categories": [c for k, c in rules.items() if k in live]})


# ----------------------------------------------------------- standing aside

def filings_waiting(conn) -> int:
    from .submit_worker import CLAIMABLE, MAX_TRIES
    # Only what a worker would claim. One already being filed holds the floor,
    # which is what the lock below waits on.
    n = conn.execute("SELECT count(*) AS n FROM submission_attempts "
                     "WHERE submit_state = %s AND tries < %s", (CLAIMABLE, MAX_TRIES)).fetchone()["n"]
    conn.commit()
    return n


async def take_floor(conn) -> bool:
    """Hold the floor for one request type. A filing goes first: while one is
    queued or under way, wait — and give up the night if it does not clear."""
    from . import job_runner
    from .submit_worker import FLOOR_LOCK
    kicked = False
    deadline = time.time() + QUEUE_WAIT_SECONDS
    while True:
        if not filings_waiting(conn):
            got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (FLOOR_LOCK,)).fetchone()["ok"]
            conn.commit()
            if got:
                return True
        elif not kicked:
            log.info("a filing is waiting; standing aside (%s)", job_runner.kick())
            kicked = True
        if time.time() > deadline:
            return False
        await asyncio.sleep(10)


def leave_floor(conn) -> None:
    from .submit_worker import FLOOR_LOCK
    conn.execute("SELECT pg_advisory_unlock(%s)", (FLOOR_LOCK,))
    conn.commit()


# ------------------------------------------------------------------ the slice

async def walk_slice(conn, targets, *, minutes: float, probe=None, pause: float = PAUSE_SECONDS) -> dict:
    """Walk `targets` in order, recording each. Returns counts and the lines
    that changed. `probe(browser, name, code, groups)` is replaceable for tests."""
    out = {"planned": len(targets), "walked": 0, "failed": 0, "suspect": 0, "changed": 0,
           "changes": [], "stopped": None}
    if not targets:
        return out
    deadline = time.time() + minutes * 60
    in_use, latest = rules_in_use(conn), latest_walks(conn)
    browser = pw = None
    if probe is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        probe = probe_category
    errors_in_a_row = 0
    try:
        for i, (name, code, groups) in enumerate(targets, 1):
            if time.time() > deadline:
                out["stopped"] = "out of time; the rest are first in line tomorrow"
                break
            if not await take_floor(conn):
                out["stopped"] = "a filing was waiting; stood aside"
                break
            t0 = time.time()
            try:
                rec = await asyncio.wait_for(probe(browser, name, code, groups), timeout=PER_CATEGORY_SECONDS)
            except Exception as e:
                rec = {"service_name": name, "service_code": code, "questions": [],
                       "error": f"{type(e).__name__}: {str(e)[:200]}"}
            finally:
                leave_floor(conn)
            got = record(conn, rec, in_use=in_use.get(code), last=latest.get(code))
            out["walked"] += 1
            out["failed"] += not got["ok"]
            out["suspect"] += got["suspect"]
            if got["changes"] and not got["suspect"]:
                out["changed"] += 1
                out["changes"] += got["changes"]
            log.info("[%d/%d] %s (%s): %s%s%s  [%.0fs]", i, len(targets), name, code, summarize(rec),
                     f" · ERROR {rec['error']}" if rec.get("error") else "",
                     " · SUSPECT, will look again" if got["suspect"] else
                     (f" · {len(got['changes'])} change(s)" if got["changes"] else ""), time.time() - t0)
            errors_in_a_row = 0 if got["ok"] else errors_in_a_row + 1
            if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                out["stopped"] = f"{errors_in_a_row} walks in a row failed; not leaning on the portal"
                break
            await asyncio.sleep(pause)
    finally:
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()
    return out


def alert(result: dict, proposal: dict, site_origin: str) -> None:
    """Tell the operator a proposal is waiting. Best-effort."""
    to = os.environ.get("WALK_ALERT_EMAIL")
    if not to:
        return
    try:
        from . import mail
        if not mail.configured():
            return
        lines = proposal["changes"]
        text = (f"The nightly walk found {len(lines)} difference(s) between the City's form and the "
                f"registry this site is using.\n\n" + "\n".join(f"  - {c}" for c in lines[:60])
                + ("\n  ..." if len(lines) > 60 else "")
                + f"\n\nNothing has changed on the site. Review and adopt it at {site_origin}/submit/admin "
                  f"(Registry tab).\n\nProposed version {proposal['version']}; "
                  f"{result['walked']} request types walked tonight.")
        mail.send(to, f"Alex311 Reborn: the City's form changed ({len(lines)})", text)
    except Exception as e:
        log.warning("could not send the walk alert: %s", type(e).__name__)


def run(conn, *, limit: int, minutes: float, codes: list[str] | None = None, propose: bool = True,
        catalog: list[dict] | None = None, probe=None, pause: float = PAUSE_SECONDS) -> dict:
    """One night's work. Returns what happened; raises nothing the caller must handle."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (WALK_LOCK,)).fetchone()["ok"]:
        conn.commit()
        return {"skipped": "another walk is running"}
    conn.commit()
    try:
        if catalog is None:
            from .client import Alex311Client
            with Alex311Client() as client:
                catalog = client.get_service_types()
        catalog = [t for t in catalog if t.get("service_code") and t.get("service_name")]
        used = registry_store.current(conn)
        known = {s["service_code"] for s in used["registry"]["services"]}
        live = {t["service_code"] for t in catalog}
        if len(live) < 0.8 * len(known):
            # a short catalog is a bad answer, not a mass retirement
            raise RuntimeError(f"the catalog listed {len(live)} request types where {len(known)} are in use; "
                               "not believing it")
        result = {"catalog": len(live), "retired": sorted(known - live), "retired_version": None}
        if propose and result["retired"]:
            result["retired_version"] = registry_store.retire_services(conn, known - live)
        targets = pick(catalog, latest_walks(conn), limit, codes)
        result |= asyncio.run(walk_slice(conn, targets, minutes=minutes, probe=probe, pause=pause))
        if propose:
            result["proposal"] = registry_store.propose(
                conn, candidate(conn, catalog),
                note=f"Built from the nightly walk; {result['walked']} request types re-read in the last run.")
        return result
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (WALK_LOCK,))
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="alex311.walk",
                                description="Walk a slice of the City's request wizard, read-only, and "
                                            "offer a new registry when it has changed. Never submits.")
    p.add_argument("--limit", type=int, default=24, help="most request types to walk this run")
    p.add_argument("--minutes", type=float, default=45, help="stop starting new walks after this long")
    p.add_argument("--codes", help="walk these service codes (comma separated) instead of the oldest")
    p.add_argument("--no-propose", action="store_true", help="record walks only; touch no registry")
    p.add_argument("--screenshot-dir")
    a = p.parse_args(argv)
    if a.screenshot_dir:
        from pathlib import Path
        wizard.SHOTS = Path(a.screenshot_dir)

    conn = db.connect()
    run_id = db.start_run(conn, "walk", None, None)
    try:
        result = run(conn, limit=a.limit, minutes=a.minutes, propose=not a.no_propose,
                     codes=[c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None)
    except Exception as e:
        log.exception("the walk failed to run")
        conn.rollback()
        db.finish_run(conn, run_id, ok=False, error=f"{type(e).__name__}: {e}")
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "changes"}, indent=1, default=str))
    if result.get("skipped"):
        db.finish_run(conn, run_id, ok=True, error=result["skipped"])
        return 0
    proposal = result.get("proposal") or {}
    waiting = proposal.get("status") == "proposed"
    notes = [f"{result['walked']} of {result['planned']} walked"]
    if result["retired"]:
        notes.append("retired by the City and removed: " + ", ".join(result["retired"]))
    if result["stopped"]:
        notes.append("stopped: " + result["stopped"])
    if waiting:
        notes.append(f"proposal {proposal['version']} waiting with {len(proposal['changes'])} change(s)")
    bad = bool(result["stopped"] and "failed" in result["stopped"])
    db.finish_run(conn, run_id, ok=not bad, records_seen=result["walked"],
                  records_upserted=result["changed"], error="; ".join(notes) if (waiting or bad or result["stopped"]) else None)
    if waiting and proposal.get("new"):
        alert(result, proposal, os.environ.get("SITE_ORIGIN", "https://alex311-reborn.com"))
    for line in proposal.get("changes", []):
        log.warning("differs from the registry in use: %s", line)
    return 1 if (bad or (waiting and proposal.get("new"))) else 0


if __name__ == "__main__":
    sys.exit(main())
