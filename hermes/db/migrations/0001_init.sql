-- Hermes — initial schema (0001_init)
-- Plain-language reference: PER_SPEC_DRAFT_HERMES_PROJECT-SCOPE.
--
-- Design rules baked in (from the spec's "Safety rules that never bend"):
--   * Every business is isolated by entity_id. It is denormalized onto child
--     rows (messages, chunks, ...) so the backend can filter cheaply.
--   * Secrets are NEVER stored here — integration_connections.secret_ref is a
--     POINTER to a secrets vault, not the credential.
--   * sensitivity_level travels with rows so search/answer can filter by it.
--   * Postgres is the record keeper. Real files live in S3 (documents.s3_uri).
--     Pinecone holds only a search copy (document_chunks.pinecone_vector_id).

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Shared helper: keep updated_at fresh on UPDATE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Sensitivity ladder used across the whole schema. Kept as a domain (not an
-- enum) so values are easy to extend without an ALTER TYPE migration.
DO $$ BEGIN
    CREATE DOMAIN sensitivity AS text
        CHECK (VALUE IN ('public', 'internal', 'confidential', 'restricted'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ===========================================================================
-- CORE TABLES
-- ===========================================================================

-- users — one row per person.
CREATE TABLE IF NOT EXISTS users (
    user_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    email       text NOT NULL,
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'disabled')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email));
DROP TRIGGER IF EXISTS users_set_updated_at ON users;
CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- roles — named permission levels (owner, admin, read-only).
CREATE TABLE IF NOT EXISTS roles (
    role_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL UNIQUE,
    description text
);

-- entities — one row per business (the 8). `slug` is the stable handle used as
-- the Pinecone namespace (entity__<slug>) and in URLs.
CREATE TABLE IF NOT EXISTS entities (
    entity_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          text NOT NULL UNIQUE,
    name          text NOT NULL,
    business_type text,
    country       text,
    region        text NOT NULL DEFAULT 'us-east-1',
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended', 'archived')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS entities_set_updated_at ON entities;
CREATE TRIGGER entities_set_updated_at BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- user_entity_access — THE GATE. Who can see which business and do what.
CREATE TABLE IF NOT EXISTS user_entity_access (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    entity_id        uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    role_id          uuid NOT NULL REFERENCES roles (role_id),
    permission_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    granted_by       uuid REFERENCES users (user_id),
    granted_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, entity_id)
);
CREATE INDEX IF NOT EXISTS uea_user_idx   ON user_entity_access (user_id);
CREATE INDEX IF NOT EXISTS uea_entity_idx ON user_entity_access (entity_id);

-- connected_apps — the catalog of app TYPES (Gmail, Drive, QuickBooks…).
CREATE TABLE IF NOT EXISTS connected_apps (
    app_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug     text NOT NULL UNIQUE,         -- matches HERMES_APPS slugs (google-mail…)
    name     text NOT NULL,                -- friendly label (Gmail)
    category text,
    provider text
);

-- integration_connections — a real connected account for one business.
-- The credential is NOT here; secret_ref points to the vault.
CREATE TABLE IF NOT EXISTS integration_connections (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id         uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    app_id            uuid NOT NULL REFERENCES connected_apps (app_id),
    account_ref       text,               -- e.g. the connected email / account id
    secret_ref        text,               -- POINTER to the secrets vault (never the secret)
    namespace         text,               -- vault / provider namespace
    sensitivity_level sensitivity NOT NULL DEFAULT 'confidential',
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'disconnected', 'error', 'pending')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity_id, app_id, account_ref)
);
CREATE INDEX IF NOT EXISTS ic_entity_idx ON integration_connections (entity_id);
DROP TRIGGER IF EXISTS ic_set_updated_at ON integration_connections;
CREATE TRIGGER ic_set_updated_at BEFORE UPDATE ON integration_connections
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- hermes_sessions — one row per chat (builds the history sidebar).
CREATE TABLE IF NOT EXISTS hermes_sessions (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    title      text,
    status     text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'archived', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON hermes_sessions (user_id, updated_at DESC);
DROP TRIGGER IF EXISTS sessions_set_updated_at ON hermes_sessions;
CREATE TRIGGER sessions_set_updated_at BEFORE UPDATE ON hermes_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- hermes_messages — one row per message. entity_id may be NULL (personal/none).
CREATE TABLE IF NOT EXISTS hermes_messages (
    message_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL REFERENCES hermes_sessions (session_id) ON DELETE CASCADE,
    entity_id         uuid REFERENCES entities (entity_id) ON DELETE SET NULL,
    role              text NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content           text,
    seq               integer NOT NULL,
    sensitivity_level sensitivity NOT NULL DEFAULT 'internal',
    model_used        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS messages_session_idx ON hermes_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS messages_entity_idx  ON hermes_messages (entity_id);

-- documents — one row per file (uploaded, AI-made, or synced). File sits in S3.
CREATE TABLE IF NOT EXISTS documents (
    document_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id         uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    message_id        uuid REFERENCES hermes_messages (message_id) ON DELETE SET NULL,
    source_system     text,
    doc_type          text,
    title             text,
    s3_uri            text,
    sensitivity_level sensitivity NOT NULL DEFAULT 'internal',
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'processing', 'archived', 'deleted')),
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_entity_idx  ON documents (entity_id);
CREATE INDEX IF NOT EXISTS documents_message_idx ON documents (message_id);
DROP TRIGGER IF EXISTS documents_set_updated_at ON documents;
CREATE TRIGGER documents_set_updated_at BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- document_chunks — a document split into pieces; each row points to its vector.
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       uuid NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    entity_id         uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    chunk_index       integer NOT NULL,
    pinecone_vector_id text,
    sensitivity_level sensitivity NOT NULL DEFAULT 'internal',
    region            text NOT NULL DEFAULT 'us-east-1',
    version           integer NOT NULL DEFAULT 1,
    is_active         boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index, version)
);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON document_chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_entity_idx   ON document_chunks (entity_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS chunks_vector_idx   ON document_chunks (pinecone_vector_id);

-- ===========================================================================
-- SUPPORTING TABLES — same isolation rules apply (entity_id, sensitivity, …).
-- ===========================================================================

-- sync_jobs — tracks each app sync run.
CREATE TABLE IF NOT EXISTS sync_jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    app_id        uuid REFERENCES connected_apps (app_id),
    source_system text,
    status        text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'success', 'error')),
    started_at    timestamptz,
    finished_at   timestamptz,
    error         text,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sync_jobs_entity_idx ON sync_jobs (entity_id);
