"""Full context vs. plain chat: a platform-wide switch for what a turn carries.

Every turn normally ships the agent's whole working context — long-term memory,
the business context pack, retrieved knowledge, past-conversation recall — plus
the JSON schema of all 33 tools. That is the right default when you are doing
work. It is pure waste when you type "Hi": measured on this deployment, a
trivial message cost ~19,950 input tokens, of which roughly

    tool schemas .... 11,000   (65%)
    memory block .....1,880
    context pack .....1,873
    base prompt ......1,586
    retrieved RAG ......660

Turning the switch off drops the retrieval blocks AND the tool schemas, which is
why it covers both rather than context alone: dropping context by itself saves
about a fifth, leaving ~15k tokens on the table for a greeting.

The trade is explicit, not hidden — with it off Hermes cannot send email, reach
Drive/Jira/WhatsApp, or cite your documents, because it is not carrying the
tools or the knowledge to do so. It is a plain assistant. Turn it back on for
real work.

Storage mirrors hermes.models: Postgres `hermes_settings` when a database is
configured, otherwise a process-global that lasts until restart.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("hermes.context_mode")

_SETTING_KEY = "context_enabled"
_DEFAULT = True  # full context unless someone deliberately turns it off

_lock = threading.Lock()
_override: bool | None = None


def _db_backed() -> bool:
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return bool(os.getenv("DBRDS_HOST", "").strip())


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return None


def enabled() -> bool:
    """True when turns should carry full context and tools.

    Fails OPEN (to full context) on any lookup error: a settings hiccup must not
    silently turn Hermes into a toy that can't reach the user's apps.
    """
    if _db_backed():
        try:
            from .db import settings as db_settings

            stored = _as_bool(db_settings.get(_SETTING_KEY))
            if stored is not None:
                return stored
        except Exception:  # noqa: BLE001 — fall back rather than break a turn
            log.warning("context_mode lookup failed; defaulting to full context", exc_info=True)
    with _lock:
        return _DEFAULT if _override is None else _override


def set_enabled(value: bool) -> bool:
    """Switch full context on/off platform-wide. Returns the new value."""
    value = bool(value)
    if _db_backed():
        from .db import settings as db_settings

        db_settings.set(_SETTING_KEY, "true" if value else "false")

    global _override
    with _lock:
        _override = value
    log.info("context mode set to %s", "full" if value else "plain chat")
    return value
