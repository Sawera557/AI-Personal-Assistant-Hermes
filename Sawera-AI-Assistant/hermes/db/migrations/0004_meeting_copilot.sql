-- Hermes — 0004: meeting copilot history.
-- Mirrors hermes_sessions/hermes_messages so past meeting copilot activity is
-- retrievable the same way past chat conversations are: a history sidebar,
-- click to reopen, full replay. A meeting session is ALWAYS locked to one
-- specific business (entity_id NOT NULL) — the copilot never runs unscoped.

-- meeting_sessions — one row per meeting, owned by the single owner user.
CREATE TABLE IF NOT EXISTS meeting_sessions (
    session_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    entity_id   uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    title       text,
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'ended', 'deleted')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS meeting_sessions_user_idx   ON meeting_sessions (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS meeting_sessions_entity_idx ON meeting_sessions (entity_id, updated_at DESC);
DROP TRIGGER IF EXISTS meeting_sessions_set_updated_at ON meeting_sessions;
CREATE TRIGGER meeting_sessions_set_updated_at BEFORE UPDATE ON meeting_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- meeting_turns — one row per heard/typed utterance + the guidance it produced.
-- bullets/sources stored as jsonb so a reopened meeting renders identically
-- to how it looked live (same convention as hermes_messages.metadata).
CREATE TABLE IF NOT EXISTS meeting_turns (
    turn_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  uuid NOT NULL REFERENCES meeting_sessions (session_id) ON DELETE CASCADE,
    entity_id   uuid NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    heard_text  text,
    bullets     jsonb NOT NULL DEFAULT '[]'::jsonb,
    sources     jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS meeting_turns_session_idx ON meeting_turns (session_id, seq);
