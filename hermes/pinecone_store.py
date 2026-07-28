"""Read-only Pinecone reader — the missing bridge to the autopilot corpus.

The ingestion-service (a separate deployable) embeds each business's emails,
Drive files and meeting notes into Pinecone — index `hermes`, one namespace per
business (`entity__<slug>`), 1536-dim text-embedding-3-small, tagged with the
same `is_active` / `sensitivity_level` metadata the pgvector reader enforces.
Those vectors are never written back into Postgres `document_chunks.embedding`,
so hermes/vector_store.py (which filters `embedding IS NOT NULL`) can't see the
~15k ingested documents. This module is that missing read path.

Contract, deliberately kept identical to the pgvector reader
(hermes/vector_store.query):
  * one namespace per call (`entity__<slug>`) — no cross-business search;
  * mandatory guards applied server-side: is_active AND sensitivity <= clearance;
  * 'restricted' is never returned regardless of the requested clearance;
  * returns {vector_id, score, document_id, chunk_id, sensitivity_level,
    source_system, text} — the same keys the pgvector reader returns, so
    hermes/rag.py can merge the two result sets without special-casing.

Read-only by design: the ingestion-service stays the SOLE writer to Pinecone.
Every failure path degrades to [] (Pinecone unreachable, missing key, SDK error)
so a retrieval hiccup can never break a chat turn — it just looks like "no
external match this turn", the same shape as an empty result.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .vector_store import SENSITIVITY_ORDER, embed

log = logging.getLogger("hermes.pinecone_store")

# 'restricted' material is local-only by rule — this reader hard-caps served
# sensitivity here so a caller asking for it still never receives it.
_MAX_SERVED_SENSITIVITY = "confidential"


def entity_namespace(slug: str) -> str:
    """Namespace for a business's shared data — identical convention to the
    ingestion-service writer and hermes/vector_store.py."""
    return f"entity__{slug}"


def _allowed_sensitivities(max_level: str) -> list[str]:
    """Levels at or below the viewer's clearance, capped at 'confidential'."""
    if max_level not in SENSITIVITY_ORDER:
        max_level = "public"
    cap = SENSITIVITY_ORDER.index(_MAX_SERVED_SENSITIVITY)
    cutoff = min(SENSITIVITY_ORDER.index(max_level), cap)
    return SENSITIVITY_ORDER[: cutoff + 1]


# ---- Client (lazy, cached) -------------------------------------------------
_pc = None


def _index():
    """The Pinecone index handle. Lazily constructed so importing this module
    (and the whole agent) costs nothing when RAG-on-Pinecone is off, and so a
    missing SDK/key only bites the caller that actually queries."""
    global _pc
    if _pc is None:
        from pinecone import Pinecone  # lazy: keeps cold-start light when RAG is off

        if not Config.PINECONE_API_KEY:
            raise RuntimeError("PINECONE_API_KEY is not set.")
        _pc = Pinecone(api_key=Config.PINECONE_API_KEY)
    return _pc.Index(Config.PINECONE_INDEX)


def query(
    *,
    entity_slug: str,
    query_text: str | None = None,
    query_vector: list[float] | None = None,
    region: str | None = None,
    top_k: int = 6,
    max_sensitivity: str = "confidential",
) -> list[dict[str, Any]]:
    """Meaning search within ONE business's Pinecone namespace, with the same
    guards the pgvector reader applies. Returns [] on any failure (disabled,
    missing key, embed error, Pinecone error) — retrieval must never raise into
    a chat turn.

    Pass a precomputed `query_vector` (1536-dim, text-embedding-3-small) to skip
    the embedding call — the federated reader shares one embedding across this
    and the pgvector query.
    """
    if not Config.rag_pinecone_enabled():
        return []

    try:
        if query_vector is None:
            if not (query_text or "").strip():
                return []
            query_vector = embed([query_text])[0]
    except Exception:  # noqa: BLE001 — no embedding, no external matches this turn
        log.warning("Pinecone query skipped: embedding failed", exc_info=True)
        return []

    flt: dict[str, Any] = {
        "is_active": {"$eq": True},
        "sensitivity_level": {"$in": _allowed_sensitivities(max_sensitivity)},
    }
    if region:
        flt["region"] = {"$eq": region}

    try:
        res = _index().query(
            namespace=entity_namespace(entity_slug),
            vector=query_vector,
            top_k=top_k,
            include_metadata=True,
            filter=flt,
        )
    except Exception:  # noqa: BLE001 — Pinecone unreachable/misconfigured => empty, not fatal
        log.warning("Pinecone query failed for %s", entity_slug, exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for m in res.get("matches", []) or []:
        md = m.get("metadata") or {}
        out.append(
            {
                "vector_id": m.get("id"),
                "score": m.get("score"),
                "document_id": md.get("document_id"),
                "chunk_id": md.get("chunk_id"),
                "sensitivity_level": md.get("sensitivity_level"),
                "source_system": md.get("source_system"),
                "text": md.get("text"),
                "origin": "pinecone",
            }
        )
    return out
