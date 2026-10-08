"""Federated business-knowledge retrieval + the always-on RETRIEVED CONTEXT block.

One business's knowledge lives in two vector stores that were built separately:

  1. pgvector  `document_chunks` (hermes/search.py / vector_store.py) — the
     hand-added Context-tab notes and files the owner uploads in the app.
  2. Pinecone  `entity__<slug>`  (hermes/pinecone_store.py) — the autopilot
     corpus the ingestion-service embeds (emails, Drive files, meeting notes).

Both use the same embedding space (text-embedding-3-small / 1536) and the same
sensitivity metadata, so this module queries BOTH with a single embedding of the
user's message, merges and de-duplicates the hits, drops anything below a
relevance threshold, ranks by score, and joins back to real documents.

Two entry points:
  * federated_search(...)    -> ranked hits (the `search_business_knowledge` tool)
  * build_context_block(...) -> the formatted block injected into the system
                                prompt every turn (always-on RAG)

Guarantees: per-business isolation (one namespace per store, never cross-business);
'restricted' never surfaces (enforced in each reader); and every failure degrades
to [] / "" — retrieval must never break a chat turn.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import guardrails, pinecone_store, vector_store
from .businesses import to_business_slug, to_entity_slug
from .config import Config
from .db import documents as docs_repo
from .db.connection import cursor
from .vector_store import embed

log = logging.getLogger("hermes.rag")

# Per-snippet cap inside the injected block — enough to be useful evidence,
# short enough that RAG_TOP_K of them stay well under RAG_CONTEXT_MAX_CHARS.
_SNIPPET_CHARS = 320


def _dedupe_key(hit: dict[str, Any]) -> str:
    """A chunk from pgvector and the same chunk from Pinecone must collapse to
    one entry. chunk_id is the stable identity; fall back to document + snippet
    head when a store omits it."""
    cid = hit.get("chunk_id")
    if cid:
        return f"chunk:{cid}"
    return f"doc:{hit.get('document_id')}::{(hit.get('text') or '')[:120]}"


def federated_search(
    entity_slug: str,
    query: str,
    *,
    query_vector: list[float] | None = None,
    top_k: int | None = None,
    max_sensitivity: str | None = None,
    min_score: float | None = None,
    presign: bool = True,
) -> list[dict[str, Any]]:
    """Search one business's knowledge across pgvector + Pinecone and return a
    merged, ranked, de-duplicated hit list.

    `entity_slug` is an ENTITIES slug (e.g. "afro_farm"), as elsewhere in
    hermes.search. Raises ValueError for an unknown entity (the caller turns it
    into a tool error); returns [] for an empty query or when nothing clears the
    threshold. Never raises for a store being down — a dead backend contributes
    zero hits, the other still answers.
    """
    top_k = top_k or Config.RAG_TOP_K
    max_sensitivity = max_sensitivity or Config.RAG_MAX_SENSITIVITY
    text = (query or "").strip()
    if not text and query_vector is None:
        return []

    with cursor(commit=False) as cur:
        resolved = docs_repo.resolve_entity(cur, entity_slug)
    if resolved is None:
        raise ValueError(f"Unknown entity: {entity_slug!r}")
    _entity_uuid, slug, region = resolved

    # Guardrail #2 (business data isolation): pass the intended business's identity
    # down so the Pinecone reader can drop any match belonging to another business.
    _enforce_isolation = guardrails.business_isolation_enabled(to_business_slug(slug))

    if query_vector is None:
        try:
            query_vector = embed([text], task="query")[0]
        except Exception:  # noqa: BLE001 — no embedding => no retrieval this turn
            log.warning("federated_search: embedding failed", exc_info=True)
            return []

    def _pg() -> list[dict[str, Any]]:
        try:
            hits = vector_store.query(
                namespace=vector_store.entity_namespace(slug),
                query_vector=query_vector,
                max_sensitivity=max_sensitivity,
                region=region,
                top_k=top_k,
            )
        except Exception:  # noqa: BLE001
            log.warning("federated_search: pgvector query failed", exc_info=True)
            return []
        for h in hits:
            h.setdefault("origin", "pgvector")
        return hits

    def _pc() -> list[dict[str, Any]]:
        # pinecone_store.query is already fully fail-open (returns [] when RAG is
        # off, key missing, or Pinecone errors).
        return pinecone_store.query(
            entity_slug=slug,
            query_vector=query_vector,
            region=region,
            top_k=top_k,
            max_sensitivity=max_sensitivity,
            verify_entity_id=(_entity_uuid if _enforce_isolation else None),
            verify_entity_slug=(slug if _enforce_isolation else None),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_pg = pool.submit(_pg)
        f_pc = pool.submit(_pc)
        pg_hits, pc_hits = f_pg.result(), f_pc.result()

    merged: dict[str, dict[str, Any]] = {}
    for h in (*pg_hits, *pc_hits):
        key = _dedupe_key(h)
        best = merged.get(key)
        if best is None or (h.get("score") or 0.0) > (best.get("score") or 0.0):
            merged[key] = h
    hits = list(merged.values())

    if min_score is not None:
        hits = [h for h in hits if (h.get("score") or 0.0) >= min_score]
    hits.sort(key=lambda h: h.get("score") or 0.0, reverse=True)
    hits = hits[:top_k]
    if not hits:
        return []

    doc_ids = [h.get("document_id") for h in hits if h.get("document_id")]
    meta = docs_repo.documents_by_id(doc_ids) if doc_ids else {}
    out: list[dict[str, Any]] = []
    for h in hits:
        doc = meta.get(h.get("document_id"), {}) or {}
        link = None
        if presign and doc.get("s3_uri"):
            try:
                from . import storage

                link = storage.presign(doc["s3_uri"])
            except Exception:  # noqa: BLE001 — a link is a bonus, never block a hit on it
                link = None
        out.append(
            {
                "score": h.get("score"),
                "snippet": h.get("text"),
                "sensitivity_level": h.get("sensitivity_level"),
                "document_id": h.get("document_id"),
                "chunk_id": h.get("chunk_id"),
                "title": doc.get("title"),
                "doc_type": doc.get("doc_type"),
                "source_system": h.get("source_system") or doc.get("source_system"),
                "download_url": link,
                "origin": h.get("origin"),
            }
        )
    return out


def _format_block(business: str | None, hits: list[dict[str, Any]]) -> str:
    who = f"'{business}'" if business else "this business"
    header = (
        f"RETRIEVED CONTEXT (relevant snippets pulled this turn from {who}'s "
        "knowledge base — treat these as retrieved evidence, and attribute them to "
        "the document/source, NEVER as something the user told you or as a memory "
        "of a past conversation. Do NOT assert any detail not listed here, and do "
        "not fill in specifics beyond these snippets). Each line is "
        "[title | source | relevance]:"
    )
    lines = [header]
    for h in hits:
        title = (h.get("title") or "untitled").strip()
        src = h.get("source_system") or h.get("origin") or "kb"
        score = h.get("score")
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        snippet = " ".join((h.get("snippet") or "").split())[:_SNIPPET_CHARS]
        lines.append(f"  - [{title} | {src} | {score_s}] {snippet}")
    block = "\n".join(lines)
    cap = Config.RAG_CONTEXT_MAX_CHARS
    if len(block) > cap:
        cut = block[:cap]
        nl = cut.rfind("\n")
        block = (cut[:nl] if nl > 0 else cut).rstrip() + "\n  ...(truncated)"
    return block


def build_context_block(*, active_business: str | None, latest_user_text: str) -> str:
    """The always-on RAG block for the system prompt. Empty string when: the
    feature is off, no single business is in scope ("All Businesses"), the query
    is blank, or nothing clears RAG_SCORE_THRESHOLD — so a small-talk turn adds
    zero tokens. Fail-open: any error yields "" and the turn proceeds without it.
    """
    if not Config.rag_context_injection():
        return ""
    text = (latest_user_text or "").strip()
    if not text:
        return ""
    # One namespace per query: "All Businesses" mode has no single business to
    # scope to, so (like memory retrieval) the always-on block is skipped there.
    if not active_business or active_business == "all":
        return ""
    entity_slug = to_entity_slug(active_business)
    if not entity_slug:
        return ""

    try:
        hits = federated_search(
            entity_slug,
            text,
            top_k=Config.RAG_TOP_K,
            max_sensitivity=Config.RAG_MAX_SENSITIVITY,
            min_score=Config.RAG_SCORE_THRESHOLD,
            presign=False,  # the injected block cites titles/sources, not S3 links
        )
    except Exception:  # noqa: BLE001 — retrieval must never break the turn
        log.warning("build_context_block failed for %s", active_business, exc_info=True)
        return ""

    if not hits:
        return ""
    return _format_block(active_business, hits)
