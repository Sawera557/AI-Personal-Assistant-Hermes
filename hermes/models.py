"""The agent's active LLM: a global, switchable setting (Claude <-> OpenAI).

Hermes can run its tool-use loop on either Anthropic or OpenAI models. Which
one is active is a single platform-wide setting — not per-user or
per-browser — so changing it in the UI changes what every conversation uses
on its next turn. Persisted in Postgres when a database is configured;
otherwise held in a process-global (works until restart, matching the
in-memory fallback hermes.memory already uses when there's no DB).
"""
from __future__ import annotations

import logging
import os
import threading

from .config import Config

log = logging.getLogger("hermes.models")

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
        "id": "claude-opus-5",
        "provider": "anthropic",
        "label": "Claude · Opus 5 (most capable)",
    },
    {
        "id": "claude-opus-4-8",
        "provider": "anthropic",
        "label": "Claude · Opus 4.8 (high capability)",
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

# Per-model price in USD per 1,000,000 tokens. `input` is the uncached prompt
# remainder, `output` the generated tokens, `cache_read` the (cheaper) tokens
# served from Anthropic's prompt cache. Edit these when provider rates change.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
    # Env default (HERMES_MODEL) — priced so a turn on the default never reads as free.
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    # TODO: confirm real rate — gpt-5.4-mini has no published price yet; placeholder.
    "gpt-5.4-mini": {"input": 0.25, "output": 2.0, "cache_read": 0.0},
}
_warned_unpriced: set[str] = set()


def _rates_for(model_id: str) -> dict[str, float] | None:
    """Price for `model_id`: exact match first, then a family fallback so a
    dated/variant id (e.g. 'claude-sonnet-4-5-20250929') still prices sensibly
    instead of silently costing $0."""
    rates = MODEL_PRICING.get(model_id)
    if rates is not None:
        return rates
    m = (model_id or "").lower()
    if "opus" in m:
        return MODEL_PRICING["claude-opus-4-8"]
    if "haiku" in m:
        return MODEL_PRICING["claude-haiku-4-5-20251001"]
    if "sonnet" in m:
        return MODEL_PRICING["claude-sonnet-5"]
    if "4o-mini" in m or "4.1-mini" in m or "o4-mini" in m:
        return MODEL_PRICING["gpt-4o-mini"]
    if m.startswith("gpt-5") and "mini" in m:
        return MODEL_PRICING["gpt-5.4-mini"]
    return None


def price_of(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
) -> float:
    """Cost in USD for one turn's token usage on `model_id`.

    Unknown models cost 0 (warned once) rather than raising — a missing price
    must never break a chat turn.
    """
    rates = _rates_for(model_id)
    if rates is None:
        if model_id not in _warned_unpriced:
            _warned_unpriced.add(model_id)
            log.warning("No price map entry for model %r — cost will show as $0.", model_id)
        return 0.0
    cost = (
        (input_tokens or 0) * rates["input"]
        + (output_tokens or 0) * rates["output"]
        + (cache_read or 0) * rates.get("cache_read", 0.0)
    ) / 1_000_000
    return round(cost, 6)


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
