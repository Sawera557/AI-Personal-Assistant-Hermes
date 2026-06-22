"""In-memory conversation store keyed by session id.

Swap this for Redis / a database to persist across restarts. The public
interface (get / append / reset) is all the agent depends on.
"""
from __future__ import annotations

import threading
from typing import Any


class ConversationStore:
    def __init__(self, max_messages: int = 100):
        self._lock = threading.Lock()
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._max_messages = max_messages

    def get(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def set(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            # Keep the most recent turns to bound memory growth.
            self._sessions[session_id] = messages[-self._max_messages :]

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


store = ConversationStore()
