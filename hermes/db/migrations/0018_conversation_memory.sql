-- Hermes — 0018: Conversation-history memory (Phase 2, "GOD mode" recall).
--
-- The durable, PROVENANCE-FIRST record of past conversations, so Hermes recalls
-- what was actually said, WHEN, and in WHICH business — not a distilled
-- impression. Deliberately pgvector on this same Postgres (NOT Pinecone): keeps
-- conversation recall independent of the business-knowledge RAG so the two can
-- never share a failure, and mirrors the existing `memories` table exactly.
--
-- Zero-hallucination stance baked into the schema:
--   * verbatim `text` is the citable evidence (recall retrieves it, never invents);
--   * (session_id, seq, business_slug, occurred_at) = full provenance for every row
--     so any recalled claim traces back to the exact turn and date;
--   * status/superseded_by give newest-wins conflict resolution (stale never wins);
--   * two kinds: 'turn' (verbatim, high-fidelity) and 'summary' (extractive,
--     navigational, idempotent by content_hash).
--
-- Idempotent (IF NOT EXISTS); additive; safe to re-run.

CREATE TABLE IF NOT EXISTS conversation_memory (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL,                         -- owner (single-owner app)
    session_id    uuid,                                  -- source conversation (provenance)
    kind          text NOT NULL CHECK (kind IN ('turn','summary')),
    seq           integer,                               -- turn position within the session
    text          text NOT NULL,                         -- verbatim exchange | extractive summary
    embedding     vector(1536),                          -- text-embedding-3-small, shared space
    embed_model   text,
    business_slug text,                                  -- business scope at the time (NULL = all)
    content_hash  text,                                  -- idempotency (summaries) / dedupe
    occurred_at   timestamptz NOT NULL DEFAULT now(),    -- WHEN it was said (temporal provenance)
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded','archived')),
    superseded_by uuid REFERENCES conversation_memory(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Semantic recall: cosine HNSW over active rows (matches memories/document_chunks).
CREATE INDEX IF NOT EXISTS ix_convmem_embedding_hnsw
    ON conversation_memory USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Owner + recency scans (recency is a first-class signal).
CREATE INDEX IF NOT EXISTS ix_convmem_user_time
    ON conversation_memory (user_id, occurred_at DESC)
    WHERE status = 'active';

-- One active summary per (session, content) — the idempotent-upsert guard so a
-- re-summarised session doesn't accumulate duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS ux_convmem_summary_ident
    ON conversation_memory (session_id, content_hash)
    WHERE kind = 'summary' AND status = 'active';
