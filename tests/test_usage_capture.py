"""Per-turn token and model telemetry (TURN_COST_PLAN step 0.5, bug B17).

Every later step of the plan promises a saving; this step is what lets those
savings be checked in production instead of estimated. What must hold:

  * The OpenAI-protocol usage chunk's reasoning and cached counts are read
    (`completion_tokens_details.reasoning_tokens`,
    `prompt_tokens_details.cached_tokens`), tolerating SDK objects, plain dicts
    and missing groups alike.
  * Billing changes in exactly one way: cached prompt tokens are billed at the
    cache-read rate ONLY for models whose rate is confirmed non-zero. On the
    0.0 placeholders (Gemini, NIM) the turn costs what it cost before. Token
    COUNTS (`usage`, hence the footer, ai_runs and the Usage tab) are not
    re-shaped at all — only the price.
  * The `done` result gains `calls` (one per completed provider call, with a
    fallback flag), `breakdown` (the first call's prompt by component, summing
    to its real input count), `reasoning_tokens` and `cached_tokens` — numbers
    and model ids only, never message text.
  * A mid-turn credit fallback shows up as two models, the second flagged.
  * A turn that runs dry after an earlier billed call reports that spend
    instead of zeros (B17).
  * Both stores persist the telemetry: Postgres in message metadata and in
    ai_runs metadata, the in-memory store in its transcript.

The provider REQUEST is untouched by all of this — tests/test_payload_golden.py
is the proof of that; nothing here re-checks it.

Offline: provider clients (or the two turn functions), retrieval, dispatch and
the guardrail are stubbed; Postgres is a fake cursor.
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anthropic
import httpx
import pytest

from hermes import agent, apps_access, context_mode, credits, guardrails, health, models
from hermes.db import conversations as pg
from hermes.memory import ConversationStore
from hermes.turn_state import TELEMETRY_FIELDS

HAIKU = "claude-haiku-4-5-20251001"  # Anthropic; confirmed cache-read rate
GPT = "gpt-4o-mini"  # OpenAI protocol; confirmed cache-read rate (0.075)
KIMI = "moonshotai/kimi-k3"  # OpenAI protocol on NIM; 0.0 placeholder rate
FLASH = "gemini-3.5-flash"  # OpenAI protocol on Gemini; 0.0 placeholder rate

#: What every fake provider reports for the prompt, unless a test says otherwise.
PROMPT_TOKENS = 15_800
COMPLETION_TOKENS = 230
REASONING_TOKENS = 180
CACHED_TOKENS = 12_000

REPLY = "Hello."
USER_TEXT = "hi there"

#: Two small local tools, fixed so the breakdown's tools share is deterministic.
TOOLS = [
    {
        "name": "search_business_knowledge",
        "description": "Search the business knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "create_file",
        "description": "Save a file the user can download.",
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string"}, "content": {"type": "string"}},
            "required": ["filename", "content"],
        },
    },
]


# ---- fake OpenAI-protocol client ------------------------------------------
def _usage_chunk(*, prompt: int, completion: int, reasoning=None, cached=None, as_dict=False):
    """The trailing usage-only chunk `stream_options.include_usage` produces.
    `None` leaves a details group out entirely, as NIM and older models do."""
    details: dict[str, Any] = {}
    if reasoning is not None:
        details["completion_tokens_details"] = (
            {"reasoning_tokens": reasoning} if as_dict
            else types.SimpleNamespace(reasoning_tokens=reasoning)
        )
    if cached is not None:
        details["prompt_tokens_details"] = (
            {"cached_tokens": cached} if as_dict
            else types.SimpleNamespace(cached_tokens=cached)
        )
    usage = types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, **details)
    return types.SimpleNamespace(usage=usage, choices=[])


def _chunks(usage_chunk) -> list[Any]:
    def choice(content, finish):
        return types.SimpleNamespace(
            delta=types.SimpleNamespace(content=content, tool_calls=None),
            finish_reason=finish,
        )

    out = [
        types.SimpleNamespace(usage=None, choices=[choice(REPLY, None)]),
        types.SimpleNamespace(usage=None, choices=[choice(None, "stop")]),
    ]
    if usage_chunk is not None:
        out.append(usage_chunk)
    return out


def _fake_openai_client(usage_chunk, requests: list[dict[str, Any]]):
    def create(**kwargs):
        requests.append(kwargs)
        return iter(_chunks(usage_chunk))

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )


class _FakeAnthropicStream:
    def __init__(self, usage):
        self._usage = usage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield types.SimpleNamespace(type="text", text=REPLY)

    def get_final_message(self):
        return types.SimpleNamespace(
            content=[{"type": "text", "text": REPLY}], stop_reason="end_turn", usage=self._usage,
        )


# ---- a deterministic world ------------------------------------------------
@pytest.fixture
def world(monkeypatch):
    """Pinned retrieval (every block non-empty), switches on, fixed tools, a
    fresh in-memory store and a clean credit state. Returns a setter for the
    configured model."""
    import hermes.context_packs as context_packs
    import hermes.conversation_retrieve as conversation_retrieve
    import hermes.db.conversation_memory as conversation_memory
    import hermes.memory as memory
    import hermes.rag as rag

    credits.clear()
    monkeypatch.setattr(agent, "build_memory_block", lambda **k: "- [owner] Prefers short answers.")
    monkeypatch.setattr(context_packs, "packs_for", lambda *a, **k: "Hard rules: money only from live QuickBooks.")
    monkeypatch.setattr(rag, "build_context_block", lambda **k: "RETRIEVED CONTEXT:\n- [rent.xlsx] Unit 4B due on the 1st.")
    monkeypatch.setattr(
        conversation_retrieve, "build_history_block",
        lambda **k: "EARLIER CONVERSATIONS:\n- 2026-10-01 user: remind me about rent.",
    )
    monkeypatch.setattr(conversation_memory, "owner_user_id", lambda: "telemetry-owner")
    monkeypatch.setattr(agent, "build_tools", lambda *a, **k: list(TOOLS))
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(apps_access, "enabled", lambda: True)
    for flag in ("HERMES_SPLIT_TOGGLES", "HERMES_STANDING_RULES_CORE", "HERMES_PROVIDER_EFFORT"):
        monkeypatch.delenv(flag, raising=False)

    store = ConversationStore()
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(memory, "store", store)

    def use(model: str) -> ConversationStore:
        monkeypatch.setattr(models, "_configured", lambda: model)
        return store

    yield use
    credits.clear()


def _turn(session="telemetry", text=USER_TEXT, **kwargs) -> dict[str, Any]:
    events = list(agent.stream_turn(session, [{"type": "text", "text": text}], **kwargs))
    assert events[-1]["type"] == "done"
    return events[-1]["result"]


# ---- 1. the usage chunk ---------------------------------------------------
@pytest.mark.parametrize("as_dict", [False, True], ids=["sdk-objects", "plain-dicts"])
def test_reasoning_and_cached_tokens_are_read_from_the_usage_chunk(world, monkeypatch, as_dict):
    """A confirmed-rate model (gpt-4o-mini): the cache hits are billed at the
    cache-read rate; the reasoning and cached counts ride along as telemetry.
    The token counts in `usage` are exactly what they were before step 0.5."""
    world(GPT)
    requests: list[dict[str, Any]] = []
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS,
                         reasoning=REASONING_TOKENS, cached=CACHED_TOKENS, as_dict=as_dict)
    monkeypatch.setattr(agent, "_openai", lambda: _fake_openai_client(chunk, requests))

    result = _turn()

    assert result["reply"] == REPLY and len(requests) == 1
    assert result["usage"] == {
        "input_tokens": PROMPT_TOKENS,
        "output_tokens": COMPLETION_TOKENS,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert result["reasoning_tokens"] == REASONING_TOKENS
    assert result["cached_tokens"] == CACHED_TOKENS
    assert result["calls"] == [{
        "model": GPT, "provider": "openai", "input": PROMPT_TOKENS,
        "output": COMPLETION_TOKENS, "reasoning": REASONING_TOKENS,
        "cached": CACHED_TOKENS, "fallback": False,
    }]
    assert result["cost_usd"] == models.price_of(
        GPT, PROMPT_TOKENS - CACHED_TOKENS, COMPLETION_TOKENS, CACHED_TOKENS,
    )
    # ... which is genuinely cheaper than billing every prompt token in full.
    assert result["cost_usd"] < models.price_of(GPT, PROMPT_TOKENS, COMPLETION_TOKENS)


@pytest.mark.parametrize("model, client", [(KIMI, "_nvidia"), (FLASH, "_gemini")])
def test_placeholder_rate_models_are_billed_exactly_as_before(world, monkeypatch, model, client):
    """Gemini and NIM cache-read rates are 0.0 placeholders. Subtracting their
    cached tokens would bill them at a made-up $0 — so they are NOT moved:
    the cost is today's, and the cached count is reported, not billed."""
    world(model)
    requests: list[dict[str, Any]] = []
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS,
                         reasoning=REASONING_TOKENS, cached=CACHED_TOKENS)
    fake = _fake_openai_client(chunk, requests)
    monkeypatch.setattr(agent, client, (lambda *a: fake))

    result = _turn()

    assert result["usage"]["input_tokens"] == PROMPT_TOKENS
    assert result["usage"]["cache_read_input_tokens"] == 0
    assert result["cost_usd"] == models.price_of(model, PROMPT_TOKENS, COMPLETION_TOKENS)
    assert result["cached_tokens"] == CACHED_TOKENS
    assert result["reasoning_tokens"] == REASONING_TOKENS
    assert result["calls"][0]["input"] == PROMPT_TOKENS


