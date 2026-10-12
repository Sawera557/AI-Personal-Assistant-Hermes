"""Ingest a file into the knowledge base so it becomes searchable by meaning.

Pipeline (per the project scope doc):
    bytes -> S3 (the locker) -> documents row (the record)
          -> chunk text -> document_chunks rows -> embed -> write vectors
             onto those rows (the search brain, pgvector)

Every chunk row carries entity_id + sensitivity + region, so the guarded
search can filter without trusting the caller. Postgres stays the truth: a
chunk row (and now its embedding) live in the same table, no separate system.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from . import vector_store as ps
from .db import documents as docs_repo
from .db.connection import cursor

log = logging.getLogger("hermes.ingest")

_TEXT_DECODABLE_HINTS = ("text/", "json", "csv", "xml", "markdown", "yaml", "html")


def chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping character windows on whitespace boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:  # don't cut mid-word — back up to the last space
            space = text.rfind(" ", start + size - overlap, end)
            if space > start:
                end = space
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _extract_text(raw: bytes | None, text: str | None, mime: str) -> str | None:
    """Best-effort text for embedding. Text-like bytes are decoded; binaries
    (pdf/image) get no text here — wire pdf-co / pypdf later to extract them."""
    if text is not None:
        return text
    if raw is None:
        return None
    mime = (mime or "").lower()
    if any(h in mime for h in _TEXT_DECODABLE_HINTS) or mime == "":
        try:
            decoded = raw.decode("utf-8")
            # Reject things that decode but are clearly binary noise.
            if "\x00" not in decoded:
                return decoded
        except UnicodeDecodeError:
            return None
    return None


def _reindex(
    *,
    entity_uuid: str | None,
    slug: str | None,
    region: str,
    document_id: str,
    body: str | None,
    sensitivity_level: str,
    source_system: str,
) -> tuple[int, int]:
    """Chunk `body`, create the document_chunks rows, then embed (the
    pgvector-backed upsert UPDATEs those rows by pinecone_vector_id, so they
    must exist first). Returns (chunk_count, embedded_count). Shared by
    ingest_document (new document) and update_document (content edit — caller
    is expected to have already cleared any prior chunks for this document).

    `entity_uuid`/`slug` are None for a GLOBAL (all-businesses) document —
    every chunk row then carries entity_id=NULL, and hermes.vector_store's
    query() matches those alongside any specific business's own chunks."""
    pieces = chunk_text(body) if body else []
    if not pieces:
        return 0, 0
    namespace = ps.entity_namespace(slug) if slug else ps.global_namespace()
    docs_repo.add_chunks([
        {
            "document_id": document_id,
            "entity_id": entity_uuid,
            "chunk_index": i,
            "pinecone_vector_id": f"{document_id}:{i}",
            "sensitivity_level": sensitivity_level,
            "region": region,
        }
        for i in range(len(pieces))
    ])
    vectors = [
        {
            "id": f"{document_id}:{i}",
            "text": piece,
            "entity_id": entity_uuid,
            "document_id": document_id,
            "chunk_id": f"{document_id}:{i}",
            "sensitivity_level": sensitivity_level,
            "region": region,
            "source_system": source_system,
            "version": 1,
            "is_active": True,
        }
        for i, piece in enumerate(pieces)
    ]
    embedded = ps.upsert_chunks(namespace, vectors)
    return len(pieces), embedded


_GLOBAL_REGION = "us-east-1"  # default region for a document with no single owning business


def _discard_partial(document_id: str, s3_uri: str | None, recorded: bool) -> None:
    """Undo a failed ingest: the documents row (its chunks cascade) and the S3
    object. Best-effort — a cleanup failure is logged, never raised, so the
    caller still sees the ORIGINAL error that made the ingest fail."""
    from . import storage  # lazy, as in ingest_document

    if recorded:
        try:
            docs_repo.delete_document(document_id)
        except Exception:  # noqa: BLE001
            log.exception("ingest rollback: could not delete document %s", document_id)
    if s3_uri:
        try:
            storage.delete(s3_uri)
        except Exception:  # noqa: BLE001
            log.exception("ingest rollback: could not delete %s", s3_uri)


