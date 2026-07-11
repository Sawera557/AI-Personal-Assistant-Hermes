-- Hermes — full document text for editable context files (0010_document_body_text)
--
-- Nullable, populated only for text-based ingests (binary/PDF docs stay NULL,
-- unchanged behavior). Without this, there is nothing to load into an editor —
-- only S3 bytes (optional) or lossy 2000-char chunk previews existed before.

ALTER TABLE documents ADD COLUMN IF NOT EXISTS body_text text;
