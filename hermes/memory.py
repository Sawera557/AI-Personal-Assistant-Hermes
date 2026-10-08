"""In-memory conversation store keyed by session id.

Holds two things per conversation:

* ``messages`` — the raw Claude message list the agent loop reads/writes. This
  is the model's working memory for the turn-by-turn tool-use loop.
* ``transcript`` + metadata (title, timestamps) — a lightweight, display-only
  record of the conversation used to power the UI's conversation-history
  sidebar (list past chats, switch between them, replay them).

Both are kept in process memory, so history lives for as long as the server is
running. Swap this for Redis / a database to persist across restarts — the
public interface (get / get_with_state / set / reset for the agent, plus
record_turn / conversations / transcript for history) is all the rest of the
app depends on.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from .turn_state import FAILED, TELEMETRY_FIELDS, TurnState, history_limit, state_from_rows


def _now() -> float:
    return time.time()


def _iso(epoch_seconds: float) -> str:
    """Unix epoch seconds -> ISO 8601 UTC string, matching the Postgres-backed store."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, max_messages: int = 100):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._max_messages = max_messages

    def _record(self, key: str) -> dict[str, Any]:
        """Get (or lazily create) the record for a session key. Caller holds the lock."""
        rec = self._sessions.get(key)
        if rec is None:
            now = _now()
            rec = {
                "messages": [],      # raw Claude messages (agent working memory)
                "transcript": [],    # display turns for the UI history
                "title": "",
                "entity_id": None,
                "session_id": None,
                "created_at": now,
                "updated_at": now,
            }
            self._sessions[key] = rec
        return rec

    # ---- Agent-facing API (keyed by the full namespaced session key) -------
    def get(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            rec = self._sessions.get(key)
            return list(rec["messages"]) if rec else []

    def get_with_state(self, key: str) -> tuple[list[dict[str, Any]], TurnState]:
        """get() plus the previous reply's state (TURN_COST_PLAN step 0.6).

        Mirrors PostgresConversationStore.get_with_state. `messages` is exactly
        what get() returns (the raw working list, tool blocks included). The
        state is derived from the display transcript — the record_turn entries,
        which carry the same `tool_calls` / `stop_reason` / `route` the
        Postgres store keeps in message metadata — through the SAME
        hermes/turn_state.state_from_rows over the SAME history window, so the
        two backends cannot diverge on what is pending or how a reply was
        routed. Both are copied under one lock acquisition, so they describe
        the same moment.

        Any exception while deriving the state gives ok=False; the messages are
        still returned, as get() would."""
        with self._lock:
            rec = self._sessions.get(key)
            messages = list(rec["messages"]) if rec else []
            transcript = list(rec["transcript"]) if rec else []
        try:
            user_turns = sum(1 for t in transcript if t.get("role") == "user")
            state = state_from_rows(
                (
                    (t.get("role"), t.get("text"), t.get("tool_calls"),
                     t.get("stop_reason"), t.get("route"), t.get("_pending"))
                    for t in transcript[-history_limit():]
                ),
                user_turns,
            )
        except Exception:  # noqa: BLE001 — history is fine; the state is unknown
            state = FAILED
        return messages, state

    def set(self, key: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            rec = self._record(key)
            # Keep the most recent turns to bound memory growth.
            rec["messages"] = messages[-self._max_messages :]
            rec["updated_at"] = _now()

    def reset(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)

    def attachments(self, key: str) -> list[dict[str, Any]]:
        """Files shared into this conversation and stored in S3, newest first.

        Mirrors PostgresConversationStore.attachments: dedupes the `media`
        pointers recorded on each assistant turn's tool_calls, so a shared file
        stays referenceable on later turns.
        """
        from .db.conversations import _collect_attachments  # shared dedupe logic

        with self._lock:
            rec = self._sessions.get(key)
            transcript = list(rec["transcript"]) if rec else []
        metas = (
            {"tool_calls": t.get("tool_calls") or []}
            for t in reversed(transcript)
            if t.get("role") == "assistant"
        )
        return _collect_attachments(metas)

    # ---- History API (used by the conversation-history UI) -----------------
    def record_turn(
        self,
        key: str,
        entity_id: str | None,
        session_id: str,
        user_text: str,
        result: dict[str, Any],
    ) -> None:
        """Append one user+assistant exchange to the conversation's transcript.

        Called after each successful agent turn so the history sidebar can list
        and replay the conversation. The first user message becomes the title.
        """
        with self._lock:
            rec = self._record(key)
            # Lock the business to whatever the FIRST turn set — later turns
            # must not change which business a conversation is scoped to.
            if rec["entity_id"] is None:
                rec["entity_id"] = entity_id or None
            rec["session_id"] = session_id
            if not rec["title"] and user_text:
                rec["title"] = user_text.strip()[:60]
            rec["transcript"].append({"role": "user", "text": user_text})
            entry = {
                "role": "assistant",
                "text": result.get("reply") or "",
                "thinking": result.get("thinking"),
                "tool_calls": result.get("tool_calls") or [],
            }
            # stop_reason and route feed get_with_state (TURN_COST_PLAN 0.6),
            # mirroring what the Postgres store keeps in message metadata —
            # and, like it, only when the result actually carries them.
            # Same emptiness filter as the Postgres metadata dict.
            #
            # usage / cost_usd / model_used and the step 0.5 telemetry
            # (TELEMETRY_FIELDS) are what the Postgres store's transcript()
            # returns for the same row, so a no-DB replay shows the same
            # token/cost footer and tooltip. Numbers and model ids only.
            for field in ("stop_reason", "route", "usage", "cost_usd", "model_used",
                          *TELEMETRY_FIELDS):
                if result.get(field) not in (None, [], ""):
                    entry[field] = result[field]
            # Actions held for the user's confirmation (B6), read back by
            # get_with_state. Underscored and stripped by transcript(): like the
            # Postgres store, never sent to the UI.
            if result.get("pending"):
                entry["_pending"] = result["pending"]
            rec["transcript"].append(entry)
            rec["updated_at"] = _now()

    def conversations(
        self, entity_id: str | None, query: str | None = None
    ) -> list[dict[str, Any]]:
        """List an entity's conversations (newest first) for the sidebar.

        With `query`, returns only chats whose title or any message matches, and
        includes a `snippet` of the first matching message.
        """
        target = entity_id or None
        q = (query or "").strip().lower()
        out = []
        with self._lock:
            for rec in self._sessions.values():
                if rec["session_id"] is None or not rec["transcript"]:
                    continue
                if (rec["entity_id"] or None) != target:
                    continue
                snippet = None
                if q:
                    if q in (rec["title"] or "").lower():
                        pass
                    matches = [
                        t for t in rec["transcript"]
                        if q in (t.get("text") or "").lower()
                    ]
                    if not matches and q not in (rec["title"] or "").lower():
                        continue
                    if matches:
                        snippet = " ".join(str(matches[0]["text"]).split())[:160]
                from .businesses import to_business_slug
                out.append({
                    "session_id": rec["session_id"],
                    "title": rec["title"] or "New conversation",
                    "created_at": rec["created_at"],
                    "updated_at": rec["updated_at"],
                    "message_count": len(rec["transcript"]),
                    "snippet": snippet,
                    "business": to_business_slug(rec["entity_id"]),
                })
        out.sort(key=lambda c: c["updated_at"], reverse=True)
        for c in out:
            c["created_at"] = _iso(c["created_at"])
            c["updated_at"] = _iso(c["updated_at"])
        return out

    def transcript(self, key: str) -> list[dict[str, Any]]:
        """Return a conversation's display transcript for replay in the UI.

        Internal-only fields (underscored — the held approvals, B6) are left
        out, matching the Postgres store's transcript()."""
        with self._lock:
            rec = self._sessions.get(key)
            entries = list(rec["transcript"]) if rec else []
        return [
            {k: v for k, v in t.items() if not k.startswith("_")} for t in entries
        ]

    def locked_business_slug(self, key: str) -> str | None:
        """The business slug this conversation is locked to (its first turn's
        entity_id), or None if it started as "All Businesses" / has no record."""
        from .businesses import to_business_slug
        with self._lock:
            rec = self._sessions.get(key)
            return to_business_slug(rec["entity_id"]) if rec else None


def _make_store():
    """Use Postgres when a database is configured (DBRDS_HOST); else in-memory.

    Both expose the same interface (get/set/reset + record_turn/conversations/
    transcript), so the rest of the app is identical either way.
    """
    import os

    # Explicit escape hatch: force the in-memory store (tests / local dev / CI),
    # so a test run never writes to the real database even when DBRDS_HOST is set.
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return ConversationStore()

    if os.getenv("DBRDS_HOST", "").strip():
        try:
            from .db.conversations import PostgresConversationStore

            return PostgresConversationStore()
        except Exception as exc:  # noqa: BLE001 — fall back rather than crash on boot
            import logging

            logging.getLogger("hermes.memory").warning(
                "Postgres store unavailable, using in-memory store: %s", exc
            )
    return ConversationStore()


store = _make_store()
