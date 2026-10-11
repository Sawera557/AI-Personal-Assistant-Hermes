"""Postgres storage for conversation-history memory (Phase 2, migration 0018).

The provenance-first record of past chats — so Hermes recalls what was actually
said, WHEN, and in which business, and can cite it. This layer is pure storage:
callers pass an already-computed embedding (one shared query embedding per turn),
exactly like hermes/db/memories.py. Never runs the model, never embeds.

Two kinds:
  * 'turn'    — a verbatim user+assistant exchange (high-fidelity recent recall).
  * 'summary' — an EXTRACTIVE rolling session summary (navigational, long-range).
                Idempotent per (session_id, content) so re-summarising a session
                supersedes the old summary instead of piling up duplicates.

Zero-hallucination stance: recall returns the stored verbatim `text` plus its
provenance (session_id, seq, business_slug, occurred_at). The prompt layer cites
that; it never lets the model invent a memory. Newest-wins via status/superseded.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from pgvector import Vector

from .connection import cursor
from .documents import _is_uuid

# Single-owner resolution — mirrors PostgresConversationStore._owner so read and
# write share one owner id without depending on the conversation store instance.
_OWNER_EMAIL = (os.getenv("OWNER_EMAIL") or "owner@hermes.local").strip().lower()
_owner_cache: dict[str, str] = {}


def owner_user_id() -> str | None:
    """The owner's users.user_id (cached). Read-only — never creates the row
    (the conversation store already does on first chat); returns None if absent,
    so a missing owner makes conversation memory a safe no-op rather than an
    error."""
    if "id" in _owner_cache:
        return _owner_cache["id"]
    try:
        with cursor(commit=False) as cur:
            cur.execute("SELECT user_id FROM users WHERE lower(email) = %s;", (_OWNER_EMAIL,))
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 — no owner => conversation memory no-ops
        return None
    uid = str(row[0]) if row else None
    if uid:
        _owner_cache["id"] = uid
    return uid


def content_hash(text: str | None) -> str:
    """Stable idempotency key for a summary — collapse whitespace so trivial
    diffs don't churn versions."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---- Write ----------------------------------------------------------------
def write_turn(
    *,
    user_id: str,
    session_id: str | None,
    seq: int | None,
    text: str,
    vector: list[float],
    business_slug: str | None = None,
    embed_model: str | None = None,
) -> str | None:
    """Insert one verbatim turn. Returns the new row id, or None if user_id is
    not a uuid (nothing to key owner-scoped recall on) or text is empty."""
    text = (text or "").strip()
    if not text or not _is_uuid(user_id):
        return None
    params = {
        "user_id": user_id,
        "session_id": session_id if (session_id and _is_uuid(session_id)) else None,
        "seq": seq,
        "text": text,
        "embedding": Vector(vector),
        "embed_model": embed_model,
        "business_slug": business_slug,
        "content_hash": content_hash(text),
    }
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversation_memory
              (user_id, session_id, kind, seq, text, embedding, embed_model,
               business_slug, content_hash)
            VALUES
              (%(user_id)s, %(session_id)s, 'turn', %(seq)s, %(text)s,
               %(embedding)s, %(embed_model)s, %(business_slug)s, %(content_hash)s)
            RETURNING id;
            """,
            params,
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def upsert_summary(
    *,
    user_id: str,
    session_id: str,
    text: str,
    vector: list[float],
    business_slug: str | None = None,
    embed_model: str | None = None,
) -> str | None:
    """Idempotently store a session's rolling summary. If the identical summary
    is already active for this session, no-op. Otherwise supersede the prior
    active summary for the session (newest-wins) and insert the new one.

    A per-session advisory lock serializes concurrent summarisations of the same
    session so they can't both insert — mirrors memories.write_memory."""
    text = (text or "").strip()
    if not text or not _is_uuid(user_id) or not _is_uuid(session_id):
        return None
    ch = content_hash(text)
    with cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (f"convsum:{session_id}",))
        # Already have this exact summary active? Nothing to do.
        cur.execute(
            """SELECT id FROM conversation_memory
               WHERE session_id = %s AND kind = 'summary'
                 AND status = 'active' AND content_hash = %s;""",
            (session_id, ch),
        )
        if cur.fetchone():
            return None
        # Supersede any prior active summary for this session (newest wins).
        cur.execute(
            """UPDATE conversation_memory SET status = 'superseded'
               WHERE session_id = %s AND kind = 'summary' AND status = 'active';""",
            (session_id,),
        )
        cur.execute(
            """
            INSERT INTO conversation_memory
              (user_id, session_id, kind, text, embedding, embed_model,
               business_slug, content_hash)
            VALUES
              (%(user_id)s, %(session_id)s, 'summary', %(text)s, %(embedding)s,
               %(embed_model)s, %(business_slug)s, %(content_hash)s)
            RETURNING id;
            """,
            {
                "user_id": user_id,
                "session_id": session_id,
                "text": text,
                "embedding": Vector(vector),
                "embed_model": embed_model,
                "business_slug": business_slug,
                "content_hash": ch,
            },
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


# ---- Read (guarded recall) -------------------------------------------------
def semantic_search(
    vector: list[float],
    *,
    user_id: str,
    top_k: int = 6,
    business_slug: str | None = None,
    kinds: tuple[str, ...] = ("turn", "summary"),
) -> list[dict[str, Any]]:
    """Owner-scoped semantic recall over active conversation rows. Returns each
    hit with its provenance (session_id, seq, kind, business_slug, occurred_at)
    and a cosine score, so the prompt layer can cite 'on <date> you said …'.

    `business_slug=None` recalls across ALL the owner's conversations (the
    'remembers everything' default); pass a slug to bias to one business.
    Returns [] for a non-uuid user (nothing to scope to)."""
    if not _is_uuid(user_id) or not kinds:
        return []
    params: dict[str, Any] = {
        "user_id": user_id,
        "qvec": Vector(vector),
        "top_k": top_k,
        "kinds": list(kinds),
    }
    where = [
        "user_id = %(user_id)s",
        "status = 'active'",
        "embedding IS NOT NULL",
        "kind = ANY(%(kinds)s)",
    ]
    if business_slug:
        # Bias to one business but still allow all-scope (NULL) rows through.
        where.append("(business_slug = %(business_slug)s OR business_slug IS NULL)")
        params["business_slug"] = business_slug
    sql = f"""
        SELECT id, session_id, kind, seq, text, business_slug, occurred_at,
               1 - (embedding <=> %(qvec)s) AS score
        FROM conversation_memory
        WHERE {' AND '.join(where)}
        ORDER BY embedding <=> %(qvec)s
        LIMIT %(top_k)s;
    """
    with cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "session_id": str(r[1]) if r[1] else None,
            "kind": r[2],
            "seq": r[3],
            "text": r[4],
            "business_slug": r[5],
            "occurred_at": r[6],
            "score": r[7],
        }
        for r in rows
    ]


def prune_turns(*, older_than_days: int = 45) -> int:
    """Delete verbatim 'turn' rows older than the cutoff (summaries are kept —
    they're the long-range memory). Returns the number removed. Bounds table
    growth without losing durable recall."""
    with cursor() as cur:
        cur.execute(
            """DELETE FROM conversation_memory
               WHERE kind = 'turn'
                 AND occurred_at < now() - make_interval(days => %s);""",
            (older_than_days,),
        )
        return cur.rowcount or 0
