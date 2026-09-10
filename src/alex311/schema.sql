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
    -- windows the portal refused to serve (volume rationing); the next run's
    -- lookback re-covers them, so a nonzero value is informational, not failure
    windows_incomplete INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- for databases created before windows_incomplete existed
ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS windows_incomplete INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ingest_runs_started_idx ON ingest_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- Gated submission layer (Option C). Nothing here reaches the City: these
-- tables record what our own layer was asked to do and what it decided, so
-- rate limits have a history to count and every decision is auditable. We are
-- the sender of record for anything we relay, so "who asked, when, and what we
-- did about it" has to be answerable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS submitters (
    submitter_id    TEXT PRIMARY KEY,              -- opaque; not the email
    email           TEXT UNIQUE,
    verified_at     TIMESTAMPTZ,                   -- NULL until the address is proven
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked_at      TIMESTAMPTZ,
    blocked_reason  TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS submission_attempts (
    attempt_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submitter_id    TEXT REFERENCES submitters (submitter_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    service_code    TEXT NOT NULL,
    service_name    TEXT,
    address         TEXT,
    address_key     TEXT,                          -- normalised; what caps count on
    lat             DOUBLE PRECISION,
    long            DOUBLE PRECISION,
    description     TEXT,
    answers         JSONB,
    -- what the policy said, and why
    outcome         TEXT NOT NULL,                 -- allow | notice | review | block
    findings        JSONB NOT NULL DEFAULT '[]'::jsonb,
    cooldown_until  TIMESTAMPTZ,
    -- the relay itself, still gated and unused
    relayed_at      TIMESTAMPTZ,
    city_case_number TEXT
);

CREATE INDEX IF NOT EXISTS sa_submitter_idx ON submission_attempts (submitter_id, created_at DESC);
CREATE INDEX IF NOT EXISTS sa_address_idx ON submission_attempts (address_key, created_at DESC);
CREATE INDEX IF NOT EXISTS sa_pending_idx ON submission_attempts (created_at DESC)
    WHERE outcome = 'review' AND relayed_at IS NULL;

-- Every human decision on a held submission. Append-only by convention: a
-- reversal is a new row, so the trail of who decided what survives.
CREATE TABLE IF NOT EXISTS moderation_actions (
    action_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id      BIGINT NOT NULL REFERENCES submission_attempts (attempt_id) ON DELETE CASCADE,
    acted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,                 -- who decided
    action          TEXT NOT NULL,                 -- approve | reject | block_submitter | note
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS ma_attempt_idx ON moderation_actions (attempt_id, acted_at);

-- One-time codes proving control of a mailbox. Only the salted hash is stored:
-- a six-digit code is guessable from a leaked table without one.
CREATE TABLE IF NOT EXISTS submitter_verifications (
    verification_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           TEXT NOT NULL,                 -- canonical form (+tags folded)
    code_hash       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    consumed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS sv_email_idx ON submitter_verifications (email, created_at DESC);

-- Sessions. The token itself only ever exists in the client; we keep its hash,
-- so the table cannot be used to impersonate anyone.
CREATE TABLE IF NOT EXISTS submitter_sessions (
    token_hash      TEXT PRIMARY KEY,
    submitter_id    TEXT NOT NULL REFERENCES submitters (submitter_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    user_agent      TEXT
);

CREATE INDEX IF NOT EXISTS ss_submitter_idx ON submitter_sessions (submitter_id, expires_at DESC);
