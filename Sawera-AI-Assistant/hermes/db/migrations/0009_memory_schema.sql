-- Hermes — persistent memory schema (0009_memory_schema)
--
-- Five memory layers, modeled as two orthogonal dimensions rather than five
-- tables:
--   * scope_type ('owner' | 'business' | 'project') — WHOSE memory this is.
--     Owner is global (this app has exactly one hardcoded owner identity, see
--     OWNER_EMAIL/OWNER_NAME in hermes/db/conversations.py + hermes/db/meetings.py
--     — no per-request multi-user auth exists today). Business ties to one of
--     the 8 `entities` rows. Project ties to a `projects` row, which may or may
--     not itself have an entity_id (engineering projects like "Data Ingestion
--     API" don't belong to any business).
--   * memory_type (.... incl. 'decision') — WHAT KIND of content it is. A
--     decision can occur at any scope, so it's a type, not a 4th scope value.
--
-- Same isolation rules as the rest of the schema: entity_id FK + the existing
-- `sensitivity` domain (defined in 0001_init.sql) travel with every row.

CREATE TABLE IF NOT EXISTS projects (
    project_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id   uuid REFERENCES entities (entity_id) ON DELETE SET NULL,  -- NULL = pure engineering project
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    kind        text NOT NULL DEFAULT 'business'
                CHECK (kind IN ('business', 'engineering')),
    description text,
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'archived')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS projects_entity_idx ON projects (entity_id);
DROP TRIGGER IF EXISTS projects_set_updated_at ON projects;
CREATE TRIGGER projects_set_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS memories (
    memory_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type        text NOT NULL CHECK (scope_type IN ('owner', 'business', 'project')),
    entity_id         uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    project_id        uuid REFERENCES projects (project_id) ON DELETE CASCADE,
    memory_type       text NOT NULL CHECK (memory_type IN
                        ('preference', 'style', 'rule', 'fact', 'decision', 'project_update')),
    text              text NOT NULL,
    embedding         vector(1536),
    embed_model       text,
    sensitivity_level sensitivity NOT NULL DEFAULT 'internal',
    region            text NOT NULL DEFAULT 'us-east-1',
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'superseded', 'archived')),
    superseded_by     uuid REFERENCES memories (memory_id) ON DELETE SET NULL,
    always_inject     boolean NOT NULL DEFAULT false,
    source_session_id uuid REFERENCES hermes_sessions (session_id) ON DELETE SET NULL,
    source_message_id uuid REFERENCES hermes_messages (message_id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (scope_type = 'owner'    AND entity_id IS NULL     AND project_id IS NULL) OR
        (scope_type = 'business' AND entity_id IS NOT NULL) OR
        (scope_type = 'project'  AND project_id IS NOT NULL)
    )
);
DROP TRIGGER IF EXISTS memories_set_updated_at ON memories;
CREATE TRIGGER memories_set_updated_at BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS memories_embedding_hnsw_idx
    ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Fast path: always-inject rows are looked up by scope, never by vector search.
CREATE INDEX IF NOT EXISTS memories_always_inject_idx
    ON memories (scope_type, entity_id, project_id) WHERE status = 'active' AND always_inject;

CREATE INDEX IF NOT EXISTS memories_entity_idx  ON memories (entity_id)  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS memories_project_idx ON memories (project_id) WHERE status = 'active';
