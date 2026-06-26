"""Meaning search — the guarded entry point the backend uses.

The website and the agent NEVER talk to Pinecone directly. They call this, which:
  1. resolves which business (entity) is in scope and its region,
  2. searches only that business's namespace,
  3. enforces sensitivity (<= the caller's clearance) + region + is_active,
  4. joins hits back to real documents (title, presigned S3 link).

No cross-business search here by design — to search several businesses (Owner
"All Businesses" mode), call this once per permitted entity and merge.
"""
from __future__ import annotations

from typing import Any

from . import pinecone_store as ps
from .db import documents as docs_repo
from .db.connection import cursor


def semantic_search(
    entity_id: str,
    query: str,
    *,
    max_sensitivity: str = "internal",
    top_k: int = 8,
    presign: bool = True,
) -> list[dict[str, Any]]:
    if not (query or "").strip():
        return []

    with cursor(commit=False) as cur:
        resolved = docs_repo.resolve_entity(cur, entity_id)
    if resolved is None:
        raise ValueError(f"Unknown entity: {entity_id!r}")
    _entity_uuid, slug, region = resolved

    matches = ps.query(
        namespace=ps.entity_namespace(slug),
        query_text=query,
        max_sensitivity=max_sensitivity,
        region=region,
        top_k=top_k,
    )
    if not matches:
        return []

    meta = docs_repo.documents_by_id([m.get("document_id") for m in matches])
    out: list[dict[str, Any]] = []
    for m in matches:
        doc = meta.get(m.get("document_id"), {})
        link = None
        if presign and doc.get("s3_uri"):
            from . import storage

            link = storage.presign(doc["s3_uri"])
        out.append({
            "score": m.get("score"),
            "snippet": m.get("text"),
            "sensitivity_level": m.get("sensitivity_level"),
            "document_id": m.get("document_id"),
            "title": doc.get("title"),
            "doc_type": doc.get("doc_type"),
            "source_system": doc.get("source_system"),
            "download_url": link,
        })
    return out
