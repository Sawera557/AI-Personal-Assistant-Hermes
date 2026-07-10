-- Hermes — 0005: full meeting lifecycle (scheduled -> prepared -> active -> ended).
-- Extends meeting_sessions (0004) rather than fragmenting into more tables — a
-- "meeting" is one thing whether it's upcoming, live, or past; these columns
-- just fill in as it moves through its life.

ALTER TABLE meeting_sessions
    ADD COLUMN IF NOT EXISTS calendar_event_id text,
    ADD COLUMN IF NOT EXISTS scheduled_at      timestamptz,
    ADD COLUMN IF NOT EXISTS attendees         jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS brief             jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS agenda_summary    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS fathom_url        text;

CREATE UNIQUE INDEX IF NOT EXISTS meeting_sessions_calendar_event_idx
    ON meeting_sessions (entity_id, calendar_event_id)
    WHERE calendar_event_id IS NOT NULL;

-- Allow the fuller lifecycle. Postgres has no ADD CONSTRAINT IF NOT EXISTS for
-- CHECK, so drop-then-add defensively (the DO block makes a re-run harmless).
ALTER TABLE meeting_sessions DROP CONSTRAINT IF EXISTS meeting_sessions_status_check;
ALTER TABLE meeting_sessions ADD CONSTRAINT meeting_sessions_status_check
    CHECK (status IN ('scheduled', 'prepared', 'active', 'ended', 'deleted'));
