"""Postgres rows for the knowledge base: documents + document_chunks.

documents     — one row per file (S3 holds the bytes via s3_uri).
document_chunks — one row per chunk; pinecone_vector_id links to the vector.

Keeps Postgres the source of truth: every vector in Pinecone has a matching
chunk row here, so search results can be joined back to a real document.
"""
from __future__ import annotations

import json
import uuid as _uuid
from typing import Any

from .connection import cursor


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def resolve_entity(cur, entity_id: str | None) -> tuple[str, str, str] | None:
    """Return (entity_uuid, slug, region) for a uuid OR slug, else None."""
    if not entity_id:
        return None
    eid = str(entity_id).strip()
    if not eid:
        return None
    col = "entity_id" if _is_uuid(eid) else "slug"
    cur.execute(
        f"SELECT entity_id, slug, region FROM entities WHERE {col} = %s;", (eid,)
    )
    row = cur.fetchone()
    return (str(row[0]), row[1], row[2]) if row else None


def create_document(
    *,
    document_id: str,
    entity_uuid: str,
    title: str,
    s3_uri: str | None,
    source_system: str,
    doc_type: str | None,
    sensitivity_level: str,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (document_id, entity_id, message_id, source_system, doc_type,
                 title, s3_uri, sensitivity_level, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                document_id, entity_uuid, message_id, source_system, doc_type,
                title, s3_uri, sensitivity_level, json.dumps(metadata or {}),
            ),
        )


def add_chunks(rows: list[dict[str, Any]]) -> int:
    """Insert document_chunks rows. Each: document_id, entity_id, chunk_index,
    pinecone_vector_id, sensitivity_level, region."""
    if not rows:
        return 0
    with cursor() as cur:
        cur.executemany(
            """
            INSERT INTO document_chunks
                (document_id, entity_id, chunk_index, pinecone_vector_id,
                 sensitivity_level, region)
            VALUES (%(document_id)s, %(entity_id)s, %(chunk_index)s,
                    %(pinecone_vector_id)s, %(sensitivity_level)s, %(region)s);
            """,
            rows,
        )
    return len(rows)


def documents_by_id(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Look up document metadata for a set of ids (for enriching search hits)."""
    ids = [i for i in dict.fromkeys(ids) if i and _is_uuid(i)]
    if not ids:
        return {}
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT document_id, title, s3_uri, doc_type, source_system, "
            "sensitivity_level FROM documents WHERE document_id = ANY(%s);",
            (ids,),
        )
        rows = cur.fetchall()
    return {
        str(r[0]): {
            "document_id": str(r[0]),
            "title": r[1],
            "s3_uri": r[2],
            "doc_type": r[3],
            "source_system": r[4],
            "sensitivity_level": r[5],
        }
        for r in rows
    }
