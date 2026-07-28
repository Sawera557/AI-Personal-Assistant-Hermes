-- Hermes — 0016: token-cost columns on ai_runs.
-- ai_runs (defined in 0001_init.sql) already carries input_tokens/output_tokens
-- per turn but was never written to. The token-usage feature revives it: one row
-- per agent turn keyed by session_id/entity_id, now with the computed USD cost
-- and the cache-token split. See hermes/db/conversations.py::record_turn and
-- hermes/db/usage.py.

ALTER TABLE ai_runs ADD COLUMN IF NOT EXISTS total_cost_usd        numeric;
ALTER TABLE ai_runs ADD COLUMN IF NOT EXISTS cache_read_tokens     integer;
ALTER TABLE ai_runs ADD COLUMN IF NOT EXISTS cache_creation_tokens integer;

CREATE INDEX IF NOT EXISTS ai_runs_session_idx ON ai_runs (session_id);