def ingest_document(
    entity_id: str | None,
    *,
    filename: str,
    mime: str = "",
    raw: bytes | None = None,
    text: str | None = None,
    message_id: str | None = None,
    source_system: str = "upload",
    doc_type: str | None = None,
    sensitivity_level: str = "internal",
) -> dict[str, Any]:
    """Ingest one file. `entity_id=None` makes it GLOBAL (all-businesses) —
    searched alongside every business's own documents, see vector_store.query().
    Returns {document_id, chunks, s3_uri, embedded}."""
    from . import storage  # lazy

    if entity_id:
        with cursor(commit=False) as cur:
            resolved = docs_repo.resolve_entity(cur, entity_id)
        if resolved is None:
            raise ValueError(f"Unknown entity: {entity_id!r}")
        entity_uuid, slug, region = resolved
    else:
        entity_uuid, slug, region = None, None, _GLOBAL_REGION

    document_id = str(uuid.uuid4())

    # All-or-nothing: if any step below fails (most often the embedding call),
    # everything already written for this document is removed before the error
    # propagates. Previously a failed upload answered 500 yet left the row (and
    # un-embedded chunks and the S3 object) behind — a document that showed on
    # the Context page but could never be found by search.
    s3_uri = None
    recorded = False
    try:
        # 1) Bytes -> S3 (optional). Project-first layout (PLAN §9b.7):
        #    "<project-slug>/uploads/<document_id>/<filename>"
        if raw is not None and storage.enabled():
            key = f"{slug or 'global'}/uploads/{document_id}/{storage.sanitize_filename(filename)}"
            s3_uri = storage.put(key, raw, mime or "application/octet-stream")

        # 2) Record the document.
        body = _extract_text(raw, text, mime)
        docs_repo.create_document(
            document_id=document_id,
            entity_uuid=entity_uuid,
            title=filename,
            s3_uri=s3_uri,
            source_system=source_system,
            doc_type=doc_type or (mime or None),
            sensitivity_level=sensitivity_level,
            message_id=message_id,
            metadata={"filename": filename, "mime": mime},
            body_text=body,
        )
        recorded = True

        # 3) Chunk + create rows + embed.
        chunks, embedded = _reindex(
            entity_uuid=entity_uuid, slug=slug, region=region, document_id=document_id,
            body=body, sensitivity_level=sensitivity_level, source_system=source_system,
        )
    except Exception:
        _discard_partial(document_id, s3_uri, recorded)
        raise

    return {
        "document_id": document_id,
        "entity_id": entity_uuid,
        "slug": slug,
        "chunks": chunks,
        "embedded": embedded,
        "s3_uri": s3_uri,
    }


def update_document(
    document_id: str,
    *,
    title: str | None = None,
    sensitivity_level: str | None = None,
    body_text: str | None = None,
) -> dict[str, Any]:
    """Edit an existing document's metadata and/or content. If `body_text` is
    given, the old chunks/embeddings are discarded and regenerated from the
    new text — search results (including search_business_knowledge) reflect
    the edit immediately, never the stale version."""
    doc = docs_repo.get_document(document_id)
    if doc is None:
        raise ValueError(f"Unknown document: {document_id!r}")

    docs_repo.update_document_row(
        document_id, title=title, sensitivity_level=sensitivity_level, body_text=body_text,
    )

    if body_text is not None:
        if doc["entity_id"]:
            with cursor(commit=False) as cur:
                resolved = docs_repo.resolve_entity(cur, doc["entity_id"])
            if resolved is None:
                raise ValueError(f"Unknown entity: {doc['entity_id']!r}")
            entity_uuid, slug, region = resolved
        else:
            entity_uuid, slug, region = None, None, _GLOBAL_REGION  # global document
        docs_repo.delete_chunks(document_id)
        _reindex(
            entity_uuid=entity_uuid, slug=slug, region=region, document_id=document_id,
            body=body_text, sensitivity_level=sensitivity_level or doc["sensitivity_level"],
            source_system=doc["source_system"] or "upload",
        )

    updated = docs_repo.get_document(document_id)
    if updated is None:
        raise ValueError(f"Unknown document: {document_id!r}")
    return updated
