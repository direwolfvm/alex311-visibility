-- Alex311 visibility portal — Cloud SQL (Postgres) schema.
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS service_requests (
    service_request_id  TEXT PRIMARY KEY,          -- case number, e.g. 26-00013327
    sf_id               TEXT,
    status              TEXT,
    service_name        TEXT,
    service_code        TEXT,
    lat                 DOUBLE PRECISION,
    long                DOUBLE PRECISION,
    address             TEXT,
    zipcode             TEXT,
    requested_datetime      TIMESTAMPTZ,
    expected_datetime       TIMESTAMPTZ,
    updated_datetime        TIMESTAMPTZ,
    last_updated_datetime   TIMESTAMPTZ,
    closed_datetime         TIMESTAMPTZ,
    canceled_datetime       TIMESTAMPTZ,
    -- detail-only fields
    description         TEXT,
    origin              TEXT,
    source              TEXT,
    priority            TEXT,
    primary_service_department TEXT,
    agency_responsible  TEXT,
    status_notes        TEXT,
    closure_details     TEXT,
    parent_service_request_id TEXT,
    duplicate_parent_service_request_id TEXT,
    owner               TEXT,
    -- bookkeeping
    media_count         INTEGER NOT NULL DEFAULT 0,
    raw_list            JSONB,
    raw_detail          JSONB,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_ingested_at    TIMESTAMPTZ,
    enriched_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS sr_requested_idx ON service_requests (requested_datetime);
CREATE INDEX IF NOT EXISTS sr_service_name_idx ON service_requests (service_name);
CREATE INDEX IF NOT EXISTS sr_status_idx ON service_requests (status);
CREATE INDEX IF NOT EXISTS sr_needs_enrich_idx ON service_requests (enriched_at, last_updated_datetime);

CREATE TABLE IF NOT EXISTS media (
    media_id            TEXT PRIMARY KEY,           -- salesforce content id
    service_request_id  TEXT NOT NULL REFERENCES service_requests (service_request_id) ON DELETE CASCADE,
    file_name           TEXT,
    mime_type           TEXT,
    private             BOOLEAN NOT NULL DEFAULT FALSE,
    source_url          TEXT,
    created_datetime    TIMESTAMPTZ,
    stored_path         TEXT,          -- GCS object name or local relative path
    stored_bytes        BIGINT,
    stored_mime         TEXT,          -- actual stored content type (HEIC gets converted to JPEG)
    downloaded_at       TIMESTAMPTZ,
    download_error      TEXT
);

-- for databases created before stored_mime existed
ALTER TABLE media ADD COLUMN IF NOT EXISTS stored_mime TEXT;

CREATE INDEX IF NOT EXISTS media_sr_idx ON media (service_request_id);
CREATE INDEX IF NOT EXISTS media_pending_idx ON media (downloaded_at) WHERE downloaded_at IS NULL;

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- incremental | backfill | health
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    ok              BOOLEAN,
    range_start     TIMESTAMPTZ,
    range_end       TIMESTAMPTZ,
    records_seen    INTEGER,
    records_upserted INTEGER,
    details_fetched INTEGER,
    media_downloaded INTEGER,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS ingest_runs_started_idx ON ingest_runs (started_at DESC);
