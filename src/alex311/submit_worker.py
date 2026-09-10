"""File approved requests from the queue, one at a time.

Why a queue rather than arguments: a browser must not live in the public web
image, and Cloud Run in this project rejects per-execution argument overrides,
so the job cannot be told on the command line what to file. The gated form
queues a prepared request, a human approves it, and this drains the approved
rows.

**Two keys, and both are per-request-shaped.** `ALEX311_ALLOW_LIVE_SUBMIT=1` is
the environment key, set on the live job and absent from the rehearsal one. A
row having `submit_state='approved'` with an `approved_by` is the request key,
set by a person looking at that specific request. Neither alone files anything.

Runs are deliberately small: one request per execution by default. Every filing
dispatches City staff, so there is no batch mode and there should not be.

    python -m alex311.submit_worker              # rehearse the next approved row
    ALEX311_ALLOW_LIVE_SUBMIT=1 python -m alex311.submit_worker --live
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from . import db
from .submit_browser import ENV_GATE, gates_open, prepare_submission

log = logging.getLogger("alex311.submit_worker")

CLAIMABLE = "approved"
MAX_TRIES = 3


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
    p.add_argument("--max", type=int, default=1,
                   help="how many to handle this run (default 1; every filing "
                        "dispatches city staff)")
    p.add_argument("--screenshot-dir")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    allowed, why = gates_open(a.live)
    log.info("submission worker starting: %s", why)
    if a.live and not allowed:
        log.error("refusing: %s", why)
        return 2

    conn = db.connect()
    handled = []
    for n in range(max(1, a.max)):
        row = claim_next(conn)
        if row is None:
            log.info("nothing approved and waiting")
            break

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
