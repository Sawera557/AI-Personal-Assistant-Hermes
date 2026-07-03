"""Postgres-backed meeting copilot history — mirrors hermes/db/conversations.py.

meeting_sessions — one row per meeting, owned by the same single owner user,
                   always locked to one specific business (entity_id).
meeting_turns    — one row per heard/typed utterance + the guidance it produced.

Every function degrades gracefully (logs a warning, returns None/[]) rather
than raising — a DB hiccup should never break live guidance, which works
without any of this (see hermes/meeting_copilot.py). This is additive history,
not a dependency of the live turn.
"""
from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.meetings")


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _owner(cur) -> str:
    """Same owner-first resolution as conversations.py's PostgresConversationStore."""
    email = (os.getenv("OWNER_EMAIL") or "owner@hermes.local").strip().lower()
    name = (os.getenv("OWNER_NAME") or "Owner").strip()
    cur.execute("SELECT user_id FROM users WHERE lower(email) = %s;", (email,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING user_id;",
            (name, email),
        )
        row = cur.fetchone()
    return str(row[0])


def _resolve_entity_id(cur, entity_id: str) -> str | None:
    eid = str(entity_id or "").strip()
    if not eid:
        return None
    col = "entity_id" if _is_uuid(eid) else "slug"
    cur.execute(f"SELECT entity_id FROM entities WHERE {col} = %s;", (eid,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def create_session(entity_id: str, title: str | None = None) -> str | None:
    """Start a new meeting session, locked to one business.

    Returns the new session_id, or None if the business can't be resolved or
    the write fails (caller should degrade to an ephemeral, unsaved session).
    """
    try:
        with cursor() as cur:
            owner = _owner(cur)
            ent = _resolve_entity_id(cur, entity_id)
            if not ent:
                log.warning("create_session: unknown business %r", entity_id)
                return None
            cur.execute(
                """INSERT INTO meeting_sessions (user_id, entity_id, title)
                   VALUES (%s, %s, %s) RETURNING session_id;""",
                (owner, ent, title),
            )
            return str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("create_session failed: %s", exc)
        return None


def add_turn(
    session_id: str,
    *,
    heard_text: str | None,
    bullets: list[str],
    sources: list[dict[str, Any]],
) -> None:
    """Persist one utterance + the guidance it produced. Bumps updated_at and,
    on the first turn, sets the session title from the heard text."""
    if not _is_uuid(session_id):
        log.warning("add_turn skipped: session_id %r is not a uuid", session_id)
        return
    try:
        with cursor() as cur:
            cur.execute(
                "SELECT entity_id, title FROM meeting_sessions WHERE session_id = %s;",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                log.warning("add_turn: unknown session_id %s", session_id)
                return
            entity_id, title = row

            cur.execute(
                "SELECT COALESCE(max(seq), 0) FROM meeting_turns WHERE session_id = %s;",
                (session_id,),
            )
            seq = int(cur.fetchone()[0]) + 1

            cur.execute(
                """INSERT INTO meeting_turns
                       (session_id, entity_id, seq, heard_text, bullets, sources)
                   VALUES (%s, %s, %s, %s, %s, %s);""",
                (
                    session_id, entity_id, seq, heard_text,
                    json.dumps(bullets or []), json.dumps(sources or []),
                ),
            )

            updates = ["updated_at = now()"]
            params: list[Any] = []
            if not title and heard_text:
                updates.append("title = %s")
                params.append(heard_text.strip()[:60])
            params.append(session_id)
            cur.execute(
                f"UPDATE meeting_sessions SET {', '.join(updates)} WHERE session_id = %s;",
                params,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("add_turn(%s) failed: %s", session_id, exc)


def end_session(session_id: str) -> None:
    if not _is_uuid(session_id):
        return
    try:
        with cursor() as cur:
            cur.execute(
                "UPDATE meeting_sessions SET status = 'ended' WHERE session_id = %s;",
                (session_id,),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("end_session(%s) failed: %s", session_id, exc)


def delete_session(session_id: str) -> bool:
    if not _is_uuid(session_id):
        return False
    try:
        with cursor() as cur:
            cur.execute(
                "UPDATE meeting_sessions SET status = 'deleted' WHERE session_id = %s;",
                (session_id,),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_session(%s) failed: %s", session_id, exc)
        return False


def sessions(entity_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List past meeting sessions (newest first) for the history sidebar."""
    try:
        with cursor(commit=False) as cur:
            owner = _owner(cur)
            where = ["s.user_id = %s", "s.status <> 'deleted'"]
            params: list[Any] = [owner]
            if entity_id:
                ent = _resolve_entity_id(cur, entity_id)
                where.append("s.entity_id = %s")
                params.append(ent)
            cur.execute(
                f"""SELECT s.session_id, s.title, s.status, e.slug,
                           extract(epoch FROM s.created_at),
                           extract(epoch FROM s.updated_at),
                           count(t.turn_id)
                    FROM meeting_sessions s
                    JOIN entities e ON e.entity_id = s.entity_id
                    LEFT JOIN meeting_turns t ON t.session_id = s.session_id
                    WHERE {' AND '.join(where)}
                    GROUP BY s.session_id, e.slug
                    ORDER BY s.updated_at DESC
                    LIMIT %s;""",
                params + [limit],
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("sessions() failed: %s", exc)
        return []

    from ..businesses import to_business_slug

    return [
        {
            "session_id": str(sid),
            "title": title or "New meeting",
            "status": status,
            "business": to_business_slug(entity_slug),
            "created_at": created,
            "updated_at": updated,
            "turn_count": int(count),
        }
        for sid, title, status, entity_slug, created, updated, count in rows
    ]


def transcript(session_id: str) -> dict[str, Any] | None:
    """Full replay of one meeting session: its metadata + every turn."""
    if not _is_uuid(session_id):
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT s.title, s.status, e.slug,
                          extract(epoch FROM s.created_at),
                          extract(epoch FROM s.updated_at)
                   FROM meeting_sessions s
                   JOIN entities e ON e.entity_id = s.entity_id
                   WHERE s.session_id = %s;""",
                (session_id,),
            )
            meta = cur.fetchone()
            if not meta:
                return None
            cur.execute(
                """SELECT heard_text, bullets, sources, seq,
                          extract(epoch FROM created_at)
                   FROM meeting_turns WHERE session_id = %s ORDER BY seq;""",
                (session_id,),
            )
            turns = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("transcript(%s) failed: %s", session_id, exc)
        return None

    from ..businesses import to_business_slug

    title, status, entity_slug, created, updated = meta
    return {
        "session_id": session_id,
        "title": title or "New meeting",
        "status": status,
        "business": to_business_slug(entity_slug),
        "created_at": created,
        "updated_at": updated,
        "turns": [
            {"heard_text": h, "bullets": b or [], "sources": s or [], "seq": sq, "created_at": t}
            for h, b, s, sq, t in turns
        ],
    }
