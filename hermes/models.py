"""The agent's active LLM: a global, switchable setting (Claude <-> OpenAI <-> NVIDIA).

Hermes can run its tool-use loop on Anthropic, OpenAI or NVIDIA NIM models.
Which one is active is a single platform-wide setting — not per-user or
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

# The switchable tiers, cheap -> expensive, across Claude / OpenAI / NVIDIA.
# `id` is the literal model string passed straight to the provider's API.
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
    {
        "id": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "provider": "nvidia",
        "label": "NVIDIA · Nemotron 3.5 Lightning 30B (reasoning, low-cost)",
    },
    {
        # Open-weight GPT-OSS served by NVIDIA NIM — NOT OpenAI's own API. The
        # label says NVIDIA so it can't be mistaken for the gpt-* options above,
        # which bill to OPENAI_API_KEY.
        "id": "openai/gpt-oss-20b",
        "provider": "nvidia",
        "label": "NVIDIA · GPT-OSS 20B (open-weight, reasoning)",
    },
    {
        "id": "google/gemma-4-31b-it",
        "provider": "nvidia",
        "label": "NVIDIA · Gemma 4 31B Instruct (open-weight)",
    },
    {
        "id": "google/diffusiongemma-26b-a4b-it",
        "provider": "nvidia",
        "label": "NVIDIA · DiffusionGemma 26B (reasoning, vision)",
    },
    {
        "id": "moonshotai/kimi-k3",
        "provider": "nvidia",
        "label": "NVIDIA · Kimi K3 (reasoning, vision)",
    },
    # deepseek-ai/deepseek-v4-flash-0731 removed: NVIDIA retired it 2026-09-21
    # (every call now returns 410 Gone).
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
    # TODO: confirm real rate — NVIDIA's hosted NIM endpoint doesn't publish a
    # per-token price for this model; placeholder so a turn never reads as free.
    # NIM has no prompt cache, hence cache_read 0.
    "nvidia/nemotron-3.5-lightning-30b-a3b": {"input": 0.10, "output": 0.40, "cache_read": 0.0},
    # TODO: confirm real rate — same as above, NIM publishes no per-token price.
    "openai/gpt-oss-20b": {"input": 0.05, "output": 0.20, "cache_read": 0.0},
    # TODO: confirm real rate — same as above.
    "google/gemma-4-31b-it": {"input": 0.10, "output": 0.30, "cache_read": 0.0},
    # TODO: confirm real rates — NIM publishes no per-token price for any of
    # these either. Sized by parameter count so bigger models don't read cheaper
    # than smaller ones on the usage page.
    "google/diffusiongemma-26b-a4b-it": {"input": 0.10, "output": 0.30, "cache_read": 0.0},
    "moonshotai/kimi-k3": {"input": 0.60, "output": 2.50, "cache_read": 0.0},
    # Retired 2026-09-21 — no longer selectable; kept so past usage still prices.
    "deepseek-ai/deepseek-v4-flash-0731": {"input": 0.15, "output": 0.60, "cache_read": 0.0},
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
    # Before the gpt-* rules below: gpt-oss is NIM-hosted open-weight and is
    # priced nothing like OpenAI's own gpt-* tiers.
    if "gpt-oss" in m:
        return MODEL_PRICING["openai/gpt-oss-20b"]
    if "4o-mini" in m or "4.1-mini" in m or "o4-mini" in m:
        return MODEL_PRICING["gpt-4o-mini"]
    if m.startswith("gpt-5") and "mini" in m:
        return MODEL_PRICING["gpt-5.4-mini"]
    if "nemotron" in m:
        return MODEL_PRICING["nvidia/nemotron-3.5-lightning-30b-a3b"]
    # Before the plain "gemma" rule: diffusiongemma contains it as a substring.
    if "diffusiongemma" in m:
        return MODEL_PRICING["google/diffusiongemma-26b-a4b-it"]
    if "gemma" in m:
        return MODEL_PRICING["google/gemma-4-31b-it"]
    if "kimi" in m:
        return MODEL_PRICING["moonshotai/kimi-k3"]
    if "deepseek" in m:
        return MODEL_PRICING["deepseek-ai/deepseek-v4-flash-0731"]
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
    """'anthropic', 'openai' or 'nvidia', inferred from a model id string."""
    for m in MODEL_OPTIONS:
        if m["id"] == model_id:
            return m["provider"]
    # Unlisted ids (e.g. a HERMES_MODEL env default) still have to route. NIM
    # ids are namespaced `vendor/model`; OpenAI's own API never namespaces, so
    # even "openai/..." means NIM-hosted open-weight, not api.openai.com.
    if model_id.startswith((
        "nvidia/", "openai/", "google/", "meta/", "mistralai/",
        "qwen/", "deepseek-ai/", "moonshotai/",
    )):
        return "nvidia"
    return "openai" if model_id.startswith(("gpt-", "o1", "o3", "o4")) else "anthropic"


def _configured() -> str:
    """The model id someone asked for: the stored platform setting, else the
    process override, else the env default."""
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


def is_available(model_id: str) -> bool:
    """False when this model's BILLING SCOPE is known to be out of credit.

    Scope, not provider: NIM issues a key per model, so a dry Kimi key must not
    take Nemotron and DeepSeek down with it.
    """
    from . import credits

    return not credits.exhausted(credits.scope_for(provider_of(model_id), model_id))


#: Providers permitted for BACKGROUND work — memory extraction, conversation
#: summaries, meeting guidance, document classification, OCR. Operator policy:
#: background jobs run on NVIDIA NIM only. They fire on every turn without
#: anyone watching, so they must not quietly spend on the metered Anthropic and
#: OpenAI accounts that the interactive chat model is chosen from.
BACKGROUND_PROVIDERS = frozenset({"nvidia"})

#: Ordered background preference, by measured latency and reliability on NIM —
#: these run per-turn, so speed matters more than raw capability:
#:   diffusiongemma      3–9s, stays grounded, also vision-capable. Default
#:                       since deepseek-v4-flash was retired 2026-09-21.
#:   nemotron            NVIDIA's own; 24–35s on meeting guidance (2026-10-06)
#:                       and invented facts when given no records
#:   kimi-k3             ~35s and throttles hard; late resort
#:   gemma-4-31b         measured >145s; last resort only
#:
#: `openai/gpt-oss-20b` is deliberately ABSENT. It is NIM-hosted and billed to
#: the NVIDIA key, but it is an OpenAI-lineage model and the instruction was no
#: OpenAI models in the background. Add it here if that reading is too strict.
BACKGROUND_MODELS: list[str] = [
    "google/diffusiongemma-26b-a4b-it",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "moonshotai/kimi-k3",
    "google/gemma-4-31b-it",
]

#: Background models that can read images (OCR / document vision).
BACKGROUND_VISION_MODELS: list[str] = [
    "google/diffusiongemma-26b-a4b-it",
    "moonshotai/kimi-k3",
    "google/gemma-4-31b-it",
]


def background_model(preferred: str | None = None, vision: bool = False) -> str:
    """The model a background job should use, enforcing BACKGROUND_PROVIDERS.

    `preferred` is honoured only if it is both an allowed provider AND funded —
    so a stale `HERMES_GUIDANCE_MODEL=claude-...` in the environment cannot drag
    background work back onto a metered account. Policy lives here rather than
    in config precisely so it can't be undone by an env var.

    Raises credits.CreditsExhausted when every permitted model is out of credit,
    instead of silently borrowing a forbidden provider.
    """
    from . import credits

    for candidate in background_candidates(preferred=preferred, vision=vision):
        return candidate
    raise credits.CreditsExhausted(next(iter(BACKGROUND_PROVIDERS)))  # all dry


def background_candidates(
    preferred: str | None = None, vision: bool = False
) -> list[str]:
    """Every permitted background model that currently has credit, best first.

    Callers walk this rather than taking only the top entry: NIM's free-tier
    endpoints throttle and time out routinely — measured repeatedly on these
    models — so "the preferred model is unreachable right now" must fall through
    to the next one instead of failing the job. Transient unavailability is the
    dominant failure mode here, not billing.
    """
    candidates = list(BACKGROUND_VISION_MODELS if vision else BACKGROUND_MODELS)
    # `preferred` only reorders the approved list — it can never ADD to it.
    # Checking the provider alone would let `openai/gpt-oss-20b` through, since
    # NIM serves it: allowed provider, excluded model.
    if preferred in candidates:
        candidates = [preferred, *[m for m in candidates if m != preferred]]
    return [m for m in candidates if is_available(m)]


def with_credit(model_id: str) -> str:
    """`model_id`, or a working substitute when its provider is out of credit.

    Used by every caller that names a specific model up front — the agent's
    active model, but also the background jobs pinned to GUIDANCE_MODEL. Those
    jobs are the reason this is a shared helper rather than agent-only logic: a
    Claude-only guidance model meant one empty Anthropic balance silently
    stopped memory extraction even while chat ran happily on NVIDIA.

    Returns `model_id` unchanged when nothing better exists, so the caller still
    fails loudly rather than being handed a silent no-op.
    """
    if is_available(model_id):
        return model_id
    return next(
        (m["id"] for m in MODEL_OPTIONS if m["id"] != model_id and is_available(m["id"])),
        model_id,
    )


def active_model() -> str:
    """The model id the next agent turn (any session) should use.

    Never returns a model whose provider is out of credit when a working one
    exists: that provider fails every call, so defaulting to it would answer
    every conversation with a billing message. The substitution is in-memory
    and expires with the credit state (see hermes.credits), so it reverts by
    itself once the account is topped up — the stored preference is left alone
    and comes back on its own.
    """
    chosen = _configured()
    substitute = with_credit(chosen)
    if substitute != chosen:
        log.warning(
            "%s is out of credit; using %s for now (the stored setting is unchanged).",
            chosen, substitute,
        )
    return substitute


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
    of the listed tiers (e.g. the env-configured default before anyone has
    switched via the UI).

    Each option carries `available`: False means that provider is out of credit,
    so the picker can grey it out instead of letting someone select a model that
    will only ever reply with a billing message.
    """
    current = _configured()
    opts = list(MODEL_OPTIONS)
    if current not in _KNOWN_IDS:
        opts.insert(0, {
            "id": current,
            "provider": provider_of(current),
            "label": f"{current} (current default)",
        })
    return [
        {**o, "available": is_available(o["id"]),
         "label": o["label"] if is_available(o["id"]) else f"{o['label']} — no API credit"}
        for o in opts
    ]
