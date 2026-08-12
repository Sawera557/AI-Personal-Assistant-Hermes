-- Hermes — 0020: Generated files.
--
-- A durable index of every file the agent produces via the create_file tool
-- (PDF / DOCX / XLSX / HTML / text → S3). Before this, a generated file lived
-- only behind a 24h presigned link that nothing recorded, so it vanished from
-- the UI once the link expired. This table lets the Library tab list them and
-- mint a FRESH download link on demand from the stored s3_uri.
--
-- Idempotent (IF NOT EXISTS); additive; safe to re-run.

CREATE TABLE IF NOT EXISTS generated_files (
    file_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- Nullable: create_file can run with no business scope ('general').
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,
    business_slug text,
    filename      text NOT NULL,
    content_type  text,
    s3_uri        text NOT NULL,      -- durable pointer; links are re-presigned on demand
    size_bytes    bigint,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS generated_files_user_idx   ON generated_files (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS generated_files_entity_idx ON generated_files (entity_id);