def test_a_usage_chunk_without_details_reads_as_zero(world, monkeypatch):
    world(KIMI)
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS)
    monkeypatch.setattr(agent, "_nvidia", lambda *a: _fake_openai_client(chunk, []))

    result = _turn()

    assert result["reasoning_tokens"] == 0 and result["cached_tokens"] == 0
    assert result["usage"]["input_tokens"] == PROMPT_TOKENS
    assert result["cost_usd"] == models.price_of(KIMI, PROMPT_TOKENS, COMPLETION_TOKENS)


def test_usage_detail_tolerates_every_shape():
    ns = types.SimpleNamespace
    assert agent._usage_detail(ns(), "prompt_tokens_details", "cached_tokens") == 0
    assert agent._usage_detail(ns(prompt_tokens_details=None), "prompt_tokens_details", "cached_tokens") == 0
    assert agent._usage_detail(ns(prompt_tokens_details=ns(cached_tokens=None)),
                               "prompt_tokens_details", "cached_tokens") == 0
    assert agent._usage_detail(ns(prompt_tokens_details={"cached_tokens": "7"}),
                               "prompt_tokens_details", "cached_tokens") == 7
    assert agent._usage_detail({"prompt_tokens_details": {"cached_tokens": 5}},
                               "prompt_tokens_details", "cached_tokens") == 5
    assert agent._usage_detail(ns(prompt_tokens_details=ns(cached_tokens="junk")),
                               "prompt_tokens_details", "cached_tokens") == 0
    assert agent._usage_detail(ns(prompt_tokens_details=ns(cached_tokens=-3)),
                               "prompt_tokens_details", "cached_tokens") == 0


