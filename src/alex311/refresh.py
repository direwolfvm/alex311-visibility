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
import time

from . import db, models
from .client import Alex311Client, Alex311Error

log = logging.getLogger("alex311.refresh")

MAX_PER_PRESS = 10          # cases per refresh; the newest first
PAUSE_SECONDS = 1.0         # between calls to the City


def refresh_cases(conn, client: Alex311Client, case_ids: list[str], *,
                  pause: float = PAUSE_SECONDS) -> list[dict]:
    """Ask the City about each case in turn and upsert what it says.

    Returns one result per case: {"service_request_id", "found", "status",
    "error"}. Never raises for one bad case; the rest still refresh.
    """
    out = []
    for i, srid in enumerate(case_ids[:MAX_PER_PRESS]):
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
