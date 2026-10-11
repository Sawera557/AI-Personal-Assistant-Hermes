-- Hermes — 0006: AI-generated pre-meeting notes (live Gmail/Drive/Calendar pull).
-- Separate from `brief` (0005) — the brief is a fast, cheap summary grounded
-- only in what's ALREADY indexed in Pinecone; ai_notes is a slower, deeper,
-- on-demand pull through the full Hermes agent (real Maton tool calls against
-- the business's own Gmail/Drive), regenerable any time the owner asks for it.

ALTER TABLE meeting_sessions
    ADD COLUMN IF NOT EXISTS ai_notes             text,
    ADD COLUMN IF NOT EXISTS ai_notes_generated_at timestamptz;
