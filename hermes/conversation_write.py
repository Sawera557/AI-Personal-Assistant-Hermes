"""Conversation-history WRITER — records each finished turn into conversation_memory.

Mirrors hermes/memory_extract exactly: fire-and-forget, zero latency to the
reply, background daemon thread, broad try/except so a write failure never
touches the conversation. Triggered from app.py right after record_turn(),
alongside extract_async().

Two writes per turn, both gated on Config.convo_memory_write_enabled():
  * verbatim 'turn'    — the exact user+assistant exchange. This is the CITABLE
                          evidence recall injects; it is never model-generated.
  * extractive 'summary' — a constrained, EXTRACTIVE 1-2 sentence digest to help
                          locate the conversation later. Navigational ONLY: it
                          may not introduce any fact not literally in the turn,
                          and it is never treated as a source. Skipped safely on
                          any error, so the verbatim turn always lands.
"""
from __future__ import annotations

import logging
import threading

from .config import Config
from .db import conversation_memory as repo
from .llm_json import call_claude_json
from .vector_store import EMBED_MODEL, embed

log = logging.getLogger("hermes.conversation_write")

# Zero-hallucination summariser: extractive, no inference, empty when nothing
# substantive. Kept deliberately strict — summaries are navigational, and the
# verbatim turn is what actually gets cited.
_SUMMARY_SYSTEM = (
    "You are Hermes's conversation summariser — a silent background step, never "
    "seen by the user. Produce a SHORT, PURELY EXTRACTIVE note of what this single "
    "exchange was about, to help locate it later.\n\n"
    "STRICT RULES (zero hallucination):\n"
    "- Include ONLY facts, decisions, or requests EXPLICITLY stated in the exchange.\n"
    "- NEVER add, infer, assume, generalise, or introduce numbers/names/details not "
    "literally present in the text.\n"
    "- If nothing substantive was discussed (small talk, a pure task with no durable "
    "point), return an empty summary.\n"
    "- 1-2 short sentences maximum.\n\n"
    'Respond with ONLY a JSON object: {"summary": "<extractive note, or empty string>"}'
)


def _make_summary(user_text: str, reply: str) -> str:
    """Constrained extractive summary, or "" on anything unexpected."""
    try:
        parsed = call_claude_json(
            system=_SUMMARY_SYSTEM,
            user_message=f"User: {user_text}\n\nHermes: {reply}",
            max_tokens=200,
            fallback_key="_raw",
            model=Config.GUIDANCE_MODEL,
        )
        if isinstance(parsed, dict):
            return (parsed.get("summary") or "").strip()
    except Exception:  # noqa: BLE001 — no summary this turn; the verbatim turn still lands
        log.warning("conversation summary failed", exc_info=True)
    return ""


def record_turn_async(
    *, session_id: str | None, active_business: str | None, user_text: str, reply: str,
) -> None:
    """Fire-and-forget: spawn the background write thread and return immediately.
    Adds zero latency to the caller (same contract as extract_async)."""
    if not Config.convo_memory_write_enabled():
        return
    if not (user_text or "").strip():
        return
    threading.Thread(
        target=_safe_record,
        args=(session_id, active_business, user_text, reply),
        daemon=True,
    ).start()


def _safe_record(session_id, active_business, user_text, reply) -> None:
    try:
        _record(session_id, active_business, user_text, reply)
    except Exception:  # noqa: BLE001 — a write failure must never affect the conversation
        log.warning("conversation write failed", exc_info=True)


def _record(session_id, active_business, user_text, reply) -> None:
    uid = repo.owner_user_id()
    if not uid:
        return  # no owner row yet => nothing to key recall on; safe no-op
    biz = None if (not active_business or active_business == "all") else active_business
    exchange = f"User: {(user_text or '').strip()}\nHermes: {(reply or '').strip()}".strip()
    if not exchange:
        return

    # 1) Verbatim turn — the citable record. Embed the whole exchange so recall
    #    matches on either what was asked or what was answered.
    turn_vec = embed([exchange])[0]
    repo.write_turn(
        user_id=uid, session_id=session_id, seq=None, text=exchange,
        vector=turn_vec, business_slug=biz, embed_model=EMBED_MODEL,
    )

    # 2) Extractive summary — navigational only, best-effort, needs a session to
    #    key idempotency on.
    if session_id:
        summary = _make_summary(user_text or "", reply or "")
        if summary:
            try:
                summ_vec = embed([summary])[0]
                repo.upsert_summary(
                    user_id=uid, session_id=session_id, text=summary,
                    vector=summ_vec, business_slug=biz, embed_model=EMBED_MODEL,
                )
            except Exception:  # noqa: BLE001 — summary is a bonus; the turn already landed
                log.warning("conversation summary upsert failed", exc_info=True)
