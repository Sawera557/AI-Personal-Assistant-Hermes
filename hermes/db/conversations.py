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
        """Rebuild prior turns as Claude messages so the agent has context."""
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return []
        try:
            with cursor(commit=False) as cur:
                cur.execute(
                    "SELECT role, content FROM hermes_messages "
                    "WHERE session_id = %s AND role IN ('user', 'assistant') "
                    "ORDER BY seq;",
                    (session_id,),
                )
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            log.warning("get(%s) failed: %s", session_id, exc)
            return []
        return [
            {"role": role, "content": [{"type": "text", "text": content or ""}]}
            for role, content in rows
        ]

    def set(self, key: str, messages: list[dict[str, Any]]) -> None:
        """No-op: persistence happens in record_turn (which has the clean text).

        Kept so the agent loop's interface is unchanged. The in-turn working list
        lives in the agent's local variable; it never needs to round-trip here.
        """
        return None

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

    def conversations(self, entity_id: str | None) -> list[dict[str, Any]]:
        """List the owner's conversations (newest first) for the sidebar."""
        try:
            with cursor(commit=False) as cur:
                owner = self._owner(cur)
                ent = self._resolve_entity_id(cur, entity_id)
                params: list[Any] = [owner]
                entity_clause = ""
                if entity_id:  # filter to chats that touched this business
                    entity_clause = (
                        " AND s.session_id IN "
                        "(SELECT session_id FROM hermes_messages WHERE entity_id = %s)"
                    )
                    params.append(ent)
                cur.execute(
                    f"""
                    SELECT s.session_id,
                           s.title,
                           extract(epoch FROM s.created_at),
                           extract(epoch FROM s.updated_at),
                           count(m.message_id)
                    FROM hermes_sessions s
                    JOIN hermes_messages m ON m.session_id = s.session_id
                    WHERE s.user_id = %s AND s.status <> 'deleted'{entity_clause}
                    GROUP BY s.session_id
                    ORDER BY s.updated_at DESC;
                    """,
                    params,
                )
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("conversations() failed: %s", exc)
            return []
        return [
            {
                "session_id": str(sid),
                "title": title or "New conversation",
                "created_at": created,
                "updated_at": updated,
                "message_count": int(count),
            }
            for sid, title, created, updated, count in rows
        ]

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
