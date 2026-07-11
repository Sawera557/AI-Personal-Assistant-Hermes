-- Hermes — global (all-businesses) context files (0011_global_documents)
--
-- Lets a context file apply to EVERY business instead of exactly one, by
-- allowing entity_id to be NULL — the same "global" idea already used for
-- owner-scope memories (see 0009_memory_schema.sql), extended to documents.
-- A NULL-entity document/chunk is searched alongside every business's own
-- documents (see hermes/vector_store.py::query()), so uploading one "all
-- businesses" note makes it available everywhere without duplicating it
-- per business.

ALTER TABLE documents ALTER COLUMN entity_id DROP NOT NULL;
ALTER TABLE document_chunks ALTER COLUMN entity_id DROP NOT NULL;
