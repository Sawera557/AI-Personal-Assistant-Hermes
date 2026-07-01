"""Postgres-backed conversation history for Hermes.

Drop-in replacement for the in-memory ConversationStore. Same public methods, so
app.py and agent.py don't change:

    get(key)                          -> prior turns as Claude messages (agent memory)
    set(key, messages)                -> no-op (persistence happens in record_turn)
    reset(key)                        -> delete the conversation
    record_turn(key, entity_id, session_id, user_text, result)
    conversations(entity_id)          -> sidebar list (newest first)
    transcript(key)                   -> full replay of one conversation

Architecture (per the project scope doc):
  * hermes_sessions  — one row per chat, owned by a USER (owner-first for now).
  * hermes_messages  — one row per message, optionally tagged with an entity_id
    (a business). content holds the text; metadata jsonb holds the assistant's
    reasoning summary + tool traces so a reloaded chat replays identically.

Cross-turn agent memory is rebuilt from the stored user/assistant text. The
in-turn tool-use blocks stay ephemeral within a single turn — Postgres is the
display/record truth; long-term semantic memory lives in Pinecone.
"""
from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.conversations")


def _session_id_from_key(key: str) -> str:
    """The store key is `entity_id:session_id` (or just `session_id`)."""
    return key.split(":", 1)[1] if ":" in key else key


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class PostgresConversationStore:
    def __init__(self) -> None:
        self._owner_id: str | None = None

    # ---- Owner / entity resolution ----------------------------------------
    def _owner(self, cur) -> str:
        """Return the owner user_id, creating the owner row once if needed.

        Owner-first: a single owner backs every chat until multi-user lands.
        Override via OWNER_EMAIL / OWNER_NAME.
        """
        if self._owner_id:
            return self._owner_id
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
        self._owner_id = str(row[0])
        return self._owner_id

    @staticmethod
    def _resolve_entity_id(cur, entity_id: str | None) -> str | None:
        """Accept a uuid or a slug (or None) and return the entity uuid, or None."""
        if not entity_id:
            return None
        eid = str(entity_id).strip()
        if not eid:
            return None
        if _is_uuid(eid):
            cur.execute("SELECT entity_id FROM entities WHERE entity_id = %s;", (eid,))
        else:
            cur.execute("SELECT entity_id FROM entities WHERE slug = %s;", (eid,))
        row = cur.fetchone()
        return str(row[0]) if row else None

    # ---- Agent-facing API --------------------------------------------------
    def get(self, key: str) -> list[dict[str, Any]]:
        """Rebuild recent turns as Claude messages so the agent has context.

        Only the most recent HERMES_HISTORY_MESSAGES messages are loaded, so token
        cost stays bounded on long chats. The window is trimmed to start with a
        `user` message, which the Claude API requires.
        """
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return []
        limit = self._history_limit()
        try:
            with cursor(commit=False) as cur:
                # Take the last `limit` messages (newest first), then reverse.
                cur.execute(
                    "SELECT role, content FROM hermes_messages "
                    "WHERE session_id = %s AND role IN ('user', 'assistant') "
                    "ORDER BY seq DESC LIMIT %s;",
                    (session_id, limit),
                )
                rows = list(reversed(cur.fetchall()))
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            log.warning("get(%s) failed: %s", session_id, exc)
            return []
        # The conversation Claude sees must begin with a user turn.
        while rows and rows[0][0] != "user":
            rows.pop(0)
        return [
            {"role": role, "content": [{"type": "text", "text": content or ""}]}
            for role, content in rows
        ]

    @staticmethod
    def _history_limit() -> int:
        try:
            return max(2, int(os.getenv("HERMES_HISTORY_MESSAGES", "40")))
        except ValueError:
            return 40

    def set(self, key: str, messages: list[dict[str, Any]]) -> None:
        """No-op: persistence happens in record_turn (which has the clean text).

        Kept so the agent loop's interface is unchanged. The in-turn working list
        lives in the agent's local variable; it never needs to round-trip here.
        """
        return None

    def locked_business_slug(self, key: str) -> str | None:
        """The business slug this conversation is locked to, from its first
        message — or None if it started as "All Businesses" / predates this
        feature (both cases mean "no single business restriction"). `key` is
        the composite `entity_id:session_id` store key, same as get()/reset()."""
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return None
        try:
            with cursor(commit=False) as cur:
                cur.execute(
                    "SELECT entity_id FROM hermes_messages "
                    "WHERE session_id = %s ORDER BY seq ASC LIMIT 1;",
                    (session_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                cur.execute("SELECT slug FROM entities WHERE entity_id = %s;", (row[0],))
                erow = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("locked_business_slug(%s) failed: %s", session_id, exc)
            return None
        from ..businesses import to_business_slug  # local import: avoid any cycle risk
        return to_business_slug(erow[0] if erow else None)

    def reset(self, key: str) -> None:
        """Delete a conversation (cascades to its messages)."""
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return
        try:
            with cursor() as cur:
                cur.execute("DELETE FROM hermes_sessions WHERE session_id = %s;", (session_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("reset(%s) failed: %s", session_id, exc)

    # ---- History API -------------------------------------------------------
    def record_turn(
        self,
        key: str,
        entity_id: str | None,
        session_id: str,
        user_text: str,
        result: dict[str, Any],
    ) -> None:
        """Persist one user + assistant exchange to Postgres."""
        if not _is_uuid(session_id):
            log.warning("record_turn skipped: session_id %r is not a uuid", session_id)
            return
        reply = result.get("reply") or ""
        meta = {
            "thinking": result.get("thinking"),
            "tool_calls": result.get("tool_calls") or [],
            "stop_reason": result.get("stop_reason"),
        }
        meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
        model = result.get("model_used") or os.getenv("HERMES_MODEL", "claude-opus-4-8")
        title = (user_text or "").strip()[:60] or "New conversation"

        try:
            with cursor() as cur:
                owner = self._owner(cur)
                ent = self._resolve_entity_id(cur, entity_id)

                # Upsert the session (create on first turn; keep its title/order).
                cur.execute(
                    """
                    INSERT INTO hermes_sessions (session_id, user_id, title)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE SET updated_at = now();
                    """,
                    (session_id, owner, title),
                )

                # Next sequence number for this session.
                cur.execute(
                    "SELECT COALESCE(max(seq), 0) FROM hermes_messages WHERE session_id = %s;",
                    (session_id,),
                )
                seq = int(cur.fetchone()[0])

                cur.execute(
                    """
                    INSERT INTO hermes_messages
                        (session_id, entity_id, role, content, seq)
                    VALUES (%s, %s, 'user', %s, %s);
                    """,
                    (session_id, ent, user_text, seq + 1),
                )
                cur.execute(
                    """
                    INSERT INTO hermes_messages
                        (session_id, entity_id, role, content, seq, model_used, metadata)
                    VALUES (%s, %s, 'assistant', %s, %s, %s, %s);
                    """,
                    (session_id, ent, reply, seq + 2, model, json.dumps(meta)),
                )
        except Exception as exc:  # noqa: BLE001 — never break the reply on a write miss
            log.warning("record_turn(%s) failed: %s", session_id, exc)

    def conversations(
        self, entity_id: str | None, query: str | None = None
    ) -> list[dict[str, Any]]:
        """List the owner's conversations (newest first) for the sidebar.

        With `query`, returns only chats whose title or any message matches, and
        includes a `snippet` of the first matching message.
        """
        q = (query or "").strip()
        try:
            with cursor(commit=False) as cur:
                owner = self._owner(cur)
                ent = self._resolve_entity_id(cur, entity_id)

                # Built in clause order (SELECT params, then WHERE params), since
                # positional %s placeholders must line up with the SQL text.
                select_params: list[Any] = []
                where = ["s.user_id = %s", "s.status <> 'deleted'"]
                where_params: list[Any] = [owner]
                snippet_select = ""

                if entity_id:  # filter to chats that touched this business
                    where.append(
                        "s.session_id IN "
                        "(SELECT session_id FROM hermes_messages WHERE entity_id = %s)"
                    )
                    where_params.append(ent)

                if q:
                    like = f"%{q}%"
                    snippet_select = (
                        ", (SELECT mm.content FROM hermes_messages mm "
                        "WHERE mm.session_id = s.session_id AND mm.content ILIKE %s "
                        "ORDER BY mm.seq LIMIT 1) AS snippet"
                    )
                    select_params.append(like)
                    where.append(
                        "(s.title ILIKE %s OR s.session_id IN "
                        "(SELECT session_id FROM hermes_messages WHERE content ILIKE %s))"
                    )
                    where_params.extend([like, like])

                cur.execute(
                    f"""
                    SELECT s.session_id,
                           s.title,
                           extract(epoch FROM s.created_at),
                           extract(epoch FROM s.updated_at),
                           count(m.message_id),
                           (SELECT mm.entity_id FROM hermes_messages mm
                            WHERE mm.session_id = s.session_id
                            ORDER BY mm.seq ASC LIMIT 1) AS locked_entity_id
                           {snippet_select}
                    FROM hermes_sessions s
                    JOIN hermes_messages m ON m.session_id = s.session_id
                    WHERE {' AND '.join(where)}
                    GROUP BY s.session_id
                    ORDER BY s.updated_at DESC;
                    """,
                    select_params + where_params,
                )
                rows = cur.fetchall()

                # Batch-resolve locked entity uuids -> business slugs (only 8
                # entities total today, so fetching all is simpler than an
                # ANY(%s) filter and just as cheap).
                slug_by_entity_id: dict[str, str] = {}
                try:
                    cur.execute("SELECT entity_id, slug FROM entities;")
                    slug_by_entity_id = {str(r[0]): r[1] for r in cur.fetchall()}
                except Exception as exc:  # noqa: BLE001
                    log.warning("conversations() entity lookup failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("conversations() failed: %s", exc)
            return []

        from ..businesses import to_business_slug  # local import: avoid any cycle risk

        out: list[dict[str, Any]] = []
        for row in rows:
            sid, title, created, updated, count, locked_entity_id = row[:6]
            snippet = row[6] if q and len(row) > 6 else None
            if snippet:
                snippet = " ".join(str(snippet).split())[:160]
            entity_slug = slug_by_entity_id.get(str(locked_entity_id)) if locked_entity_id else None
            out.append({
                "session_id": str(sid),
                "title": title or "New conversation",
                "created_at": created,
                "updated_at": updated,
                "message_count": int(count),
                "snippet": snippet,
                "business": to_business_slug(entity_slug),
            })
        return out

    def transcript(self, key: str) -> list[dict[str, Any]]:
        """Full replay of one conversation (user + assistant turns with extras)."""
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return []
        try:
            with cursor(commit=False) as cur:
                cur.execute(
                    "SELECT role, content, metadata FROM hermes_messages "
                    "WHERE session_id = %s ORDER BY seq;",
                    (session_id,),
                )
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("transcript(%s) failed: %s", session_id, exc)
            return []
        out: list[dict[str, Any]] = []
        for role, content, meta in rows:
            meta = meta or {}
            if role == "assistant":
                out.append({
                    "role": "assistant",
                    "text": content or "",
                    "thinking": meta.get("thinking"),
                    "tool_calls": meta.get("tool_calls") or [],
                })
            else:
                out.append({"role": role, "text": content or ""})
        return out
