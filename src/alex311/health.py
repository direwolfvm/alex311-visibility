"""Health check: prove the whole Salesforce-facing path still works.

Bootstraps a fresh session, pulls a 1-day list window, validates the record
schema, fetches one detail, and validates that too. Records the outcome in
ingest_runs (kind='health') and exits non-zero on any failure — wire a Cloud
Scheduler job to this and alert on job failure.

Usage: python -m alex311.health [--skip-db]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import db, models
from .client import Alex311Client

log = logging.getLogger("alex311.health")


def check(client: Alex311Client) -> str:
    """Raises on failure; returns a human summary on success."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    records = client.fetch_range(start, end, window=timedelta(days=2))
    if not records:
        raise RuntimeError("health list window returned zero records "
                           "(2 days of a live 311 system should not be empty)")
    rec = next(iter(records.values()))
    drift = models.validate_list_record(rec)
    if drift:
        raise RuntimeError(f"list schema drift, missing keys: {drift}")

    srid = rec["service_request_id"]
    detail = client.get_detail(srid)
    drift = models.validate_detail(detail)
    if drift:
        raise RuntimeError(f"detail schema drift for {srid}, missing keys: {drift}")
    if detail.get("contact"):
        # Guests must never see reporter PII; if this appears, stop and review.
        raise RuntimeError("detail 'contact' is non-empty for a guest session — "
                           "privacy invariant violated, investigate before ingesting")
    return f"ok: {len(records)} records in 2-day window; detail {srid} valid"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="alex311.health")
    p.add_argument("--skip-db", action="store_true",
                   help="don't record the outcome in ingest_runs")
    args = p.parse_args(argv)

    conn = run_id = None
    if not args.skip_db:
        conn = db.connect()
        run_id = db.start_run(conn, "health", None, None)

    try:
        with Alex311Client() as client:
            summary = check(client)
        log.info("health %s", summary)
        if conn:
            db.finish_run(conn, run_id, ok=True)
        return 0
    except Exception as e:
        log.error("health FAILED: %s", e)
        if conn:
            db.finish_run(conn, run_id, ok=False, error=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
