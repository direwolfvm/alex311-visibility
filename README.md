# Alex311 Visibility

Unofficial data-visibility portal for Alexandria, VA's Alex311 service
requests. The city portal (Salesforce Experience Cloud) has no public API or
bulk export; this project ingests records via the site's own guest-accessible
Aura endpoint, stores them in Postgres (Cloud SQL), mirrors photos to GCS,
and serves a dashboard with filters, a map, category trends, and deep links
back to the official record.

## Layout

| Path | What |
|---|---|
| `src/alex311/client.py` | **All Salesforce/Aura quirks live here.** Guest bootstrap, fwuid auto-refresh, window slicing + adaptive volume splitting, pagination, dedupe, retries, media download. |
| `src/alex311/models.py` | Defensive extraction from raw JSON to flat columns + schema-drift validators. |
| `src/alex311/schema.sql` | Postgres DDL (idempotent): `service_requests`, `media`, `ingest_runs`. |
| `src/alex311/db.py` | Storage layer: upserts, enrichment/media queues, run bookkeeping. |
| `src/alex311/ingest.py` | CLI: `init-db`, `incremental`, `backfill`. |
| `src/alex311/health.py` | End-to-end canary; exits non-zero on breakage. |
| `src/alex311/media_store.py` | Local-dir (dev) or GCS (prod) photo storage. |
| `dashboard/` | FastAPI API + single-page frontend (Leaflet + Chart.js). |
| `spike/` | Phase 0 de-risk spike + findings (how guest auth actually works). |
| `deploy/README.md` | GCP runbook: Cloud Run jobs, Scheduler, alerting. |

## Local quickstart

```bash
docker run -d --name alex311-pg -e POSTGRES_PASSWORD=alex311 \
    -e POSTGRES_DB=alex311 -p 54329:5432 postgres:16
export DATABASE_URL=postgresql://postgres:alex311@localhost:54329/alex311
export MEDIA_DIR=./media_store

uv sync --group dev
uv run python -m alex311.ingest init-db
uv run python -m alex311.ingest incremental --days 7 --max-details 25 --max-media 25
uv run uvicorn dashboard.app:app --port 8311   # http://localhost:8311
uv run pytest                                   # unit tests, no network
```

## Operating principles

- **Good citizen.** Sequential requests, ≥250 ms spacing, exponential backoff,
  small date windows. The dashboard never touches the city portal.
- **Defensive.** Undocumented internal API; expect breakage on Salesforce
  redeploys. The client re-bootstraps itself on fwuid rotation; the health
  job alerts on anything it can't fix (see `deploy/README.md`).
- **Idempotent.** Everything upserts by `service_request_id`; re-running any
  ingest or backfill is always safe.
- **Privacy.** Guest sessions expose no reporter PII (`contact` is empty) and
  the health check fails hard if that ever changes. Don't republish photos
  without cause — some involve third parties.
