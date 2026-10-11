"""One platform-wide boolean, stored the way hermes.models stores the active
model: Postgres `hermes_settings` when a database is configured, otherwise a
process-global that lasts until restart.

Extracted so that the two switches that share this exact shape —
hermes.context_mode (business context) and hermes.apps_access (tools/apps) —
are one implementation rather than two copies that can drift apart.

Every flag fails OPEN: on any lookup error `enabled()` returns the flag's
default, which for both current flags is True. A settings hiccup must never
silently strip capability from a turn and leave the user guessing why.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("hermes.settings_flag")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _db_backed() -> bool:
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in _TRUE:
        return False
    return bool(os.getenv("DBRDS_HOST", "").strip())


def as_bool(value: str | None) -> bool | None:
    """Parse a stored/env string into a bool, or None when it says nothing."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


class Flag:
    """A named on/off switch shared by every worker and conversation."""

    def __init__(self, key: str, default: bool, label: str = ""):
        self.key = key
        self.default = default
        self.label = label or key
        self._lock = threading.Lock()
        self._override: bool | None = None

    def enabled(self) -> bool:
        if _db_backed():
            try:
                from .db import settings as db_settings

                stored = as_bool(db_settings.get(self.key))
                if stored is not None:
                    return stored
            except Exception:  # noqa: BLE001 — fall back rather than break a turn
                log.warning(
                    "%s lookup failed; defaulting to %s", self.key, self.default, exc_info=True
                )
        with self._lock:
            return self.default if self._override is None else self._override

    def set_enabled(self, value: bool) -> bool:
        """Switch it on/off platform-wide. Returns the new value."""
        value = bool(value)
        if _db_backed():
            from .db import settings as db_settings

            db_settings.set(self.key, "true" if value else "false")

        with self._lock:
            self._override = value
        log.info("%s set to %s", self.label, "on" if value else "off")
        return value

    def reset(self) -> None:
        """Drop the process-local override (tests; not a user-facing action)."""
        with self._lock:
            self._override = None