def test_cached_tokens_never_make_the_uncached_remainder_negative(world, monkeypatch):
    world(GPT)
    chunk = _usage_chunk(prompt=100, completion=5, cached=500)  # a misbehaving server
    monkeypatch.setattr(agent, "_openai", lambda: _fake_openai_client(chunk, []))

    result = _turn()

    assert result["usage"]["input_tokens"] == 100
    assert result["calls"][0]["input"] == 100
    assert agent._billed_usage(GPT, {"input_tokens": 100, "output_tokens": 5,
                                     "cached_tokens": 500}) == (0, 5, 100)
    assert result["cost_usd"] == models.price_of(GPT, 0, 5, 100)


def test_billed_usage_is_todays_math_without_a_cached_count():
    """Anthropic usage (no cached_tokens key) and every test fake price exactly
    as before: input at the input rate, cache reads at the cache-read rate."""
    usage = {"input_tokens": 3_000, "output_tokens": 40,
             "cache_read_input_tokens": 9_000, "cache_creation_input_tokens": 500}
    assert agent._billed_usage(HAIKU, usage) == (3_000, 40, 9_000)
    assert agent._billed_usage(KIMI, {"input_tokens": 7, "output_tokens": 1}) == (7, 1, 0)
    assert agent._billed_usage(KIMI, {"input_tokens": 7, "output_tokens": 1,
                                      "cached_tokens": 5}) == (7, 1, 0)


