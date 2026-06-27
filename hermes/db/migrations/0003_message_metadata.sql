-- Hermes — 0003: replay fidelity for chat history.
-- The UI shows each assistant turn with its reasoning summary and tool traces.
-- hermes_messages stores the text; this jsonb column carries the extras
-- (thinking, tool_calls, stop_reason) so a reloaded conversation looks the same
-- as it did live. Supporting/extra fields as jsonb match the spec's convention.

ALTER TABLE hermes_messages
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
