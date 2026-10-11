"""A turn whose model is out of credit must be answered by the next funded
model in the SAME request.

The regression this guards: the fallback used to be lazy. With Anthropic and
OpenAI both dry (seen 2026-10-07), the chain Claude → gpt-4o-mini → NVIDIA cost
the user two turns of "API credits are out" after every restart and every
credits.TTL_SECONDS, each one also written into the conversation history.
Offline — all provider calls are stubbed.

The chain itself is policy in hermes/models.py (`_fallback_order`): Claude,
then OpenAI, then Gemini — ranked ahead of NIM on 2026-10-07, when it answered a
tool call in 1.4s while the free NIM endpoints were timing out — then NIM.
"""
import os
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anthropic
import httpx
import pytest

from hermes import agent, context_mode, credits, models

CONFIGURED = "claude-haiku-4-5-20251001"


def _anthropic_credit_error():
    return anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
        body=None,
    )


class _Store:
    def __init__(self):
        self.saved = {}

    def get(self, sid):
        return list(self.saved.get(sid, []))

    def set(self, sid, messages):
        self.saved[sid] = list(messages)

    def attachments(self, sid):
        return []


@pytest.fixture
def env(monkeypatch):
    credits.clear()
    store = _Store()
    calls = []
    monkeypatch.setattr(models, "_configured", lambda: CONFIGURED)
    monkeypatch.setattr(context_mode, "enabled", lambda: False)
    monkeypatch.setattr(agent, "_system_prompt", lambda *a, **k: "system")
    monkeypatch.setattr(agent, "store", store)

    def fake_anthropic(model_id, tools, system, messages, *a, **k):
        calls.append(("anthropic", model_id))
        credits.check("anthropic")
        raise _anthropic_credit_error()
        yield  # pragma: no cover — makes this a generator, like the real one

    monkeypatch.setattr(agent, "_anthropic_turn", fake_anthropic)
    yield store, calls
    credits.clear()


def _nvidia_answers(calls, dry_nvidia=False):
    """Fake `_openai_turn`: OpenAI is out of quota; every other OpenAI-protocol
    provider (Gemini, then NIM) answers — or, with `dry_nvidia`, is dry too.

    `**kwargs` keeps it call-compatible as `_openai_turn` grows keyword-only
    knobs (effort/light, TURN_COST_PLAN step 0.4); a fake that rejected them
    would fail the turn for a reason that has nothing to do with credits."""
    def fake_openai(model_id, tools, system, messages, max_tokens, continuing, provider, **kwargs):
        calls.append((provider, model_id))
        if provider == "openai":
            raise RuntimeError("Error code: 429 - insufficient_quota: You exceeded your current quota")
        if dry_nvidia:
            raise RuntimeError("402 Payment Required: insufficient credits")
        yield {"type": "text", "text": "12 × 8 = 96."}
        yield {
            "type": "_final",
            "content": [{"type": "text", "text": "12 × 8 = 96."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 20},
        }

    return fake_openai


def test_dry_providers_fall_through_to_a_funded_model_in_one_call(env, monkeypatch):
    store, calls = env
    monkeypatch.setattr(agent, "_openai_turn", _nvidia_answers(calls))

    result = agent.run_turn("s1", "What is 12 times 8?")

    assert result["reply"] == "12 × 8 = 96."
    assert result["stop_reason"] == "end_turn"
    # Claude → OpenAI → Gemini: the first funded model after two dry ones is
    # Gemini, which models._fallback_order deliberately ranks ahead of NIM.
    assert [p for p, _ in calls] == ["anthropic", "openai", "gemini"]
    assert models.provider_of(result["model_used"]) == "gemini"
    # Billed at the rates of the model that answered — not the one that failed.
    assert result["cost_usd"] == models.price_of(result["model_used"], 1000, 20)
    # The failed attempts leave no trace in the conversation.
    assert [m["role"] for m in store.saved["s1"]] == ["user", "assistant"]
    # The stored preference is untouched; substitution is per-call.
    assert models._configured() == CONFIGURED


def test_every_provider_dry_gives_the_billing_notice_without_looping(env, monkeypatch):
    store, calls = env
    monkeypatch.setattr(agent, "_openai_turn", _nvidia_answers(calls, dry_nvidia=True))

    result = agent.run_turn("s2", "hello")

    assert result["stop_reason"] == "credits_exhausted"
    assert "API credits are out" in result["reply"]
    # One attempt per distinct model at most — bounded, never a retry loop.
    assert len(calls) <= len(models.MODEL_OPTIONS)
    assert len(set(calls)) == len(calls)


def test_no_retry_once_output_has_streamed(env, monkeypatch):
    """A reply the user has already started reading can't be silently redone on
    another model — that would show two different answers."""
    store, calls = env
    credits.note_failure("anthropic", _anthropic_credit_error())  # start on OpenAI

    def streams_then_dies(model_id, tools, system, messages, max_tokens, continuing, provider, **kwargs):
        calls.append((provider, model_id))
        yield {"type": "text", "text": "Partial"}
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(agent, "_openai_turn", streams_then_dies)

    result = agent.run_turn("s3", "hello")

    assert result["stop_reason"] == "credits_exhausted"
    assert len(calls) == 1
