-- Hermes — 0019: Artefacts.
--
-- A persisted, re-openable HTML "canvas" (like the reviews report) bound to a
-- LIVE data source. Opening an artefact re-fetches its source through Maton and
-- deterministically re-renders the HTML — NO LLM call on open. The AI only
-- CREATES the row (via the create_artefact agent tool). rendered_html /
-- last_synced_at are a convenience cache (instant paint, then refresh); because
-- every open re-renders from source, the cache is disposable and cascades away
-- with the row.
--
-- Idempotent (IF NOT EXISTS / DROP TRIGGER IF EXISTS); additive; safe to re-run
-- under both scripts/init_db.py and POST /api/admin/migrate.

CREATE TABLE IF NOT EXISTS artefacts (
    artefact_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- The tenant. Nullable so the 'reviews' wrapper (which may be cross-business)
    -- is allowed; every other source_type carries a real entity_id.
    entity_id      uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    -- The BUSINESS slug (afk/gak/gif/gakc/nh/per) captured at creation so a
    -- re-sync OUTSIDE a chat turn can rebuild the correct MatonClient via
    -- Config.maton_key_for_slug(slug) with no active_business in scope. This is
    -- the load-bearing column for open-time re-render.
    business_slug  text,

    title          text NOT NULL,

    -- One of: 'sheets' | 'drive' | 'docs' | 'reviews'.
    source_type    text NOT NULL
                   CHECK (source_type IN ('sheets', 'drive', 'docs', 'reviews')),

    -- How to FETCH the live source. Shape varies by source_type:
    --   sheets:  {spreadsheet_id, range:"Sheet1!A1:F200", connection_id?}
    --   drive:   {file_id, connection_id?}
    --   docs:    {document_id, connection_id?}
    --   reviews: {date:"YYYY-MM-DD"}  (or {} for the latest)
    source_binding jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- How to RENDER the normalized data deterministically:
    --   {render:"table"|"kv"|"raw"|"reviews", title?, columns?:[...],
    --    options?:{header_row, max_rows, theme, ...}}
    render_recipe  jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Convenience cache — the last successfully rendered HTML + when. Disposable.
    rendered_html  text,
    last_synced_at timestamptz,
    last_error     text,        -- last sync failure message (source deleted, etc.)

    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artefacts_user_idx   ON artefacts (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS artefacts_entity_idx ON artefacts (entity_id);

DROP TRIGGER IF EXISTS artefacts_set_updated_at ON artefacts;
CREATE TRIGGER artefacts_set_updated_at BEFORE UPDATE ON artefacts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
