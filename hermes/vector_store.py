"""Vector store — the search brain for Hermes, backed by pgvector on the same
Postgres instance as everything else (not Pinecone, not any external vector
service). One storage stack: Postgres is truth for both the record and the
search copy; S3 still holds the original file bytes.

Layout (unchanged from the original Pinecone-backed design, per the project
scope doc):
  * One namespace per business:  entity__<slug>  (e.g. entity__gak_properties)
  * One private namespace per user: user__<user_id>  (private chat memory,
    never shared) — kept separate from entity__personal on purpose. Never
    populated to date; parsed here as a no-op returning no results, same
    behavior as before this module existed.
  * One GLOBAL namespace (see global_namespace()): documents with no single
    owning business (entity_id IS NULL) — e.g. "for all businesses" context.
    Searching any specific business's namespace automatically ALSO matches
    global documents, so a global note surfaces everywhere without being
    duplicated per business.
  * Sensitivity + region are enforced as SQL WHERE filters, not separate
    tables/indexes — same idea as the old metadata-filter approach, just one
    round trip instead of two systems.

Every query MUST go through ``query()`` so the role/sensitivity/region/is_active
filters and the namespace boundary are always applied — that is the guard.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import requests
from pgvector import Vector

from .db.connection import cursor

# Sensitivity ladder (low -> high). A viewer may see their level and below.
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))


# ---- Namespaces -----------------------------------------------------------
def entity_namespace(slug: str) -> str:
    """Namespace for a business's shared data."""
    return f"entity__{slug}"


def user_namespace(user_id: str) -> str:
    """Namespace for a user's PRIVATE chat memory (never shared)."""
    return f"user__{user_id}"


_GLOBAL_NAMESPACE = "__global__"


def global_namespace() -> str:
    """Namespace for documents that apply to every business (no single owner)."""
    return _GLOBAL_NAMESPACE


def _entity_slug_of(namespace: str) -> str | None:
    """Pull the entity slug back out of an `entity__<slug>` namespace, or None
    for a `user__<id>` namespace (never populated — see module docstring)."""
    if namespace.startswith("entity__"):
        return namespace[len("entity__") :]
    return None


def _allowed_sensitivities(max_level: str) -> list[str]:
    """Levels at or below the viewer's clearance."""
    if max_level not in SENSITIVITY_ORDER:
        return ["public"]
    cutoff = SENSITIVITY_ORDER.index(max_level)
    return SENSITIVITY_ORDER[: cutoff + 1]


# ---- Embeddings (OpenAI) ---------------------------------------------------
def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with OpenAI. Returns one vector per input."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (needed for embeddings).")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    resp = requests.post(
        f"{base}/embeddings",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [row["embedding"] for row in sorted(data, key=lambda d: d["index"])]


# ---- Write ----------------------------------------------------------------
def upsert_chunks(namespace: str, chunks: Iterable[dict[str, Any]]) -> int:
    """Embed and write chunks' vectors into their document_chunks rows.

    Each chunk: {id, text, entity_id, document_id, chunk_id, sensitivity_level,
    region, source_system, version}. `id` is the chunk's pinecone_vector_id —
    the document_chunks row for it must already exist (ingest.py creates the
    row before calling this) since the write below is an UPDATE keyed on it.
    `namespace` is accepted for call-signature compatibility but unused here:
    each chunk already carries its own entity_id.
    """
    chunks = list(chunks)
    if not chunks:
        return 0
    vectors = embed([c["text"] for c in chunks])
    rows = [
        {
            "vector_id": c["id"],
            "embedding": Vector(vec),  # bare list[float] is ambiguous to psycopg for `vector`
            "embed_model": EMBED_MODEL,
            "text_preview": c["text"][:2000],
        }
        for c, vec in zip(chunks, vectors)
    ]
    with cursor() as cur:
        cur.executemany(
            """
            UPDATE document_chunks
            SET embedding = %(embedding)s, embed_model = %(embed_model)s,
                text_preview = %(text_preview)s
            WHERE pinecone_vector_id = %(vector_id)s;
            """,
            rows,
        )
    return len(rows)


# ---- Read (the guarded query) ---------------------------------------------
def query(
    *,
    namespace: str,
    query_text: str | None = None,
    query_vector: list[float] | None = None,
    max_sensitivity: str = "internal",
    region: str | None = None,
    top_k: int = 8,
    extra_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Meaning search within ONE namespace, with the mandatory filters applied.

    The caller (backend) must first verify who the user is and that they may see
    `namespace`. This function then enforces sensitivity (<= clearance), region,
    and is_active. There is no cross-business search here by design — search each
    permitted namespace separately and merge.

    A business namespace ALSO matches global (all-businesses) documents —
    those have no entity_id to check a region against, so the region filter
    only applies to that business's own chunks, never to global ones.

    Pass a precomputed `query_vector` to skip the embedding call — used by the
    federated reader (hermes/rag.py) so one embedding of the user's message
    serves both this pgvector query and the Pinecone query. Otherwise pass
    `query_text` and it is embedded here.
    """
    if query_vector is None:
        if not (query_text or "").strip():
            return []
        query_vector = embed([query_text])[0]
    allowed = _allowed_sensitivities(max_sensitivity)
    params: dict[str, Any] = {
        "allowed": allowed,
        "top_k": top_k,
        "qvec": Vector(query_vector),
    }

    if namespace == _GLOBAL_NAMESPACE:
        scope_sql = "dc.entity_id IS NULL"
    else:
        slug = _entity_slug_of(namespace)
        if slug is None:
            return []  # user__<id> namespace: never populated, same as before.
        params["slug"] = slug
        if region:
            params["region"] = region
            scope_sql = "(e.slug = %(slug)s AND dc.region = %(region)s) OR dc.entity_id IS NULL"
        else:
            scope_sql = "e.slug = %(slug)s OR dc.entity_id IS NULL"

    where = [
        f"({scope_sql})",
        "dc.is_active",
        "dc.sensitivity_level = ANY(%(allowed)s)",
        "dc.embedding IS NOT NULL",
    ]
    if extra_filter:
        # Only document_id is used as an extra filter anywhere in this codebase today.
        if "document_id" in extra_filter:
            where.append("dc.document_id = %(document_id)s")
            params["document_id"] = extra_filter["document_id"]

    sql = f"""
        SELECT dc.chunk_id, dc.document_id, dc.pinecone_vector_id,
               dc.sensitivity_level, 1 - (dc.embedding <=> %(qvec)s) AS score,
               dc.text_preview
        FROM document_chunks dc
        LEFT JOIN entities e ON e.entity_id = dc.entity_id
        WHERE {' AND '.join(where)}
        ORDER BY dc.embedding <=> %(qvec)s
        LIMIT %(top_k)s;
    """
    with cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {
            "vector_id": r[2],
            "score": r[4],
            "document_id": str(r[1]),
            "chunk_id": str(r[0]),
            "sensitivity_level": r[3],
            "text": r[5],
        }
        for r in rows
    ]
