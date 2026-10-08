"""Postgres-backed conversation history for Hermes.

Drop-in replacement for the in-memory ConversationStore. Same public methods, so
app.py and agent.py don't change:

    get(key)                          -> prior turns as Claude messages (agent memory)
    get_with_state(key)               -> (get(key), TurnState) in one query (hermes/turn_state.py)
    set(key, messages)               -> no-op (persistence happens in record_turn)
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
display/record truth; long-term semantic memory (documents + the owner/
business/project memory in hermes/db/memories.py) lives in pgvector, also
Postgres.
"""
from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from ..turn_state import FAILED, TELEMETRY_FIELDS, TurnState, history_limit, state_from_rows
from .connection import cursor

log = logging.getLogger("hermes.db.conversations")


def _iso(epoch_seconds: float | None) -> str | None:
    """Unix epoch seconds -> ISO 8601 UTC string (e.g. "2026-07-13T14:32:07.078369+00:00")."""
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).isoformat()


def _session_id_from_key(key: str) -> str:
    """The store key is `entity_id:session_id` (or just `session_id`)."""
    return key.split(":", 1)[1] if ":" in key else key


def _collect_attachments(metas) -> list[dict[str, Any]]:
    """Dedupe `tool_calls[].media` file pointers from an iterable of turn
    metadata dicts (given newest-first). Returns newest-first, one per s3_uri."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        for tc in meta.get("tool_calls") or []:
            media = tc.get("media") if isinstance(tc, dict) else None
            if not isinstance(media, dict):
                continue
            uri = media.get("s3_uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            out.append({
                "filename": media.get("filename") or (uri.rsplit("/", 1)[-1]),
                "s3_uri": uri,
                "content_type": media.get("content_type"),
                "kind": media.get("kind"),
            })
    return out


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
        return self._to_messages(rows)

    @staticmethod
    def _to_messages(rows) -> list[dict[str, Any]]:
        """`(role, content)` rows, oldest first -> Claude messages.

        Shared by get() and get_with_state() so the history the model sees is
        byte-identical whichever one a caller uses."""
        rows = list(rows)
        # The conversation Claude sees must begin with a user turn.
        while rows and rows[0][0] != "user":
            rows.pop(0)
        return [
            {"role": role, "content": [{"type": "text", "text": content or ""}]}
            for role, content in rows
        ]

    def get_with_state(self, key: str) -> tuple[list[dict[str, Any]], TurnState]:
        """get() plus the previous reply's persisted state, in ONE round-trip
        (TURN_COST_PLAN step 0.6).

        Same window, same ordering and the same messages as get() — the SELECT
        only adds columns: the per-row `metadata->'tool_calls'`, `->'route'`,
        `->>'stop_reason'` and `->'pending'` (B6) (NULL on user rows, whose metadata is the '{}'
        default from 0003), plus one uncorrelated scalar subquery counting the
        conversation's user rows. Postgres runs that subquery once (an
        InitPlan), on `messages_session_idx (session_id, seq)` from 0001, so
        it is one extra index scan inside the same statement — not a second
        query.

        Failure is EXPLICIT, unlike get(): a DB error returns `([], FAILED)`
        (ok=False) so the caller can tell "no rows" from "read failed" (B23).
        A non-uuid key is not a failure — there is genuinely nothing stored
        under it, exactly as get() reports."""
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return [], TurnState()
        limit = self._history_limit()
        try:
            with cursor(commit=False) as cur:
                cur.execute(
                    "SELECT role, content, "
                    "metadata->'tool_calls', metadata->'route', metadata->>'stop_reason', "
                    "metadata->'pending', "
                    "(SELECT count(*) FROM hermes_messages u "
                    " WHERE u.session_id = %s AND u.role = 'user') "
                    "FROM hermes_messages "
                    "WHERE session_id = %s AND role IN ('user', 'assistant') "
                    "ORDER BY seq DESC LIMIT %s;",
                    (session_id, session_id, limit),
                )
                rows = list(reversed(cur.fetchall()))
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, but say so
            log.warning("get_with_state(%s) failed: %s", session_id, exc)
            return [], FAILED
        messages = self._to_messages((row[0], row[1]) for row in rows)
        try:
            # Every row carries the same count; no rows means no user turns.
            user_turns = int(rows[-1][6]) if rows else 0
            state = state_from_rows(
                ((role, content, tool_calls, stop_reason, route, pending)
                 for role, content, tool_calls, route, stop_reason, pending, _ in rows),
                user_turns,
            )
        except Exception as exc:  # noqa: BLE001 — history is fine; the state is unknown
            log.warning("get_with_state(%s) state decode failed: %s", session_id, exc)
            state = FAILED
        return messages, state

    def attachments(self, key: str) -> list[dict[str, Any]]:
        """Files shared into this conversation and stored in S3, newest first.

        Walks the persisted assistant-turn metadata (`tool_calls[].media`, written
        by record_turn) and returns a deduped `{filename, s3_uri, content_type,
        kind}` list. This is what makes a WhatsApp doc discoverable on later turns:
        get() drops the in-turn tool blocks, but the S3 pointer survives here.
        """
        session_id = _session_id_from_key(key)
        if not _is_uuid(session_id):
            return []
        try:
            with cursor(commit=False) as cur:
                cur.execute(
                    "SELECT metadata FROM hermes_messages "
                    "WHERE session_id = %s AND role = 'assistant' "
                    "AND metadata IS NOT NULL ORDER BY seq DESC;",
                    (session_id,),
                )
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("attachments(%s) failed: %s", session_id, exc)
            return []
        return _collect_attachments(meta for (meta,) in rows)

    @staticmethod
    def _history_limit() -> int:
        # One definition for both stores (see hermes/turn_state.history_limit):
        # the in-memory store derives its TurnState from the same window.
        return history_limit()

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
        usage = result.get("usage") or {}
        cost_usd = result.get("cost_usd")
        meta = {
            "thinking": result.get("thinking"),
            "tool_calls": result.get("tool_calls") or [],
            "stop_reason": result.get("stop_reason"),
            # How the reply was routed (CHAT/LIGHT/FULL, TURN_COST_PLAN step
            # 1.2) — read back by get_with_state() as TurnState.last_route.
            # Absent until the router ships, so today's rows are unchanged
            # (None is dropped below).
            "route": result.get("route"),
            # Actions this reply held for the user's confirmation (B6): read back
            # by get_with_state() so an explicit "yes" next turn gets its token.
            # Deliberately NOT returned by transcript() — never sent to the UI.
            "pending": result.get("pending"),
            # Persist per-response usage so the token/cost line replays from history.
            "usage": usage or None,
            "cost_usd": cost_usd,
            # Per-turn telemetry (TURN_COST_PLAN step 0.5): the provider calls
            # (model, tokens, fallback flag), the prompt breakdown and the
            # reasoning/cached totals — numbers and model ids only, never
            # text. A result from before 0.5 has none of them and its row is
            # unchanged (None is dropped below).
            **{field: result.get(field) for field in TELEMETRY_FIELDS},
        }
        meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
        telemetry = {k: meta[k] for k in TELEMETRY_FIELDS if k in meta}
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
            return

        # Usage/cost lands in its OWN transaction, after the messages are safely
        # committed above. A failure here (e.g. the 0016 migration hasn't been
        # applied yet, so ai_runs lacks the cost columns) must never roll back
        # the conversation itself — it's analytics, not the reply.
        if usage:
            self._record_usage(session_id, ent, owner, model, usage, cost_usd, telemetry)

    def _record_usage(
        self,
        session_id: str,
        ent: str | None,
        owner: str,
        model: str,
        usage: dict[str, Any],
        cost_usd: float | None,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        """Write one ai_runs row for the Usage tab, isolated so its failure
        can't affect message persistence.

        Uses only columns present in the base schema (0001) — cost and cache
        tokens go into the existing `metadata` jsonb — so this works with NO
        extra migration. The 0016 columns, if applied, are simply left unused.
        The step 0.5 telemetry rides in the same jsonb, so the cost reports
        can read per-call models and the fallback rate straight off ai_runs.
        """
        run_meta = {
            "cost_usd": cost_usd,
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            **(telemetry or {}),
        }
        try:
            with cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai_runs
                        (entity_id, session_id, user_id, model_used,
                         input_tokens, output_tokens, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        ent, session_id, owner, model,
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        json.dumps(run_meta),
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — analytics write, never fatal
            log.warning("ai_runs write skipped for %s: %s", session_id, exc)

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
                "created_at": _iso(created),
                "updated_at": _iso(updated),
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
                    "SELECT role, content, metadata, model_used FROM hermes_messages "
                    "WHERE session_id = %s ORDER BY seq;",
                    (session_id,),
                )
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("transcript(%s) failed: %s", session_id, exc)
            return []
        out: list[dict[str, Any]] = []
        for role, content, meta, model_used in rows:
            meta = meta or {}
            if role == "assistant":
                out.append({
                    "role": "assistant",
                    "text": content or "",
                    "thinking": meta.get("thinking"),
                    "tool_calls": meta.get("tool_calls") or [],
                    "usage": meta.get("usage"),
                    "cost_usd": meta.get("cost_usd"),
                    "model_used": model_used,
                    # Step 0.5 telemetry for the footer tooltip; None on rows
                    # written before it, which the UI renders without.
                    **{field: meta.get(field) for field in TELEMETRY_FIELDS},
                })
            else:
                out.append({"role": role, "text": content or ""})
        return out
