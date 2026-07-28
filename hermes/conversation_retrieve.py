"""Conversation-history recall — the always-on EARLIER CONVERSATIONS block.

Owner-scoped semantic recall over past turns + summaries (hermes/db/
conversation_memory.py), formatted PROVENANCE-FIRST: every line carries the date
(and business) of the exchange, so the model can cite "on 2026-07-12 you said …".

Zero-hallucination stance is enforced in the block header itself: this is
presented as the model's ONLY memory of prior conversations, it must cite the
date when it uses one, and if something isn't here it must say so rather than
guess. A relevance threshold means a small-talk turn recalls nothing (empty
block, zero tokens) instead of injecting weak matches that invite invention.

Fail-open: any error yields "" and the turn proceeds. Gated by
HERMES_CONVO_MEMORY (default OFF) so it merges dark and never disturbs Phase 1.
"""
from __future__ import annotations

import logging

from .config import Config
from .db import conversation_memory as repo
from .vector_store import embed

log = logging.getLogger("hermes.conversation_retrieve")

_SNIPPET_CHARS = 320


def _fmt_date(occurred_at) -> str:
    try:
        return occurred_at.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — a missing/odd timestamp must not break formatting
        return "undated"


def _format_block(hits: list[dict]) -> str:
    header = (
        "EARLIER CONVERSATIONS (verbatim excerpts from your past chats with the "
        "user, most relevant first — this is your ONLY record of prior "
        "conversations. When you use one, cite its date. State ONLY what these "
        "excerpts actually contain — do not add names, amounts, dates, or details "
        "that are not written here, and do not attribute anything to the user "
        "unless the excerpt shows the user saying it. If a line is tagged with a "
        "different business than the current one, it belongs to that business. If "
        "something is not listed here, say you don't have it in memory rather than "
        "guessing). Each line is [date | business | kind]:"
    )
    lines = [header]
    for h in hits:
        date = _fmt_date(h.get("occurred_at"))
        biz = h.get("business_slug") or "—"
        kind = h.get("kind") or "turn"
        snippet = " ".join((h.get("text") or "").split())[:_SNIPPET_CHARS]
        lines.append(f"  - [{date} | {biz} | {kind}] {snippet}")
    block = "\n".join(lines)
    cap = Config.CONVO_MEMORY_MAX_CHARS
    if len(block) > cap:
        cut = block[:cap]
        nl = cut.rfind("\n")
        block = (cut[:nl] if nl > 0 else cut).rstrip() + "\n  ...(truncated)"
    return block


def build_history_block(
    *,
    user_id: str | None,
    latest_user_text: str,
    active_business: str | None = None,
    query_vector: list[float] | None = None,
) -> str:
    """The always-on recall block. Empty string when the feature is off, there's
    no owner to scope to, the query is blank, or nothing clears the threshold —
    so a chit-chat turn adds zero tokens. Pass a precomputed `query_vector` to
    reuse the turn's single embedding.
    """
    if not Config.convo_memory_enabled():
        return ""
    text = (latest_user_text or "").strip()
    if not text or not user_id:
        return ""

    try:
        vec = query_vector if query_vector is not None else embed([text])[0]
    except Exception:  # noqa: BLE001 — no embedding => no recall this turn
        log.warning("conversation recall: embedding failed", exc_info=True)
        return ""

    try:
        hits = repo.semantic_search(
            vec,
            user_id=user_id,
            top_k=Config.CONVO_MEMORY_TOP_K,
            business_slug=None,  # recall across ALL the owner's conversations
        )
    except Exception:  # noqa: BLE001 — recall must never break the turn
        log.warning("conversation recall failed", exc_info=True)
        return ""

    hits = [h for h in hits if (h.get("score") or 0.0) >= Config.CONVO_MEMORY_SCORE_THRESHOLD]
    if not hits:
        return ""
    return _format_block(hits)
