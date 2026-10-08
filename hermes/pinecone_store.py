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

from . import health
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


# Guardrail #2 (business data isolation): metadata keys the ingestion-service may
# use to stamp which business a vector belongs to. We check all of them so the
# verification is robust to the exact key/format the writer chose (docs say
# `entity_id` = the entity UUID).
_BUSINESS_META_KEYS = ("entity_id", "entity_slug", "business", "entity", "tenant")


def _business_ok(
    md: dict[str, Any], expected_id: str | None, expected_slug: str | None
) -> bool:
    """Whether a match's metadata is consistent with the requested business.

    Returns True if the match carries no readable business tag (the namespace
    already isolated it) OR any readable tag matches the expected UUID/slug.
    Returns False only when a business tag is present AND none of them match —
    i.e. a genuine cross-business result that must be dropped.
    """
    expected = {str(v).lower() for v in (expected_id, expected_slug) if v}
    if not expected:
        return True
    found = [str(md[k]).lower() for k in _BUSINESS_META_KEYS if md.get(k) not in (None, "")]
    if not found:
        return True  # untagged vector — namespace is the guard, keep it
    return any(f in expected for f in found)


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
    verify_entity_id: str | None = None,
    verify_entity_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Meaning search within ONE business's Pinecone namespace, with the same
    guards the pgvector reader applies. Returns [] on any failure (disabled,
    missing key, embed error, Pinecone error) — retrieval must never raise into
    a chat turn.

    Pass a precomputed `query_vector` (1536-dim, text-embedding-3-small) to skip
    the embedding call — the federated reader shares one embedding across this
    and the pgvector query.

    Guardrail #2: when `verify_entity_id`/`verify_entity_slug` are given (the
    business we intend to serve), each returned match is checked against its
    metadata and any match belonging to a DIFFERENT business is dropped and logged
    — defense-in-depth over the namespace so a wrong namespace can't leak.
    """
    if not Config.rag_pinecone_enabled():
        return []

    # Never query without a real namespace — an empty slug would build the
    # `entity__` catch-all and could scan across businesses.
    if not (entity_slug or "").strip():
        log.warning("Pinecone query skipped: empty entity_slug (no namespace)")
        return []

    try:
        if query_vector is None:
            if not (query_text or "").strip():
                return []
            query_vector = embed([query_text], task="query")[0]
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
    except Exception as exc:  # noqa: BLE001 — Pinecone unreachable/misconfigured => empty, not fatal
        # Recorded, not just logged: this returns [] on a missing `pinecone`
        # package or a bad key, which silently removes every one of the ~56k
        # indexed vectors from retrieval while chat carries on looking fine.
        health.record("pinecone", "failed", exc)
        log.warning("Pinecone query failed for %s", entity_slug, exc_info=True)
        return []

    verify = bool(verify_entity_id or verify_entity_slug)
    out: list[dict[str, Any]] = []
    dropped = 0
    sample_bad: str | None = None
    for m in res.get("matches", []) or []:
        md = m.get("metadata") or {}
        if verify and not _business_ok(md, verify_entity_id, verify_entity_slug):
            dropped += 1
            if sample_bad is None:
                sample_bad = next(
                    (str(md[k]) for k in _BUSINESS_META_KEYS if md.get(k) not in (None, "")),
                    "?",
                )
            continue
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
    if dropped:
        # A caught cross-business leak — loud on purpose so it's auditable.
        log.error(
            "business_isolation: dropped %d Pinecone match(es) not belonging to "
            "namespace %s (expected entity_id=%s / slug=%s, sample foreign id=%s)",
            dropped, entity_namespace(entity_slug), verify_entity_id, verify_entity_slug, sample_bad,
        )
    health.record("pinecone", "ok" if out else "empty")
    return out
