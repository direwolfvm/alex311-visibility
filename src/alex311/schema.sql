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

-- The resident-identity tables (submitters, submitter_sessions,
-- submitter_verifications) that once lived here were retired with the account
-- system: Firebase Authentication proves control of a mailbox now, and holds
-- the address instead of us. `submission_attempts.submitter_id` is the portal
-- account's user_id.
DROP TABLE IF EXISTS submitter_verifications;
DROP TABLE IF EXISTS submitter_sessions;
ALTER TABLE IF EXISTS submission_attempts DROP CONSTRAINT IF EXISTS submission_attempts_submitter_id_fkey;
DROP TABLE IF EXISTS submitters;

CREATE TABLE IF NOT EXISTS submission_attempts (
    attempt_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submitter_id    TEXT,                          -- the portal account (user_id)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    service_code    TEXT NOT NULL,
    service_name    TEXT,
    address         TEXT,
    address_key     TEXT,                          -- normalized; what caps count on
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


-- The submission queue. A browser cannot live in the public web image, and
-- Cloud Run rejects per-execution argument overrides in this project, so the
-- job cannot be told what to file on the command line. It takes its work from
-- here instead: the gated form queues a prepared request, a human approves it,
-- and the job drains approved rows one at a time.
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS submit_state TEXT NOT NULL DEFAULT 'prepared';
--   prepared  evaluated by the policy, nothing more
--   queued    a resident asked for it to be filed
--   approved  a human said yes; this is the per-request half of the live gate
--   filing    a worker has claimed it
--   filed     the City accepted it; city_case_number holds their number
--   failed    the worker could not file it; submit_error says why
--   cancelled withdrawn before filing
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS queued_at    TIMESTAMPTZ;
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS approved_at  TIMESTAMPTZ;
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS approved_by  TEXT;
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS submit_error TEXT;
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS tries        INTEGER NOT NULL DEFAULT 0;
-- Contact details travel with the request because some services refuse it
-- without them. They are the resident's own, given for this purpose, and go
-- to the City exactly as typed.
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS contact      JSONB;
-- The City's own spelling of the address, when we could find one. `address` is
-- what the resident typed and is what we show them; this is what gets typed
-- into the City's gazetteer box, because that box only recognizes its own
-- wording. A tester's "1437 Janneys Lane" finds nothing there; the City's
-- "1437 JANNEY'S LN" finds it at once. Null means we had no match and the
-- resident's own wording is all we have.
ALTER TABLE submission_attempts ADD COLUMN IF NOT EXISTS city_address TEXT;

CREATE INDEX IF NOT EXISTS sa_state_idx ON submission_attempts (submit_state, approved_at)
    WHERE submit_state IN ('queued', 'approved', 'filing');

-- ---------------------------------------------------------------------------
-- Who may open the gated prototype at all. Separate from `submitters`, which
-- records which *resident* filed a request: this is the front door, and its job
-- is keeping out passers-by while the authorization question with the City is
-- open. Passwords are scrypt hashes with a per-user salt; the plaintext exists
-- only in the browser and in whatever the admin wrote down.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS portal_users (
    user_id         TEXT PRIMARY KEY,
    email           TEXT,                           -- only where a password login needs it
    password_hash   TEXT,
    salt            TEXT,
    role            TEXT NOT NULL DEFAULT 'user',   -- admin | user
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,
    last_login_at   TIMESTAMPTZ,
    disabled_at     TIMESTAMPTZ
);
-- A name the person chose to be shown to themselves — in the account chip
-- and on the account page. Optional, never shown on the public site: a score
-- someone chooses to show carries no name, and nothing here reaches the City.
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS display_name TEXT;
-- Two ways in, one account. The password columns serve the testers already
-- here; a Firebase sign-in carries only the uid, and for such an account we
-- hold no email at all — Firebase does. An existing tester who signs in with
-- Firebase using the same address is linked, not duplicated.
ALTER TABLE portal_users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE portal_users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE portal_users ALTER COLUMN salt DROP NOT NULL;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS firebase_uid   TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS policy_version TEXT;   -- the data policy accepted
CREATE UNIQUE INDEX IF NOT EXISTS pu_firebase_uid_idx ON portal_users (firebase_uid) WHERE firebase_uid IS NOT NULL;
-- the original UNIQUE on email allows many NULLs, which is what we want

CREATE TABLE IF NOT EXISTS portal_sessions (
    token_hash      TEXT PRIMARY KEY,               -- the token itself lives in the cookie
    user_id         TEXT NOT NULL REFERENCES portal_users (user_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    user_agent      TEXT
);

CREATE INDEX IF NOT EXISTS ps_user_idx ON portal_sessions (user_id, expires_at DESC);

-- ---------------------------------------------------------------------------
-- Accounts, phase 2: a pointer from an account to a request.
--
-- A row is a case number and nothing the City already holds. `mine` is written
-- by the worker at the one moment it can be vouched for — when the City hands
-- back a case number for a request this account sent. `following` is anyone
-- saying "I care about this", and needs no proof.
--
-- Row-level security is on, and FORCED so it binds the table's owner too. The
-- application sets app.user_id and app.role at the start of each transaction
-- (see db.as_user); a handler that forgets to filter by account then gets
-- nothing rather than everything. Operators reading this table by hand need
-- the same: SELECT set_config('app.role', 'admin', false);
CREATE TABLE IF NOT EXISTS request_links (
    user_id             TEXT NOT NULL REFERENCES portal_users (user_id) ON DELETE CASCADE,
    service_request_id  TEXT NOT NULL,              -- the City's case number
    relation            TEXT NOT NULL CHECK (relation IN ('mine', 'following')),
    attempt_id          BIGINT REFERENCES submission_attempts (attempt_id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, service_request_id)
);
CREATE INDEX IF NOT EXISTS rl_case_idx ON request_links (service_request_id);

ALTER TABLE request_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own   ON request_links;
DROP POLICY IF EXISTS admin ON request_links;
CREATE POLICY own   ON request_links USING (user_id = current_setting('app.user_id', true));
CREATE POLICY admin ON request_links USING (current_setting('app.role', true) = 'admin');

-- The web service connects as a role that owns nothing, so that RLS applies
-- to it without FORCE having to carry the whole weight. The role is created
-- by an operator (it needs a password); these grants take effect the next
-- time the schema is applied after it exists, and cover tables created later.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alex311_app') THEN
    GRANT USAGE ON SCHEMA public TO alex311_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO alex311_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO alex311_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO alex311_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO alex311_app;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Accounts, phase 3: the resident's own verdict on whether an issue was
-- addressed. One per account per request, editable. The score is a five-point
-- scale with NULL for "not sure"; the note is free text that is read by a
-- person and never shown publicly — there is no moderation here because
-- there is nothing public to moderate. `share_score` is stored now and
-- rendered by phase 4. `status_at_rating` is what the mirror said at the time,
-- because "still open, rated unresolved" and "closed, rated unresolved" are
-- different findings.
CREATE TABLE IF NOT EXISTS feedback (
    user_id             TEXT NOT NULL REFERENCES portal_users (user_id) ON DELETE CASCADE,
    service_request_id  TEXT NOT NULL,
    relation            TEXT NOT NULL,              -- mine | following, at the time
    score               SMALLINT CHECK (score BETWEEN 1 AND 5),
    note                TEXT,
    status_at_rating    TEXT,
    share_score         BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, service_request_id)
);
CREATE INDEX IF NOT EXISTS fb_case_idx ON feedback (service_request_id);

ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own       ON feedback;
DROP POLICY IF EXISTS admin     ON feedback;
DROP POLICY IF EXISTS analytics ON feedback;
CREATE POLICY own   ON feedback USING (user_id = current_setting('app.user_id', true));
CREATE POLICY admin ON feedback USING (current_setting('app.role', true) = 'admin');
-- The public analytics reads this table in aggregate only. The handler names
-- this role for that one transaction and its SQL never selects `note`; a test
-- pins the second half, since the policy cannot.
CREATE POLICY analytics ON feedback FOR SELECT USING (current_setting('app.role', true) = 'analytics');

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alex311_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON feedback TO alex311_app;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Accounts, phase 4: a score a resident chose to show.
--
-- `share_score` is off by default and set per verdict. This policy lets any
-- reader — signed in or not, naming no account — see rows the resident opted
-- to share. It exposes the row; the public query selects the score, the
-- relation and the status at rating, and never the note or the account. A
-- test pins that, since a policy cannot. Nothing here ever shows a note:
-- there are no public notes, so there is nothing to moderate.
DROP POLICY IF EXISTS shared ON feedback;
CREATE POLICY shared ON feedback FOR SELECT USING (share_score);