DROP TRIGGER IF EXISTS sync_jobs_set_updated_at ON sync_jobs;
CREATE TRIGGER sync_jobs_set_updated_at BEFORE UPDATE ON sync_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ai_runs — record each AI job for tracing / analytics.
CREATE TABLE IF NOT EXISTS ai_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE SET NULL,
    session_id    uuid REFERENCES hermes_sessions (session_id) ON DELETE SET NULL,
    user_id       uuid REFERENCES users (user_id) ON DELETE SET NULL,
    model_used    text,
    input_tokens  integer,
    output_tokens integer,
    status        text NOT NULL DEFAULT 'success',
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_runs_entity_idx  ON ai_runs (entity_id);
CREATE INDEX IF NOT EXISTS ai_runs_session_idx ON ai_runs (session_id);

-- hermes_agent_runs — one row per agent turn (the tool-use loop).
CREATE TABLE IF NOT EXISTS hermes_agent_runs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid NOT NULL REFERENCES hermes_sessions (session_id) ON DELETE CASCADE,
    message_id  uuid REFERENCES hermes_messages (message_id) ON DELETE SET NULL,
    entity_id   uuid REFERENCES entities (entity_id) ON DELETE SET NULL,
    model_used  text,
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'success', 'error', 'refusal')),
    stop_reason text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS agent_runs_session_idx ON hermes_agent_runs (session_id);

