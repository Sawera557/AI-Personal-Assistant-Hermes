-- Hermes — pgvector cutover (0008_pgvector)
--
-- Replaces Pinecone as the vector-search backend with `pgvector` on this same
-- Postgres instance: one storage stack, no third-party vector vendor holding
-- lease/PHI/business data. `document_chunks.pinecone_vector_id` is kept as-is
-- (now just "the chunk's vector key") so hermes/db/documents.py's existing SQL
-- doesn't need to change — only the embedding itself moves here.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embed_model text;
-- Small preview of the chunk's own text (first 2000 chars), for displaying a
-- search-result snippet — mirrors what the old Pinecone metadata carried.
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS text_preview text;

-- The pgvector-backed upsert targets a chunk row by pinecone_vector_id
-- (UPDATE ... WHERE pinecone_vector_id = %s) — this must be unique to be a
-- safe target. NULLs (chunks written before this migration, if any) are
-- excluded rather than colliding.
CREATE UNIQUE INDEX IF NOT EXISTS chunks_vector_id_uidx
    ON document_chunks (pinecone_vector_id) WHERE pinecone_vector_id IS NOT NULL;

-- vector_cosine_ops matches Pinecone's existing metric="cosine" config, so
-- relevance behavior doesn't silently shift on cutover.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
