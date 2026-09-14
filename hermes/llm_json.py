"""Shared helper for backend calls that expect a single JSON object back —
structured extraction/classification, not the tool-use agent loop.

Provider-agnostic on purpose. This used to talk only to Claude, which quietly
coupled the entire memory layer to one vendor's billing page: memory
extraction, conversation summaries, meeting guidance and Drive classification
all name Config.GUIDANCE_MODEL (a Claude model), so when that account ran out
of credit every one of them stopped — for a month — while chat carried on
answering happily on NVIDIA. A background job pinned to a provider the operator
has already switched away from is a bug, not a configuration choice.

So the model is resolved through models.with_credit() and dispatched to
whichever protocol it speaks. Anthropic and the OpenAI-shaped providers (OpenAI,
NVIDIA NIM) both return the same thing here: parsed JSON.
"""
from __future__ import annotations

import json
import logging
import re

import anthropic
import openai

from . import credits, models
from .config import Config

log = logging.getLogger("hermes.llm_json")

_anthropic_client: anthropic.Anthropic | None = None
_openai_clients: dict[str, openai.OpenAI] = {}


def _anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _openai_compatible(provider: str, model_id: str) -> openai.OpenAI:
    """An OpenAI-protocol client for `provider`, cached by key.

    Deliberately built here rather than imported from hermes.agent: this module
    sits underneath the agent (the agent's own memory layer calls it), and a
    structured-extraction helper has no business depending on the chat loop.
    """
    if provider == "nvidia":
        key, base = Config.nvidia_key_for(model_id), Config.NVIDIA_BASE_URL
    else:
        key, base = Config.OPENAI_API_KEY, Config.OPENAI_BASE_URL
    if not key:
        raise RuntimeError(f"No API key configured for {provider} (model {model_id!r}).")
    # Short timeout and NO retries: these clients only serve background jobs,
    # which move on to the next candidate model rather than waiting out a
    # saturated endpoint. Retrying here would multiply the wait by the retry
    # count before the fallthrough even gets a turn.
    if key not in _openai_clients:
        _openai_clients[key] = openai.OpenAI(
            api_key=key, base_url=base,
            timeout=Config.BACKGROUND_TIMEOUT, max_retries=0,
        )
    return _openai_clients[key]


def _parse(text: str, fallback_key: str) -> dict:
    """Pull the JSON object out of a reply. Tolerates a stray ```json fence
    (models do this despite instructions not to) and any prose around it. On a
    parse failure, degrades to {fallback_key: <raw text>} rather than raising —
    a malformed result is still better than a crashed caller."""
    text = (text or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {fallback_key: text}


def _call_anthropic(model_id: str, system: str, user_message: str, max_tokens: int) -> str:
    resp = _anthropic().messages.create(
        model=model_id,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content
        if getattr(b, "type", None) == "text"
    )


def _call_openai_protocol(
    provider: str, model_id: str, system: str, user_message: str, max_tokens: int
) -> str:
    resp = _openai_compatible(provider, model_id).chat.completions.create(
        model=model_id,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
    )
    return resp.choices[0].message.content or ""


def call_json(
    *, system: str, user_message: str, max_tokens: int, fallback_key: str,
    model: str | None = None,
) -> dict:
    """Ask a model for one JSON object and return it parsed.

    The requested model is used when its provider has credit; otherwise a
    working one is substituted for this call only (the configured preference is
    never rewritten, so it resumes by itself after a top-up).

    Raises credits.CreditsExhausted only when EVERY provider is out. That case
    is deliberately not degraded to a fallback value: every caller is a
    background job whose output gets stored, and inventing an empty result would
    quietly write a wrong record.
    """
    requested = model or Config.GUIDANCE_MODEL
    # Background policy: NVIDIA only. A requested Anthropic/OpenAI model is
    # overridden rather than honoured — see models.background_model().
    candidates = models.background_candidates(preferred=requested)
    if not candidates:
        raise credits.CreditsExhausted(next(iter(models.BACKGROUND_PROVIDERS)))
    if candidates[0] != requested:
        log.info("background JSON call: %s -> %s", requested, candidates[0])

    last_exc: Exception | None = None
    for model_id in candidates:
        provider = models.provider_of(model_id)
        scope = credits.scope_for(provider, model_id)
        try:
            if provider == "anthropic":
                text = _call_anthropic(model_id, system, user_message, max_tokens)
            else:
                text = _call_openai_protocol(
                    provider, model_id, system, user_message, max_tokens
                )
            return _parse(text, fallback_key)
        except (openai.APITimeoutError, openai.RateLimitError, openai.APIConnectionError) as exc:
            # Free-tier NIM saturates and throttles; that says nothing about the
            # next model, which has its own endpoint and its own key.
            last_exc = exc
            log.warning("background JSON call: %s unavailable (%s), trying next.",
                        model_id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not credits.note_failure(scope, exc):
                raise  # a real bug — surface it rather than cycling models
            log.warning("background JSON call: %s out of credit, trying next.", model_id)

    # Every permitted model refused. Deliberately raises rather than returning a
    # fallback value: callers store what they get back, and a fabricated empty
    # result would quietly write a wrong record.
    raise RuntimeError(
        f"No background model could be reached (tried {len(candidates)}): {last_exc}"
    ) from last_exc


#: Historical name — this helper predates being provider-agnostic and is called
#: from four modules. Kept so the rename isn't a flag day.
call_claude_json = call_json
