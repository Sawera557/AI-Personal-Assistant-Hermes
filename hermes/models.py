"""The agent's active LLM: a global, switchable setting (Claude <-> OpenAI).

Hermes can run its tool-use loop on either Anthropic or OpenAI models. Which
one is active is a single platform-wide setting — not per-user or
per-browser — so changing it in the UI changes what every conversation uses
on its next turn. Persisted in Postgres when a database is configured;
otherwise held in a process-global (works until restart, matching the
in-memory fallback hermes.memory already uses when there's no DB).
"""
from __future__ import annotations

import os
import threading

from .config import Config

_SETTING_KEY = "active_model"

# The four switchable tiers: cheap/expensive x Claude/OpenAI. `id` is the
# literal model string passed straight to the provider's API.
MODEL_OPTIONS: list[dict[str, str]] = [
    {
        "id": "claude-haiku-4-5-20251001",
        "provider": "anthropic",
        "label": "Claude · Haiku 4.5 (fast, low-cost)",
    },
    {
        "id": "claude-opus-4-8",
        "provider": "anthropic",
        "label": "Claude · Opus 4.8 (most capable)",
    },
    {
        "id": "gpt-4o-mini",
        "provider": "openai",
        "label": "OpenAI · GPT-4o mini (fast, low-cost)",
    },
    {
        "id": "gpt-5.4-mini",
        "provider": "openai",
        "label": "OpenAI · GPT-5.4-mini (faster, cost-efficient)",
    },
]
_KNOWN_IDS = {m["id"] for m in MODEL_OPTIONS}

_lock = threading.Lock()
_override: str | None = None  # process-global fallback when there's no DB


def _db_backed() -> bool:
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return bool(os.getenv("DBRDS_HOST", "").strip())


def provider_of(model_id: str) -> str:
    """'anthropic' or 'openai', inferred from a model id string."""
    for m in MODEL_OPTIONS:
        if m["id"] == model_id:
            return m["provider"]
    return "openai" if model_id.startswith(("gpt-", "o1", "o3", "o4")) else "anthropic"


def active_model() -> str:
    """The model id the next agent turn (any session) should use."""
    if _db_backed():
        try:
            from .db import settings as db_settings

            value = db_settings.get(_SETTING_KEY)
            if value:
                return value
        except Exception:  # noqa: BLE001 — fall back rather than break chat
            pass
    with _lock:
        return _override or Config.MODEL


def set_active_model(model_id: str) -> None:
    """Change the platform-wide active model. Raises ValueError if unknown."""
    model_id = (model_id or "").strip()
    if model_id not in _KNOWN_IDS:
        raise ValueError(f"Unknown model: {model_id!r}")

    if _db_backed():
        from .db import settings as db_settings

        db_settings.set(_SETTING_KEY, model_id)

    global _override
    with _lock:
        _override = model_id


def options() -> list[dict[str, str]]:
    """The picker's options, prefixed with the current model if it isn't one
    of the four tiers (e.g. the env-configured default before anyone has
    switched via the UI)."""
    current = active_model()
    if current in _KNOWN_IDS:
        return list(MODEL_OPTIONS)
    extra = {"id": current, "provider": provider_of(current), "label": f"{current} (current default)"}
    return [extra, *MODEL_OPTIONS]
