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


def list_entities() -> list[dict[str, Any]]:
    """Active businesses (slug + name), for prompts that need the full roster
    (e.g. the memory extractor's classification prompt)."""
    with cursor(commit=False) as cur:
        cur.execute("SELECT entity_id, slug, name FROM entities WHERE status = 'active' ORDER BY name;")
        rows = cur.fetchall()
    return [{"entity_id": str(r[0]), "slug": r[1], "name": r[2]} for r in rows]


def create_document(
    *,
    document_id: str,
    entity_uuid: str | None,
    title: str,
    s3_uri: str | None,
    source_system: str,
    doc_type: str | None,
    sensitivity_level: str,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    body_text: str | None = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (document_id, entity_id, message_id, source_system, doc_type,
                 title, s3_uri, sensitivity_level, metadata, body_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                document_id, entity_uuid, message_id, source_system, doc_type,
                title, s3_uri, sensitivity_level, json.dumps(metadata or {}), body_text,
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


def list_documents(entity_id: str | None, source_system: str | None = None) -> list[dict[str, Any]]:
    """Active documents, newest first — optionally filtered to one
    `source_system` (e.g. "business_context" for the Context tab).

    `entity_id=None` lists across EVERY business PLUS global (all-businesses)
    documents — used by the "All Businesses" Context view; each row is tagged
    with entity_slug/entity_name (both None for a global doc) so the caller
    can group by business. A real entity_id/slug/uuid scopes to that business's
    own documents plus global ones (LEFT JOIN — a global doc has no matching
    `entities` row, so an inner join would silently drop it)."""
    with cursor(commit=False) as cur:
        entity_uuid = None
        if entity_id:
            resolved = resolve_entity(cur, entity_id)
            if resolved is None:
                raise ValueError(f"Unknown entity: {entity_id!r}")
            entity_uuid = resolved[0]

        sql = (
            "SELECT d.document_id, d.title, d.doc_type, d.source_system, d.sensitivity_level, "
            "d.created_at, e.slug AS entity_slug, e.name AS entity_name, (d.entity_id IS NULL) AS is_global "
            "FROM documents d LEFT JOIN entities e ON e.entity_id = d.entity_id "
            "WHERE d.status = 'active'"
        )
        params: list[Any] = []
        if entity_uuid:
            sql += " AND (d.entity_id = %s OR d.entity_id IS NULL)"
            params.append(entity_uuid)
        if source_system:
            sql += " AND d.source_system = %s"
            params.append(source_system)
        sql += " ORDER BY d.created_at DESC;"
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "document_id": str(r[0]), "title": r[1], "doc_type": r[2],
            "source_system": r[3], "sensitivity_level": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
            "entity_slug": r[6], "entity_name": r[7], "is_global": bool(r[8]),
        }
        for r in rows
    ]


def get_document(document_id: str) -> dict[str, Any] | None:
    """Full single-row fetch, including body_text — for loading a document into
    an editor. `list_documents()` deliberately omits body_text to stay light."""
    if not _is_uuid(document_id):
        return None
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT document_id, entity_id, title, doc_type, source_system, "
            "sensitivity_level, body_text, created_at, updated_at "
            "FROM documents WHERE document_id = %s AND status = 'active';",
            (document_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "document_id": str(row[0]), "entity_id": str(row[1]) if row[1] else None, "title": row[2],
        "doc_type": row[3], "source_system": row[4], "sensitivity_level": row[5],
        "body_text": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
        "updated_at": row[8].isoformat() if row[8] else None,
    }


_UPDATABLE_DOCUMENT_FIELDS = {"title", "sensitivity_level", "body_text"}


def update_document_row(document_id: str, **fields: Any) -> bool:
    """Allow-listed partial update (title / sensitivity_level / body_text).
    Returns True if a row was found and updated."""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE_DOCUMENT_FIELDS and v is not None}
    if not updates:
        return False
    cols = ", ".join(f"{k} = %s" for k in updates)
    with cursor() as cur:
        cur.execute(
            f"UPDATE documents SET {cols} WHERE document_id = %s;",
            [*updates.values(), document_id],
        )
        return cur.rowcount > 0


def delete_chunks(document_id: str) -> int:
    """Remove all document_chunks rows for a document (used before re-chunking
    on a content edit — distinct from delete_document, which removes the whole
    document)."""
    with cursor() as cur:
        cur.execute("DELETE FROM document_chunks WHERE document_id = %s;", (document_id,))
        return cur.rowcount


def delete_document(document_id: str) -> dict[str, Any] | None:
    """Hard-delete a document (document_chunks cascade). Returns
    {"s3_uri": <uri or None>} if a row was found and deleted, else None."""
    if not _is_uuid(document_id):
        return None
    with cursor() as cur:
        cur.execute("DELETE FROM documents WHERE document_id = %s RETURNING s3_uri;", (document_id,))
        row = cur.fetchone()
    return {"s3_uri": row[0]} if row is not None else None


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
