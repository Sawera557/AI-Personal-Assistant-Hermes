"""Ingest a file into the knowledge base so it becomes searchable by meaning.

Pipeline (per the project scope doc):
    bytes -> S3 (the locker) -> documents row (the record)
          -> chunk text -> embed -> Pinecone (the search brain)
          -> document_chunks rows (each links to its pinecone_vector_id)

Every vector carries entity_id + sensitivity + region in its metadata, so the
guarded search can filter without trusting the caller. Postgres stays the truth:
a chunk row exists for every vector.
"""
from __future__ import annotations

import uuid
from typing import Any

from . import pinecone_store as ps
from .db import documents as docs_repo
from .db.connection import cursor

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


def ingest_document(
    entity_id: str,
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
    """Ingest one file. Returns {document_id, chunks, s3_uri, embedded}."""
    from . import storage  # lazy

    with cursor(commit=False) as cur:
        resolved = docs_repo.resolve_entity(cur, entity_id)
    if resolved is None:
        raise ValueError(f"Unknown entity: {entity_id!r}")
    entity_uuid, slug, region = resolved

    document_id = str(uuid.uuid4())

    # 1) Bytes -> S3 (optional).
    s3_uri = None
    if raw is not None and storage.enabled():
        key = f"{slug}/{document_id}/{filename}"
        s3_uri = storage.put(key, raw, mime or "application/octet-stream")

    # 2) Record the document.
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
    )

    # 3) Chunk + embed + upsert to Pinecone, then link chunk rows.
    body = _extract_text(raw, text, mime)
    pieces = chunk_text(body) if body else []
    embedded = 0
    if pieces:
        namespace = ps.entity_namespace(slug)
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

    return {
        "document_id": document_id,
        "entity_id": entity_uuid,
        "slug": slug,
        "chunks": len(pieces),
        "embedded": embedded,
        "s3_uri": s3_uri,
    }