def test_anthropic_cache_reads_are_reported_as_cached(world, monkeypatch):
    """Anthropic already reports the split; `input` adds it back up to the
    whole prompt, and its cache reads are the call's cached tokens."""
    world(HAIKU)
    usage = types.SimpleNamespace(input_tokens=3_000, output_tokens=40,
                                  cache_read_input_tokens=9_000, cache_creation_input_tokens=500)
    client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **k: _FakeAnthropicStream(usage))
    )
    monkeypatch.setattr(agent, "_anthropic", lambda: client)

    result = _turn()

    assert result["calls"] == [{
        "model": HAIKU, "provider": "anthropic", "input": 12_500, "output": 40,
        "reasoning": 0, "cached": 9_000, "fallback": False,
    }]
    assert result["cached_tokens"] == 9_000 and result["reasoning_tokens"] == 0
    assert result["cost_usd"] == models.price_of(HAIKU, 3_000, 40, 9_000)


def test_only_confirmed_cache_rates_bill_cache_reads():
    assert models.bills_cache_reads(GPT)
    assert models.bills_cache_reads(HAIKU)
    assert models.bills_cache_reads("claude-sonnet-5")
    for placeholder in (KIMI, FLASH, "gemini-3.5-flash-lite", "gpt-5.4-mini",
                        "nvidia/nemotron-3.5-lightning-30b-a3b", "some-unknown-model"):
        assert not models.bills_cache_reads(placeholder), placeholder


# ---- 2. the breakdown -----------------------------------------------------
@pytest.mark.parametrize("model, client", [(KIMI, "_nvidia"), (HAIKU, "_anthropic")])
def test_breakdown_sums_to_the_first_calls_real_input(world, monkeypatch, model, client):
    """Every component is present, the ones this turn really carried are
    non-zero, and together they account for the provider's own input count
    (the plan's bar is ±15%; the scaling makes it exact)."""
    world(model)
    if client == "_anthropic":
        usage = types.SimpleNamespace(input_tokens=PROMPT_TOKENS, output_tokens=5,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake = types.SimpleNamespace(
            messages=types.SimpleNamespace(stream=lambda **k: _FakeAnthropicStream(usage))
        )
        monkeypatch.setattr(agent, "_anthropic", lambda: fake)
    else:
        chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=5)
        monkeypatch.setattr(agent, "_nvidia", lambda *a: _fake_openai_client(chunk, []))

    result = _turn(active_business="nh")
    breakdown = result["breakdown"]

    assert set(breakdown) == set(agent._BREAKDOWN_KEYS)
    assert all(isinstance(v, int) and v >= 0 for v in breakdown.values())
    assert abs(sum(breakdown.values()) - PROMPT_TOKENS) <= 0.15 * PROMPT_TOKENS
    assert sum(breakdown.values()) == PROMPT_TOKENS
    for carried in ("system_base", "scope", "pack_core", "memory", "recall", "rag",
                    "tools", "history"):
        assert breakdown[carried] > 0, carried
    # Not on this turn: no voice, no files shared.
    assert breakdown["voice"] == 0 and breakdown["ledger"] == 0


