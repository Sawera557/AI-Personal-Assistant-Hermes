-- Hermes — 0007: platform-wide key/value settings.
-- First use: the active agent model (Claude vs OpenAI, cheap vs expensive
-- tier), switchable from the UI and shared across every user/session — see
-- hermes/models.py.

CREATE TABLE IF NOT EXISTS hermes_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
