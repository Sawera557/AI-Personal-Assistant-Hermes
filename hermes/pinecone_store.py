"""Pinecone — the search brain for Hermes.

Holds a *search copy* of text so the AI can find things by meaning. It never
holds real files (those live in S3) and never the source of truth (that's
Postgres). Only the backend talks to Pinecone, and only with the filters below.

Layout (per the project scope doc):
  * One serverless index (aws, us-east-1), cosine, 1536 dims.
  * Embeddings: OpenAI text-embedding-3-small.
  * One namespace per business:  entity__<slug>   (e.g. entity__gak_properties)
  * One private namespace per user: user__<user_id>  (private chat memory,
    never shared) — kept separate from entity__personal on purpose.
  * Sensitivity + region are enforced as METADATA FILTERS, not separate indexes.

Every query MUST go through ``query()`` so the role/sensitivity/region/is_active
filters and the namespace boundary are always applied — that is the guard.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import requests

# Sensitivity ladder (low -> high). A viewer may see their level and below.
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]

INDEX_NAME = os.getenv("PINECONE_INDEX", "hermes")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
CLOUD = os.getenv("PINECONE_CLOUD", "aws")
REGION = os.getenv("PINECONE_REGION", "us-east-1")


# ---- Namespaces -----------------------------------------------------------
def entity_namespace(slug: str) -> str:
    """Namespace for a business's shared data."""
    return f"entity__{slug}"


def user_namespace(user_id: str) -> str:
    """Namespace for a user's PRIVATE chat memory (never shared)."""
    return f"user__{user_id}"


def _allowed_sensitivities(max_level: str) -> list[str]:
    """Levels at or below the viewer's clearance."""
    if max_level not in SENSITIVITY_ORDER:
        return ["public"]
    cutoff = SENSITIVITY_ORDER.index(max_level)
    return SENSITIVITY_ORDER[: cutoff + 1]


# ---- Clients (lazy so importing this module never needs the keys) ----------
_pc = None


def _client():
    global _pc
    if _pc is None:
        from pinecone import Pinecone  # lazy import

        key = os.getenv("PINECONE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("PINECONE_API_KEY is not set.")
        _pc = Pinecone(api_key=key)
    return _pc


def index():
    return _client().Index(INDEX_NAME)


def ensure_index() -> dict[str, Any]:
    """Create the serverless index if it doesn't exist. Idempotent."""
    from pinecone import ServerlessSpec

    pc = _client()
    existing = {i["name"] for i in pc.list_indexes()}
    if INDEX_NAME not in existing:
        pc.create_index(
            name=INDEX_NAME,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud=CLOUD, region=REGION),
        )
    return pc.describe_index(INDEX_NAME).to_dict()


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
    """Embed and upsert chunks into one namespace.

    Each chunk: {id, text, entity_id, document_id, chunk_id, sensitivity_level,
    region, source_system, version}. `id` becomes document_chunks.pinecone_vector_id.
    """
    chunks = list(chunks)
    if not chunks:
        return 0
    vectors = embed([c["text"] for c in chunks])
    payload = []
    for c, vec in zip(chunks, vectors):
        meta = {
            "entity_id": c.get("entity_id"),
            "document_id": c.get("document_id"),
            "chunk_id": c.get("chunk_id"),
            "sensitivity_level": c.get("sensitivity_level", "internal"),
            "region": c.get("region", "us-east-1"),
            "source_system": c.get("source_system"),
            "version": c.get("version", 1),
            "is_active": c.get("is_active", True),
            "text": c["text"][:2000],  # small preview for source display
        }
        payload.append({
            "id": c["id"],
            "values": vec,
            "metadata": {k: v for k, v in meta.items() if v is not None},
        })
    index().upsert(vectors=payload, namespace=namespace)
    return len(payload)


# ---- Read (the guarded query) ---------------------------------------------
def query(
    *,
    namespace: str,
    query_text: str,
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
    """
    flt: dict[str, Any] = {
        "is_active": {"$eq": True},
        "sensitivity_level": {"$in": _allowed_sensitivities(max_sensitivity)},
    }
    if region:
        flt["region"] = {"$eq": region}
    if extra_filter:
        flt.update(extra_filter)

    vec = embed([query_text])[0]
    res = index().query(
        namespace=namespace,
        vector=vec,
        top_k=top_k,
        include_metadata=True,
        filter=flt,
    )
    out = []
    for m in res.get("matches", []):
        md = m.get("metadata", {})
        out.append({
            "vector_id": m.get("id"),
            "score": m.get("score"),
            "document_id": md.get("document_id"),
            "chunk_id": md.get("chunk_id"),
            "sensitivity_level": md.get("sensitivity_level"),
            "text": md.get("text"),
        })
    return out
