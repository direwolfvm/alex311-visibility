"""Refresh a few cases from the City on demand.

The mirror reads the City's list four times a day, so a request someone sent
minutes ago — or one that was just closed — can sit stale on their list for
hours. "Refresh status" on My requests asks the City about those few cases
right now, one at a time, and folds the answers into the mirror the same way
the ingest job would: the list-level columns, the detail columns, and any
media rows (the files themselves are fetched by the next ingest run).

Politeness is the constraint. This is the same public endpoint the ingest
job uses, on the same terms: sequential, a pause between calls, a small cap
per press, and a per-account cooldown enforced by the caller. A case the
City does not know yet (filed seconds ago, or a number that never existed)
comes back as `found: False` rather than an error.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from . import db, models
from .client import Alex311Client, Alex311Error

log = logging.getLogger("alex311.refresh")

MAX_PER_PRESS = 10          # cases per refresh; the newest first
PAUSE_SECONDS = 1.0         # between calls to the City


def refresh_cases(conn, client: Alex311Client, case_ids: list[str], *,
                  pause: float = PAUSE_SECONDS, limit: int = MAX_PER_PRESS,
                  stop_on_error: bool = False) -> list[dict]:
    """Ask the City about each case in turn and upsert what it says.

    Returns one result per case: {"service_request_id", "found", "status",
    "error"}. Never raises for one bad case; the rest still refresh — unless
    `stop_on_error`, which the scheduled pass uses: if the City is refusing
    or unreachable, asking again for the next case is the wrong response.
    """
    out = []
    for i, srid in enumerate(case_ids[:limit]):
        if stop_on_error and out and out[-1]["error"]:
            break
        if i and pause:
            time.sleep(pause)
        try:
            detail = client.get_detail(srid)
        except Alex311Error as e:
            # Two ways the City says "no such case (yet)": an empty detail, or
            # the Apex action refusing the number outright.
            unknown = "empty detail" in str(e) or "Invalid Service Request number" in str(e)
            out.append({"service_request_id": srid, "found": False, "status": None,
                        "error": None if unknown else str(e)[:120]})
            continue
        except Exception as e:                      # network, timeout, schema
            log.warning("refresh of %s failed: %s", srid, type(e).__name__)
            out.append({"service_request_id": srid, "found": False, "status": None,
                        "error": "could not reach the City just now"})
            continue
        if models.validate_list_record(detail) or models.validate_detail(detail):
            out.append({"service_request_id": srid, "found": False, "status": None,
                        "error": "the City's answer was not in the expected shape"})
            continue
        cols = models.list_record_columns(detail)
        db.upsert_list_records(conn, [(cols, detail)])
        db.apply_detail(conn, srid, models.detail_columns(detail), detail)
        db.upsert_media_rows(conn, models.media_rows(srid, detail))
        out.append({"service_request_id": srid, "found": True,
                    "status": cols.get("status"), "error": None})
    return out


# ---------------------------------------------------------------- the scheduled pass
# Every fifteen minutes: the cases people sent or follow, asked about at a
# rate that matches how likely each is to have changed, then the push pass.
# The six-hourly ingest still reads everything; this is only the handful a
# person is waiting on.

FRESH_HOURS = 48            # just filed from here: asked about every run
RECENT_DAYS = 14            # still young: asked about hourly
FRESH_EVERY_MIN = 12        # a little under the 15-minute schedule, so a run never skips one
RECENT_EVERY_MIN = 55
MAX_PER_RUN = 40
REFRESH_LOCK = 311_2027     # one pass at a time


def due_cases(conn, *, now: datetime | None = None, cap: int = MAX_PER_RUN) -> list[dict]:
    """Which linked cases to ask the City about now, most urgent first.

      fresh   sent from here, and filed (or linked) in the last 48 hours —
              including one the City has not listed yet
      recent  open, sent or followed, under two weeks old
      (older open cases wait for the ingest; closed ones are not asked about)
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        WITH linked AS (
            SELECT l.service_request_id,
                   bool_or(l.relation = 'mine') AS mine,
                   min(l.created_at) AS linked_at,
                   max(l.refreshed_at) AS refreshed_at
              FROM request_links l GROUP BY l.service_request_id
        ), c AS (
            SELECT k.service_request_id, k.mine,
                   r.service_request_id IS NOT NULL AS in_mirror,
                   COALESCE(r.requested_datetime, k.linked_at) AS born,
                   GREATEST(k.refreshed_at, r.last_ingested_at) AS checked,
                   (r.closed_datetime IS NOT NULL OR lower(COALESCE(r.status, '')) IN ('closed', 'canceled', 'cancelled')) AS done
              FROM linked k LEFT JOIN service_requests r USING (service_request_id)
        )
        SELECT service_request_id, in_mirror,
               CASE WHEN mine AND born > %(now)s - make_interval(hours => %(fresh_h)s) THEN 'fresh'
                    ELSE 'recent' END AS tier, checked
          FROM c
         WHERE NOT done
           AND born > %(now)s - make_interval(days => %(recent_d)s)
           AND (checked IS NULL OR checked < %(now)s - make_interval(mins =>
                  CASE WHEN mine AND born > %(now)s - make_interval(hours => %(fresh_h)s)
                       THEN %(fresh_m)s ELSE %(recent_m)s END))
         ORDER BY (mine AND born > %(now)s - make_interval(hours => %(fresh_h)s)) DESC,
                  checked ASC NULLS FIRST
         LIMIT %(cap)s""",
        {"now": now, "fresh_h": FRESH_HOURS, "recent_d": RECENT_DAYS,
         "fresh_m": FRESH_EVERY_MIN, "recent_m": RECENT_EVERY_MIN, "cap": cap}).fetchall()
    return rows


def run(conn, client: Alex311Client, *, now: datetime | None = None,
        site_origin: str = "https://alex311-reborn.com", notify=None) -> dict:
    """One scheduled pass. Returns counts for the log."""
    from . import push
    admin = "SELECT set_config('app.role', 'admin', false)"     # links are under forced RLS
    conn.execute(admin)
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (REFRESH_LOCK,)).fetchone()["ok"]:
        log.info("another refresh pass is running; nothing to do")
        return {"skipped": True}
    try:
        due = due_cases(conn, now=now)
        ids = [d["service_request_id"] for d in due]
        results = refresh_cases(conn, client, ids, limit=MAX_PER_RUN, stop_on_error=True) if ids else []
        asked = [r["service_request_id"] for r in results if not r["error"]]
        conn.execute(admin)
        if asked:
            conn.execute("UPDATE request_links SET refreshed_at = now() WHERE service_request_id = ANY(%s)",
                         (asked,))
            conn.commit()
            conn.execute(admin)
        counts = {"due": len(due), "asked": len(results),
                  "found": sum(1 for r in results if r["found"]),
                  "not_yet": sum(1 for r in results if not r["found"] and not r["error"]),
                  "errors": sum(1 for r in results if r["error"]),
                  "stopped_early": len(results) < len(ids)}
        # tell the people it concerns — after every pass, so a change found now is news now
        notify = notify or (push.notify if push.configured() else None)
        if notify:
            try:
                counts["push"] = notify(conn, site_origin=site_origin)
            except Exception as e:
                log.warning("push pass failed: %s", type(e).__name__)
                conn.rollback()
        log.info("refresh pass: %s", counts)
        return counts
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (REFRESH_LOCK,))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with db.connect() as conn, Alex311Client() as client:
        print(run(conn, client, site_origin=os.environ.get("SITE_ORIGIN", "https://alex311-reborn.com")))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