def test_breakdown_estimate_measures_each_component_exactly(world):
    """Before scaling: chars/4 per component, where each system-prompt piece is
    the prompt minus the prompt without it — so the pieces add back up to the
    whole system prompt, and the voice suffix is exactly _VOICE_STYLE."""
    world(KIMI)
    args = dict(
        voice=True, active_business="nh", memory_block="- [owner] Prefers short answers.",
        attachments=[{"filename": "lease.pdf", "s3_uri": "s3://bucket/lease.pdf"}],
        context_pack="Hard rules: money only from live QuickBooks.",
        retrieved_context="RETRIEVED CONTEXT:\n- x", conversation_block="EARLIER CONVERSATIONS:\n- y",
        tools_available=True, rich_context=True, standing_rules="", standing_memories="",
        tools_off_by="context",
    )
    system = agent._system_prompt(**args)
    sent = [{"role": "user", "content": [{"type": "text", "text": USER_TEXT}]}]

    est = agent._breakdown_estimate(args, system, TOOLS, sent, "nvidia")

    system_parts = sum(v for k, v in est.items() if k not in ("tools", "history"))
    assert abs(system_parts - len(system) / 4) <= len(agent._BREAKDOWN_KEYS)
    assert est["voice"] == round(len(agent._VOICE_STYLE) / 4)
    assert est["ledger"] > 0 and est["scope"] > 0
    assert est["tools"] == round(len(json.dumps(agent._to_openai_tools(TOOLS), ensure_ascii=False)) / 4)
    assert est["history"] == round(len(json.dumps(sent[0]["content"][0], ensure_ascii=False)) / 4)


def test_base64_media_is_not_charged_by_its_text_length():
    huge = "A" * 2_000_000
    sent = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": huge}},
        {"type": "text", "text": "what is this?"},
    ]}]
    assert agent._history_chars(sent) < 10_000


def test_breakdown_without_real_usage_is_the_unscaled_estimate(world, monkeypatch):
    """A provider that sends no usage chunk: nothing to scale to, so the raw
    chars/4 estimate is kept rather than a made-up total."""
    world(KIMI)
    monkeypatch.setattr(agent, "_nvidia", lambda *a: _fake_openai_client(None, []))

    result = _turn()

    assert result["calls"][0]["input"] == 0
    assert 0 < sum(result["breakdown"].values()) < PROMPT_TOKENS


def test_scale_breakdown_keeps_shares_and_hits_the_total_exactly():
    est = {"a": 3, "b": 3, "c": 3, "d": 0}
    scaled = agent._scale_breakdown(est, 10)
    assert sum(scaled.values()) == 10 and scaled["d"] == 0
    assert sorted(scaled.values()) == [0, 3, 3, 4]
    assert agent._scale_breakdown(est, 0) == est
    assert agent._scale_breakdown({}, 500) == {}


def test_a_failing_estimate_never_breaks_the_turn(world, monkeypatch):
    world(KIMI)
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=5)
    monkeypatch.setattr(agent, "_nvidia", lambda *a: _fake_openai_client(chunk, []))
    monkeypatch.setattr(agent, "_history_chars", lambda *_: 1 / 0)

    result = _turn()

    assert result["reply"] == REPLY
    assert result["breakdown"] == {}
    assert result["calls"][0]["input"] == PROMPT_TOKENS


# ---- 3. calls and the credit fallback --------------------------------------
def _anthropic_credit_error():
    return anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
        body=None,
    )


@pytest.fixture
def scripted(monkeypatch):
    """Turn functions stubbed at `_anthropic_turn` / `_openai_turn`, dispatch and
    the guardrail allowing a harmless read tool, retrieval empty."""
    credits.clear()
    store = ConversationStore()
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(apps_access, "enabled", lambda: True)
    monkeypatch.setattr(agent, "build_tools", lambda *a, **k: list(TOOLS))
    monkeypatch.setattr(health, "guarded", lambda signal, fn, default="": default)
    monkeypatch.setattr(agent, "dispatch", lambda *a, **k: ('{"results": []}', False, None, None))
    monkeypatch.setattr(
        guardrails, "check", lambda *a, **k: guardrails.Decision(True, "", None, "allowed"),
    )
    yield monkeypatch
    credits.clear()


