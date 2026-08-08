"""Health check: prove the whole Salesforce-facing path still works.

Bootstraps a fresh session, pulls a list window, validates the record schema,
fetches one detail, validates that too, and asserts the guest-privacy
invariant. Records the outcome in ingest_runs (kind='health') and exits
non-zero on failure — wire a Cloud Scheduler job to this and alert on it.

What counts as failure is deliberately narrow: bootstrap/schema/privacy
breakage, or our stored data actually going stale. The portal rationing
queries (its "volume too large" guard, seen most weekends) is reported as
*degraded* and exits 0 — it self-heals, and it still proves our client path
works, so paging on it is noise.

Usage: python -m alex311.health [--skip-db]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import db, models
from .client import Alex311Client
from .ingest import STALE_AFTER

log = logging.getLogger("alex311.health")


def check_freshness(conn) -> None:
    """Fail when our data has actually gone stale.

    This is the signal worth waking someone for. Individual portal outages
    are not — they self-heal — but one that keeps us from ingesting for a
    day has stopped being transient.
    """
    last_ok = db.last_successful_ingest(conn)
    if last_ok is None:
        return  # nothing ingested yet (fresh install), not a health failure
    age = datetime.now(timezone.utc) - last_ok
    if age > STALE_AFTER:
        raise RuntimeError(
            f"no successful ingest in {age.total_seconds() / 3600:.1f}h "
            f"(threshold {STALE_AFTER}) — data is stale")


def check(client: Alex311Client, conn=None) -> str:
    """Raises on failure; returns a human summary on success."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    capped: list[tuple[datetime, datetime]] = []
    records = client.fetch_range(start, end, window=timedelta(days=2),
                                 on_capped=lambda s, e: capped.append((s, e)))
    if conn is not None:
        check_freshness(conn)

    if not records:
        if capped:
            # The portal *did* answer — with its volume guard. That still
            # exercises bootstrap, the Aura envelope and our parsing, which is
            # what this canary exists to verify. Freshness is checked above,
            # so a rationing weekend no longer pages anyone.
            return ("degraded: portal is rationing queries (volume cap); "
                    "client path verified and data freshness OK")
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
            summary = check(client, conn)
        log.log(logging.WARNING if summary.startswith("degraded") else logging.INFO,
                "health %s", summary)
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