-- hermes_tool_calls — one row per tool call inside an agent run.
CREATE TABLE IF NOT EXISTS hermes_tool_calls (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_run_id uuid NOT NULL REFERENCES hermes_agent_runs (id) ON DELETE CASCADE,
    tool_name    text NOT NULL,
    tool_input   jsonb NOT NULL DEFAULT '{}'::jsonb,
    ok           boolean,
    output       text,
    latency_ms   integer,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_run_idx ON hermes_tool_calls (agent_run_id);

-- approvals — the queue of real-world actions waiting for the owner's "go".
CREATE TABLE IF NOT EXISTS approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type         text NOT NULL,           -- send_email, qb_write, calendar_create, …
    entity_id    uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    requested_by uuid REFERENCES users (user_id) ON DELETE SET NULL,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by   uuid REFERENCES users (user_id),
    decided_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_entity_idx ON approvals (entity_id);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals (status) WHERE status = 'pending';

-- audit_logs — record of every read and write.
CREATE TABLE IF NOT EXISTS audit_logs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid REFERENCES users (user_id) ON DELETE SET NULL,
    entity_id   uuid REFERENCES entities (entity_id) ON DELETE SET NULL,
    action      text NOT NULL,            -- read, write, search, login, …
    source      text,
    model_used  text,
    target_type text,
    target_id   text,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_logs (entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_user_idx   ON audit_logs (user_id, created_at DESC);

-- activity_events — feeds the Activity Analytics dashboard.
CREATE TABLE IF NOT EXISTS activity_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    user_id       uuid REFERENCES users (user_id) ON DELETE SET NULL,
    event_type    text NOT NULL,
    source_system text,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_entity_idx ON activity_events (entity_id, created_at DESC);

-- pipeline_runs — feeds the Data Pipeline dashboard.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id   uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    pipeline    text NOT NULL,
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'success', 'error')),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pipeline_entity_idx ON pipeline_runs (entity_id);

-- notifications — owner alerts.
CREATE TABLE IF NOT EXISTS notifications (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    entity_id  uuid REFERENCES entities (entity_id) ON DELETE SET NULL,
    kind       text,
    title      text,
    body       text,
    read_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications (user_id, created_at DESC);

-- quickbooks_snapshots — saved QuickBooks summaries per business (read-only).
CREATE TABLE IF NOT EXISTS quickbooks_snapshots (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id    uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    report_type  text NOT NULL,           -- profit_loss, balance_sheet, ar_aging, …
    period_start date,
    period_end   date,
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qb_entity_idx ON quickbooks_snapshots (entity_id, report_type);

-- calendar_events — synced meetings and deadlines.
CREATE TABLE IF NOT EXISTS calendar_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    source_system text,
    external_id   text,
    title         text,
    starts_at     timestamptz,
    ends_at       timestamptz,
    location      text,
    attendees     jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, external_id)
);
CREATE INDEX IF NOT EXISTS calendar_entity_idx ON calendar_events (entity_id, starts_at);
DROP TRIGGER IF EXISTS calendar_set_updated_at ON calendar_events;
CREATE TRIGGER calendar_set_updated_at BEFORE UPDATE ON calendar_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- email_threads — synced, classified email threads.
CREATE TABLE IF NOT EXISTS email_threads (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id      uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    source_system  text,
    external_id    text,
    subject        text,
    classification text,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, external_id)
);
CREATE INDEX IF NOT EXISTS email_threads_entity_idx ON email_threads (entity_id);
DROP TRIGGER IF EXISTS email_threads_set_updated_at ON email_threads;
CREATE TRIGGER email_threads_set_updated_at BEFORE UPDATE ON email_threads
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- email_messages — individual emails inside a thread.
CREATE TABLE IF NOT EXISTS email_messages (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id         uuid NOT NULL REFERENCES email_threads (id) ON DELETE CASCADE,
    entity_id         uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    external_id       text,
    sender            text,
    recipients        jsonb NOT NULL DEFAULT '[]'::jsonb,
    sent_at           timestamptz,
    snippet           text,
    sensitivity_level sensitivity NOT NULL DEFAULT 'confidential',
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, external_id)
);
CREATE INDEX IF NOT EXISTS email_messages_thread_idx ON email_messages (thread_id, sent_at);

-- farm_form_submissions — Afro Farm form data.
CREATE TABLE IF NOT EXISTS farm_form_submissions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id    uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    form_ref     text,
    submitted_at timestamptz,
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS farm_forms_entity_idx ON farm_form_submissions (entity_id);
