"""File approved requests from the queue, one at a time.

Why a queue rather than arguments: a browser must not live in the public web
image, and Cloud Run in this project rejects per-execution argument overrides,
so the job cannot be told on the command line what to file. The form releases a
prepared request and this drains the released rows.

**One key, since the moderation step was removed.** `ALEX311_ALLOW_LIVE_SUBMIT=1`
is set on the live job and absent from the rehearsal one, and it is now the only
thing standing between a tester pressing a button and a real City record. There
used to be a second, per-request key held by an administrator; testers found
waiting for it worse than useless during a test, and it was removed
deliberately. Anyone changing this file should know that the environment
variable is the whole gate.

**One filing at a time, whoever asks.** The web service starts this job as soon
as a tester releases a request, so several executions can be alive at once. A
Postgres advisory lock means only one of them drives a browser against the
City's site; the rest see the floor is taken and exit. The holder drains the
queue and checks once more before letting go, so a request released during its
run is picked up rather than stranded.

    python -m alex311.submit_worker              # rehearse whatever is waiting
    ALEX311_ALLOW_LIVE_SUBMIT=1 python -m alex311.submit_worker --live
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from . import db
from .submit_browser import ENV_GATE, gates_open, prepare_submission

log = logging.getLogger("alex311.submit_worker")

CLAIMABLE = "approved"
MAX_TRIES = 3

# One arbitrary but fixed number, shared by every execution of this job. Two
# workers holding browsers against the City's form at the same time is the thing
# this prevents.
FLOOR_LOCK = 311_2026
# The gap between "nothing waiting" and letting go of the lock is exactly when a
# newly released request would be missed, so look again before leaving.
RECHECK_SECONDS = 4
# Between filings, so a burst of testers does not read as a burst on their site.
PAUSE_SECONDS = 5


def take_the_floor(conn) -> bool:
    """True if this execution may drive a browser. Never waits."""
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (FLOOR_LOCK,)).fetchone()
    conn.commit()
    return bool(got and got["ok"])


def leave_the_floor(conn) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (FLOOR_LOCK,))
    conn.commit()


def claim_next(conn) -> dict | None:
    """Take the oldest approved request, so two workers cannot file the same one.

    `FOR UPDATE SKIP LOCKED` is what makes a second execution pick a different
    row rather than block on this one, and the state flip to `filing` is what
    stops it being re-claimed after this transaction commits.
    """
    row = conn.execute(
        """SELECT * FROM submission_attempts
            WHERE submit_state = %s AND tries < %s
            ORDER BY approved_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1""",
        (CLAIMABLE, MAX_TRIES),
    ).fetchone()
    if row is None:
        conn.rollback()
        return None
    conn.execute(
        "UPDATE submission_attempts SET submit_state = 'filing', tries = tries + 1 "
        "WHERE attempt_id = %s", (row["attempt_id"],))
    conn.commit()
    return row


def finish(conn, attempt_id: int, *, state: str, case_number: str | None = None,
           error: str | None = None) -> None:
    conn.execute(
        """UPDATE submission_attempts
              SET submit_state = %s,
                  city_case_number = COALESCE(%s, city_case_number),
                  relayed_at = CASE WHEN %s = 'filed' THEN now() ELSE relayed_at END,
                  submit_error = %s
            WHERE attempt_id = %s""",
        (state, case_number, state, error, attempt_id))
    conn.commit()


def release(conn, attempt_id: int, error: str) -> None:
    """Put a row back for another try, or park it once it has had enough.

    A request that keeps failing is left `failed` rather than retried forever:
    the City's form changing is the likely cause, and hammering it helps nobody.
    """
    row = conn.execute("SELECT tries FROM submission_attempts WHERE attempt_id = %s",
                       (attempt_id,)).fetchone()
    state = "failed" if (row and row["tries"] >= MAX_TRIES) else CLAIMABLE
    finish(conn, attempt_id, state=state, error=error[:800])


def file_one(row: dict, *, live: bool, screenshot: str | None = None) -> dict:
    """Drive one queued request. Returns a summary for the log."""
    contact = row.get("contact") or {}
    answers = row.get("answers") or {}
    result = prepare_submission(
        service_code=row["service_code"],
        address=row.get("address") or "",
        description=row.get("description") or "",
        answers=answers if isinstance(answers, dict) else {},
        contact=contact if isinstance(contact, dict) else {},
        live=live, headless=True, screenshot=screenshot)
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="alex311.submit_worker",
        description="File approved requests from the queue. Rehearses unless "
                    f"--live AND {ENV_GATE}=1.")
    p.add_argument("--live", action="store_true",
                   help=f"actually file. Also needs {ENV_GATE}=1. Creates REAL city records.")
    p.add_argument("--max", type=int, default=10,
                   help="most requests to handle this run (default 10). The run "
                        "stops when the queue is empty, which is the usual case.")
    p.add_argument("--screenshot-dir")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    allowed, why = gates_open(a.live)
    log.info("submission worker starting: %s", why)
    if a.live and not allowed:
        log.error("refusing: %s", why)
        return 2

    conn = db.connect()
    if not take_the_floor(conn):
        # Another execution is already driving a browser. Leaving is correct:
        # the one holding the floor re-checks the queue before it lets go.
        log.info("another worker holds the floor; nothing to do")
        return 0

    handled = []
    rechecked = False
    for n in range(max(1, a.max)):
        row = claim_next(conn)
        if row is None:
            if rechecked:
                log.info("nothing released and waiting")
                break
            # the window in which a request released just now would be missed
            log.info("queue empty; looking again in %ss before letting go", RECHECK_SECONDS)
            time.sleep(RECHECK_SECONDS)
            rechecked = True
            continue
        rechecked = False
        if handled:
            time.sleep(PAUSE_SECONDS)

        attempt_id = row["attempt_id"]
        log.info("claimed attempt %s: %s at %s (approved by %s)",
                 attempt_id, row.get("service_name"), row.get("address"),
                 row.get("approved_by"))
        shot = (f"{a.screenshot_dir.rstrip('/')}/attempt-{attempt_id}.png"
                if a.screenshot_dir else None)
        try:
            result = file_one(row, live=allowed, screenshot=shot)
        except Exception as e:                       # a crash must not strand the row
            log.exception("attempt %s crashed", attempt_id)
            release(conn, attempt_id, f"{type(e).__name__}: {e}")
            handled.append({"attempt_id": attempt_id, "stage": "crashed"})
            continue

        if result.stage == "submitted":
            finish(conn, attempt_id, state="filed", case_number=result.case_number)
            log.warning("attempt %s FILED as %s", attempt_id, result.case_number)
        elif not allowed and result.stage == "ready_not_submitted":
            # a rehearsal proves the path; the row stays approved for the real run
            finish(conn, attempt_id, state=CLAIMABLE, error=None)
            conn.execute("UPDATE submission_attempts SET tries = tries - 1 "
                         "WHERE attempt_id = %s", (attempt_id,))
            conn.commit()
            log.info("attempt %s rehearsed to the review step; left approved", attempt_id)
        else:
            release(conn, attempt_id, f"{result.stage}: {result.note}")
            log.error("attempt %s not filed — %s: %s", attempt_id, result.stage, result.note)

        handled.append({"attempt_id": attempt_id, "stage": result.stage,
                        "case_number": result.case_number, "note": result.note})

    leave_the_floor(conn)

    if a.json:
        print(json.dumps({"live": allowed, "handled": handled}, indent=1, default=str))
    else:
        for h in handled:
            print(f"  attempt {h['attempt_id']}: {h['stage']}"
                  + (f" -> {h['case_number']}" if h.get("case_number") else ""))
        if not handled:
            print("  queue empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