def _tool_round(model_id):
    yield {
        "type": "_final",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "search_business_knowledge",
                     "input": {"query": "rent"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 9_000, "output_tokens": 30,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    }


def test_a_mid_turn_credit_fallback_records_both_models(scripted):
    """Haiku runs the first (tool) round, then its account runs dry; the turn
    is finished on the next funded model. Both calls are recorded, and the
    one the fallback served says so — fact 7 made visible."""
    scripted.setattr(models, "_configured", lambda: HAIKU)
    anthropic_calls = []

    def fake_anthropic(model_id, tools, system, messages, *a, **k):
        anthropic_calls.append(model_id)
        if len(anthropic_calls) == 1:
            yield from _tool_round(model_id)
            return
        credits.check("anthropic")
        raise _anthropic_credit_error()

    def fake_openai(model_id, tools, system, messages, *a, **k):
        yield {"type": "text", "text": "No rent is overdue."}
        yield {
            "type": "_final",
            "content": [{"type": "text", "text": "No rent is overdue."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9_100, "output_tokens": 12,
                      "reasoning_tokens": 4, "cached_tokens": 8_000},
        }

    scripted.setattr(agent, "_anthropic_turn", fake_anthropic)
    scripted.setattr(agent, "_openai_turn", fake_openai)

    result = _turn()

    assert result["reply"] == "No rent is overdue."
    # Every Claude tier shares the dry Anthropic account; OpenAI is next.
    assert result["model_used"] == GPT
    assert [(c["model"], c["fallback"]) for c in result["calls"]] == [
        (HAIKU, False), (GPT, True),
    ]
    assert len({c["model"] for c in result["calls"]}) == 2
    assert result["reasoning_tokens"] == 4 and result["cached_tokens"] == 8_000
    # Each call billed at its own model's rates — gpt-4o-mini's cache hits at
    # its confirmed cache-read rate — and the token totals add up.
    assert result["usage"]["input_tokens"] == 9_000 + 9_100
    assert result["cost_usd"] == pytest.approx(
        models.price_of(HAIKU, 9_000, 30) + models.price_of(GPT, 1_100, 12, 8_000)
    )


def test_a_model_benched_before_the_turn_is_flagged_as_a_fallback(scripted):
    """The quiet case of fact 7: the configured model was already out of
    credit, so the turn STARTS on a substitute. Its call is a fallback too."""
    scripted.setattr(models, "_configured", lambda: HAIKU)
    credits.note_failure("anthropic", _anthropic_credit_error())

    def fake_openai(model_id, tools, system, messages, *a, **k):
        yield {"type": "_final", "content": [{"type": "text", "text": "Hi!"}],
               "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 2}}

    scripted.setattr(agent, "_openai_turn", fake_openai)

    result = _turn()

    assert result["model_used"] != HAIKU
    assert [c["fallback"] for c in result["calls"]] == [True]


def test_credits_result_carries_the_spend_before_running_dry(scripted):
    """B17: one billed tool round on Haiku, then every provider is dry. The
    billing notice must report that first call's tokens and cost, not zeros."""
    scripted.setattr(models, "_configured", lambda: HAIKU)
    anthropic_calls = []

    def fake_anthropic(model_id, tools, system, messages, *a, **k):
        anthropic_calls.append(model_id)
        if len(anthropic_calls) == 1:
            yield from _tool_round(model_id)
            return
        credits.check("anthropic")
        raise _anthropic_credit_error()

    def all_dry(model_id, tools, system, messages, *a, **k):
        raise RuntimeError("402 Payment Required: insufficient credits")
        yield  # pragma: no cover — a generator, like the real one

    scripted.setattr(agent, "_anthropic_turn", fake_anthropic)
    scripted.setattr(agent, "_openai_turn", all_dry)

    result = _turn()

    assert result["stop_reason"] == "credits_exhausted"
    assert result["usage"] == {
        "input_tokens": 9_000, "output_tokens": 30,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    assert result["cost_usd"] == models.price_of(HAIKU, 9_000, 30) > 0
    assert [c["model"] for c in result["calls"]] == [HAIKU]
    assert set(result["breakdown"]) == set(agent._BREAKDOWN_KEYS)
    assert sum(result["breakdown"].values()) == 9_000
    assert [t["tool"] for t in result["tool_calls"]] == ["search_business_knowledge"]


def test_credits_result_on_the_first_call_still_reads_zero(scripted):
    scripted.setattr(models, "_configured", lambda: HAIKU)

    def dry(model_id, tools, system, messages, *a, **k):
        raise RuntimeError("402 Payment Required: insufficient credits")
        yield  # pragma: no cover

    scripted.setattr(agent, "_anthropic_turn", dry)
    scripted.setattr(agent, "_openai_turn", dry)

    result = _turn()

    assert result["stop_reason"] == "credits_exhausted"
    assert result["cost_usd"] == 0.0
    assert set(result["usage"].values()) == {0}
    assert result["calls"] == []
    assert result["reasoning_tokens"] == 0 and result["cached_tokens"] == 0
    # No call answered, so the estimate stands unscaled — still useful.
    assert set(result["breakdown"]) == set(agent._BREAKDOWN_KEYS)


def test_credits_result_defaults_are_unchanged_for_other_callers():
    done = agent._credits_result("anthropic", HAIKU, [])
    result = done["result"]
    assert result["cost_usd"] == 0.0 and set(result["usage"].values()) == {0}
    assert result["calls"] == [] and result["breakdown"] == {}


# ---- 4. persistence --------------------------------------------------------
def _telemetry_result() -> dict[str, Any]:
    return {
        "reply": "Two tenants are late.", "thinking": None, "tool_calls": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 15_800, "output_tokens": 230,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "cost_usd": 0.001234, "model_used": GPT,
        "calls": [{"model": GPT, "provider": "openai", "input": 15_800, "output": 230,
                   "reasoning": 180, "cached": 12_000, "fallback": False}],
        "breakdown": dict.fromkeys(agent._BREAKDOWN_KEYS, 1580),
        "reasoning_tokens": 180, "cached_tokens": 12_000,
    }


def test_in_memory_transcript_keeps_the_telemetry():
    store = ConversationStore()
    store.record_turn("k", None, "k", "rent status?", _telemetry_result())
    store.record_turn("k", None, "k", "hi", {"reply": "hello"})  # a pre-0.5 result
    full, plain = [t for t in store.transcript("k") if t["role"] == "assistant"]

    expected = _telemetry_result()
    for field in (*TELEMETRY_FIELDS, "usage", "cost_usd", "model_used"):
        assert full[field] == expected[field], field
    for field in (*TELEMETRY_FIELDS, "usage", "cost_usd", "model_used"):
        assert field not in plain, field


class _FakeCursorDB:
    """Just enough of Postgres for record_turn, _record_usage and transcript."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.ai_runs: list[dict[str, Any]] = []

    @contextmanager
    def cursor(self, commit: bool = True):
        yield self

    def execute(self, sql: str, params: tuple = ()) -> None:
        s = " ".join(sql.split())
        self._result: list[tuple] = []
        if s.startswith("SELECT user_id FROM users"):
            self._result = [("00000000-0000-0000-0000-00000000000a",)]
        elif s.startswith("INSERT INTO hermes_sessions"):
            pass
        elif s.startswith("SELECT COALESCE(max(seq), 0)"):
            self._result = [(len(self.messages),)]
        elif s.startswith("INSERT INTO hermes_messages") and "'user'" in s:
            _sid, _ent, content, seq = params
            self.messages.append({"role": "user", "content": content, "seq": seq,
                                  "metadata": {}, "model_used": None})
        elif s.startswith("INSERT INTO hermes_messages") and "'assistant'" in s:
            _sid, _ent, content, seq, model, meta = params
            self.messages.append({"role": "assistant", "content": content, "seq": seq,
                                  "metadata": json.loads(meta), "model_used": model})
        elif s.startswith("INSERT INTO ai_runs"):
            _ent, _sid, _owner, model, tin, tout, meta = params
            self.ai_runs.append({"model": model, "input_tokens": tin, "output_tokens": tout,
                                 "metadata": json.loads(meta)})
        elif s.startswith("SELECT role, content, metadata, model_used FROM hermes_messages"):
            self._result = [(m["role"], m["content"], m["metadata"], m["model_used"])
                            for m in sorted(self.messages, key=lambda m: m["seq"])]
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def test_postgres_persists_telemetry_in_message_and_ai_runs_metadata(monkeypatch):
    db = _FakeCursorDB()
    monkeypatch.setattr(pg, "cursor", db.cursor)
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    expected = _telemetry_result()

    store.record_turn(sid, None, sid, "rent status?", _telemetry_result())

    meta = [m for m in db.messages if m["role"] == "assistant"][0]["metadata"]
    run = db.ai_runs[0]["metadata"]
    for field in TELEMETRY_FIELDS:
        assert meta[field] == expected[field], field
        assert run[field] == expected[field], field
    # The pre-0.5 ai_runs keys are all still there, unchanged.
    assert run["cost_usd"] == expected["cost_usd"]
    assert run["cache_read_tokens"] == 0 and run["cache_creation_tokens"] == 0
    assert db.ai_runs[0]["input_tokens"] == 15_800

    assistant = [t for t in store.transcript(sid) if t["role"] == "assistant"][0]
    for field in TELEMETRY_FIELDS:
        assert assistant[field] == expected[field], field


def test_postgres_rows_without_telemetry_are_unchanged(monkeypatch):
    """A result from before step 0.5 writes exactly the metadata it always did."""
    db = _FakeCursorDB()
    monkeypatch.setattr(pg, "cursor", db.cursor)
    store = pg.PostgresConversationStore()
    sid = str(uuid.uuid4())
    old = {k: v for k, v in _telemetry_result().items() if k not in TELEMETRY_FIELDS}

    store.record_turn(sid, None, sid, "rent status?", old)

    meta = [m for m in db.messages if m["role"] == "assistant"][0]["metadata"]
    assert set(meta) == {"stop_reason", "usage", "cost_usd"}
    assert set(db.ai_runs[0]["metadata"]) == {"cost_usd", "cache_read_tokens", "cache_creation_tokens"}
    assistant = [t for t in store.transcript(sid) if t["role"] == "assistant"][0]
    assert all(assistant[field] is None for field in TELEMETRY_FIELDS)


def test_telemetry_never_carries_message_text(world, monkeypatch):
    """Numbers and model ids only: neither the user's words nor the reply (nor
    any retrieved block) may leak into what is persisted as telemetry."""
    world(KIMI)
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=5, reasoning=2, cached=10)
    monkeypatch.setattr(agent, "_nvidia", lambda *a: _fake_openai_client(chunk, []))
    secret = "the tenant in unit 4B owes 1234"

    result = _turn(text=secret, active_business="nh")
    telemetry = json.dumps({field: result[field] for field in TELEMETRY_FIELDS})

    for leaked in (secret, REPLY, "QuickBooks", "Unit 4B", "remind me"):
        assert leaked not in telemetry, leaked


def test_a_real_turn_round_trips_through_the_in_memory_store(world, monkeypatch):
    store = world(GPT)
    chunk = _usage_chunk(prompt=PROMPT_TOKENS, completion=COMPLETION_TOKENS,
                         reasoning=REASONING_TOKENS, cached=CACHED_TOKENS)
    monkeypatch.setattr(agent, "_openai", lambda: _fake_openai_client(chunk, []))

    result = _turn(session="round-trip")
    store.record_turn("round-trip", None, "round-trip", USER_TEXT, result)
    entry = [t for t in store.transcript("round-trip") if t["role"] == "assistant"][0]

    assert entry["calls"] == result["calls"] and entry["breakdown"] == result["breakdown"]
    assert entry["reasoning_tokens"] == REASONING_TOKENS
    assert entry["cached_tokens"] == CACHED_TOKENS
    assert entry["cost_usd"] == result["cost_usd"] and entry["model_used"] == GPT
