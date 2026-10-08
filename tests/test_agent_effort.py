"""Reasoning-effort plumbing and the route-gated effort matcher.

TURN_COST_PLAN step 0.4. Two independent pieces, both shipped with NO change to
any default request:

  * Provider knobs. `_anthropic_turn(..., light=)` and
    `_openai_turn(..., effort=, light=)`. `light` asks for a reply with no
    reasoning: Claude drops `thinking` / `output_config`, NIM's boolean
    thinking toggles go False and `reasoning_budget` 0 — the values every
    continuation call already sends, so they need no flag. The UNVERIFIED
    per-model `reasoning_effort` values in `agent._REASONING` go out only on a
    light call while HERMES_PROVIDER_EFFORT is on. A provider that 400s on a
    knob puts the model in `_NO_REASONING_KNOB` and the call is retried once
    with today's request.
  * The `_auto_effort` matcher fixes ("Hi!" → low, "good morning" → low,
    "explanation" / "remodel" no longer "high"), applied only under
    route="auto". route="full" — the default, and what every unattended caller
    gets — is today's matcher unchanged, including its quirks.

What must hold, and what each section below proves:

  * With the defaults (effort None, light False) every model's request is
    byte-identical to the pre-0.4 request — and passing an effort level, or
    turning the flag on, changes nothing for a call that is not light. The
    expected requests are written out here by hand from the pre-0.4 code, so
    they are an independent statement of "today", not a re-run of the code
    under test. tests/test_payload_golden.py proves the same end to end.
  * light and light+flag send exactly the documented values, per model.
  * The 400 fallback retries once, only for a knob-bearing request, only for a
    knob-shaped complaint, and never shows the user two attempts.
  * route auto vs full give the outputs the plan lists, and the real
    meeting-notes prompt keeps today's effort.

Offline: the provider CLIENT factories are stubbed (as in the golden test), so
the kwargs `_anthropic_turn` / `_openai_turn` build are what is recorded.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import openai
import pytest

from hermes import agent, apps_access, context_mode, credits, health, models
from hermes.config import Config
from hermes.memory import ConversationStore

SYSTEM = "SYSTEM PROMPT"
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
TOOL = {
    "name": "search_business_knowledge",
    "description": "Search the business knowledge base.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}
REPLY = "Hello."
BUDGET = 16384  # NVIDIA_REASONING_BUDGET, pinned below
ADAPTIVE = {"type": "adaptive", "display": "summarized"}
LEVELS = ("low", "medium", "high")

HAIKU = "claude-haiku-4-5-20251001"
KIMI = "moonshotai/kimi-k3"
NEMOTRON = "nvidia/nemotron-3.5-lightning-30b-a3b"
GPT_OSS = "openai/gpt-oss-20b"

#: Every selectable model, plus the HERMES_MODEL env default (unlisted, so it
#: routes by name — the adaptive-thinking Claude branch).
ALL_MODELS = [m["id"] for m in models.MODEL_OPTIONS] + ["claude-sonnet-5"]

#: The reasoning-related part of each model's request BEFORE step 0.4, for a
#: non-continuation call with no output cap — transcribed from the pre-0.4
#: `_anthropic_turn` / `_openai_turn` / `_NVIDIA_TUNING`, in wire key order.
#: "<effort>" stands for normalize_effort(effort): the Claude output_config.
TODAY: dict[str, dict[str, Any]] = {
    "claude-haiku-4-5-20251001": {},
    "claude-opus-5": {"thinking": ADAPTIVE, "output_config": {"effort": "<effort>"}},
    "claude-opus-4-8": {"thinking": ADAPTIVE, "output_config": {"effort": "<effort>"}},
    "claude-sonnet-5": {"thinking": ADAPTIVE, "output_config": {"effort": "<effort>"}},
    "gpt-4o-mini": {},
    "gpt-5.4-mini": {},
    "gemini-3.5-flash": {},
    "gemini-3-flash-preview": {},
    "gemini-3.5-flash-lite": {},
    NEMOTRON: {
        "temperature": 1.0, "top_p": 0.95,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_budget": BUDGET},
    },
    GPT_OSS: {"temperature": 1.0, "top_p": 1.0},
    "google/gemma-4-31b-it": {
        "temperature": 0.5, "top_p": 1.0, "frequency_penalty": 0.0, "presence_penalty": 0.0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
    },
    "google/diffusiongemma-26b-a4b-it": {
        "temperature": 1.0, "top_p": 0.95,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
    },
    KIMI: {"temperature": 1.0, "extra_body": {"reasoning_effort": "max"}},
}

#: What `light` alone changes (no flag needed — proven values). Models absent
#: here are unchanged by light alone.
LIGHT: dict[str, dict[str, Any]] = {
    "claude-opus-5": {},  # thinking + output_config omitted
    "claude-opus-4-8": {},
    "claude-sonnet-5": {},
    NEMOTRON: {
        "temperature": 1.0, "top_p": 0.95,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_budget": 0},
    },
    "google/gemma-4-31b-it": {
        "temperature": 0.5, "top_p": 1.0, "frequency_penalty": 0.0, "presence_penalty": 0.0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "google/diffusiongemma-26b-a4b-it": {
        "temperature": 1.0, "top_p": 0.95,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
}

#: The UNVERIFIED values light+flag adds, per model and level. Written out
#: rather than read from agent._REASONING so a change to that table (step 2.3,
#: from the probe results) is a deliberate, reviewed change here too.
FLAGGED: dict[str, dict[str, dict[str, Any]]] = {
    KIMI: {lv: {"temperature": 1.0, "extra_body": {"reasoning_effort": lv}} for lv in LEVELS},
    GPT_OSS: {
        lv: {"temperature": 1.0, "top_p": 1.0, "extra_body": {"reasoning_effort": lv}}
        for lv in LEVELS
    },
    "gpt-5.4-mini": {lv: {"reasoning_effort": lv} for lv in LEVELS},
    "gemini-3.5-flash": {lv: {"reasoning_effort": lv} for lv in LEVELS},
    "gemini-3-flash-preview": {lv: {"reasoning_effort": lv} for lv in LEVELS},
}


# ---- Recording fake clients ------------------------------------------------
def _snap(value: Any) -> Any:
    """An independent, JSON-safe copy of a kwarg (SDK sentinels → a marker)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _snap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snap(v) for v in value]
    return f"<{type(value).__name__}>"


class _FakeAnthropicStream:
    def __enter__(self) -> "_FakeAnthropicStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __iter__(self):
        yield types.SimpleNamespace(type="text", text=REPLY)

    def get_final_message(self) -> Any:
        return types.SimpleNamespace(
            content=[{"type": "text", "text": REPLY}],
            stop_reason="end_turn",
            usage=types.SimpleNamespace(
                input_tokens=10, output_tokens=2,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


def _chunks() -> list[Any]:
    def choice(content: str | None, finish: str | None) -> Any:
        return types.SimpleNamespace(
            delta=types.SimpleNamespace(content=content, tool_calls=None), finish_reason=finish,
        )

    return [
        types.SimpleNamespace(usage=None, choices=[choice(REPLY, None)]),
        types.SimpleNamespace(usage=None, choices=[choice(None, "stop")]),
        types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=2), choices=[],
        ),
    ]


def _bad_request(message: str, status: int = 400) -> openai.APIStatusError:
    """The error the OpenAI SDK raises from create() when the server answers
    400/422 — built locally, no request is made."""
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    body = {"error": {"message": message}}
    response = httpx.Response(status, request=request, json=body)
    cls = openai.BadRequestError if status == 400 else openai.UnprocessableEntityError
    return cls(message, response=response, body=body)


class Recorder:
    """Fake client factories. `failures` scripts create() calls: the n-th
    OpenAI-protocol call raises failures[n] when that entry is an exception."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures: list[Exception | None] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def stream(**kwargs: Any) -> _FakeAnthropicStream:
            self.calls.append({"client": "anthropic", "kwargs": _snap(kwargs)})
            return _FakeAnthropicStream()

        def protocol(name: str) -> Any:
            def create(**kwargs: Any):
                index = sum(1 for c in self.calls if c["client"] != "anthropic")
                self.calls.append({"client": name, "kwargs": _snap(kwargs)})
                if index < len(self.failures) and self.failures[index] is not None:
                    raise self.failures[index]
                return iter(_chunks())

            return types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
            )

        anthropic_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
        openai_client, gemini_client, nvidia_client = (
            protocol("openai"), protocol("gemini"), protocol("nvidia"),
        )
        monkeypatch.setattr(agent, "_anthropic", lambda: anthropic_client)
        monkeypatch.setattr(agent, "_openai", lambda: openai_client)
        monkeypatch.setattr(agent, "_gemini", lambda: gemini_client)
        monkeypatch.setattr(agent, "_nvidia", lambda model_id: nvidia_client)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Recording clients, a pinned Config, a clean credit state, the flag
    unset and an empty fallback set (it is process-level state)."""
    credits.clear()
    monkeypatch.setattr(Config, "OUTPUT_CAP", 0)
    monkeypatch.setattr(Config, "NVIDIA_REASONING_BUDGET", BUDGET)
    monkeypatch.setattr(Config, "EFFORT", "medium")
    monkeypatch.delenv("HERMES_PROVIDER_EFFORT", raising=False)
    monkeypatch.setattr(agent, "_NO_REASONING_KNOB", set())
    r = Recorder()
    r.install(monkeypatch)
    yield r
    credits.clear()


def _call(model: str, *, effort: str | None = None, light: bool | None = None,
          continuation: bool = False) -> list[dict[str, Any]]:
    """Drive one provider call the way stream_turn does; return its events.
    `light=None` leaves the keyword out entirely — the pre-0.4 call shape."""
    provider = models.provider_of(model)
    extra = {} if light is None else {"light": light}
    if provider == "anthropic":
        gen = agent._anthropic_turn(
            model, [TOOL], SYSTEM, MESSAGES, effort, None, continuation, **extra,
        )
    else:
        gen = agent._openai_turn(
            model, [TOOL], SYSTEM, MESSAGES, None, continuation, provider,
            effort=effort, **extra,
        )
    return list(gen)


def _expected(model: str, extras: dict[str, Any], effort: str | None) -> dict[str, Any]:
    """The full request for `model` with `extras` as its reasoning part."""
    extras = json.loads(json.dumps(extras).replace('"<effort>"', json.dumps(agent.normalize_effort(effort))))
    if models.provider_of(model) == "anthropic":
        base = {
            "model": model, "max_tokens": Config.MAX_TOKENS, "system": SYSTEM,
            "tools": [TOOL], "messages": MESSAGES,
        }
    else:
        base = {
            "model": model,
            "messages": agent._to_openai_messages(SYSTEM, MESSAGES),
            "tools": agent._to_openai_tools([TOOL]),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    return _snap({**base, **extras})


def _assert_request(rec: Recorder, expected: dict[str, Any]) -> None:
    assert len(rec.calls) == 1, rec.calls
    actual = rec.calls[0]["kwargs"]
    # Byte comparison of the serialised request, key order included — the
    # same standard as the golden snapshots.
    assert json.dumps(actual) == json.dumps(expected), (actual, expected)


# ---- Captured kwargs: defaults are today's request ------------------------
@pytest.mark.parametrize("model", ALL_MODELS)
def test_defaults_send_exactly_todays_request(model: str, rec: Recorder) -> None:
    events = _call(model)
    assert [e["type"] for e in events] == ["text", "_final"]
    _assert_request(rec, _expected(model, TODAY[model], None))


@pytest.mark.parametrize("flag", ["off", "on"])
@pytest.mark.parametrize("effort", LEVELS)
@pytest.mark.parametrize("model", ALL_MODELS)
def test_effort_without_light_changes_nothing(
    model: str, effort: str, flag: str, rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_turn now hands the resolved effort to _openai_turn too. On a call
    that is not light it must not move a byte — not even with the flag on:
    FULL turns keep today's reasoning (plan 2.3, §6)."""
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", flag)
    _call(model, effort=effort, light=False)
    _assert_request(rec, _expected(model, TODAY[model], effort))


@pytest.mark.parametrize("effort", [None, *LEVELS])
@pytest.mark.parametrize("model", ALL_MODELS)
def test_light_sends_only_the_proven_values(
    model: str, effort: str | None, rec: Recorder,
) -> None:
    """Flag off: light omits Claude's thinking and flips the NIM booleans —
    nothing else. Kimi stays at "max" (plan §3) and Gemini/OpenAI unchanged."""
    _call(model, effort=effort, light=True)
    _assert_request(rec, _expected(model, LIGHT.get(model, TODAY[model]), effort))


@pytest.mark.parametrize("model", ALL_MODELS)
def test_light_plus_flag_without_an_effort_level_is_light(
    model: str, rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """effort=None has no level to translate, so the flag adds nothing."""
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    _call(model, effort=None, light=True)
    _assert_request(rec, _expected(model, LIGHT.get(model, TODAY[model]), None))


@pytest.mark.parametrize("effort", LEVELS)
@pytest.mark.parametrize("model", ALL_MODELS)
def test_light_plus_flag_sends_the_reasoning_map(
    model: str, effort: str, rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    _call(model, effort=effort, light=True)
    if model in FLAGGED:
        extras = FLAGGED[model][effort]
    else:
        extras = LIGHT.get(model, TODAY[model])
    _assert_request(rec, _expected(model, extras, effort))


def test_flag_reads_only_recognised_on_values(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown value must fall back to off, never into the untested values."""
    for value, sends in (("on", True), ("true", True), ("1", True), ("maybe", False), ("", False)):
        rec.calls.clear()
        monkeypatch.setenv("HERMES_PROVIDER_EFFORT", value)
        _call(KIMI, effort="low", light=True)
        sent = rec.calls[0]["kwargs"]["extra_body"]["reasoning_effort"]
        assert sent == ("low" if sends else "max"), value


@pytest.mark.parametrize("model", [NEMOTRON, "claude-opus-5", KIMI])
def test_continuation_is_unchanged_by_light_and_the_flag(
    model: str, rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation already runs with reasoning off; light and the flag
    must not alter it (for Kimi: continuation never carried an effort knob)."""
    _call(model, effort="low", continuation=True)
    plain = rec.calls[-1]["kwargs"]
    rec.calls.clear()
    _call(model, effort="low", light=True, continuation=True)
    assert rec.calls[-1]["kwargs"] == plain
    if model == KIMI:
        rec.calls.clear()
        monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
        _call(model, effort="low", light=False, continuation=True)
        assert rec.calls[-1]["kwargs"] == plain


def test_light_does_not_leak_into_the_tuning_table(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag replaces Kimi's "max" in the request's own extra_body copy —
    the shared _NVIDIA_TUNING recipe must still say "max" afterwards."""
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    _call(KIMI, effort="low", light=True)
    assert agent._NVIDIA_TUNING[KIMI]["extra"] == {"reasoning_effort": "max"}
    rec.calls.clear()
    _call(KIMI)
    assert rec.calls[0]["kwargs"]["extra_body"] == {"reasoning_effort": "max"}


# ---- The 400 fallback ------------------------------------------------------
def test_400_on_the_effort_knob_retries_once_with_legacy_kwargs(
    rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    rec.failures = [_bad_request("Invalid value for 'reasoning_effort': 'low' is not supported")]
    events = _call(KIMI, effort="low", light=True)

    assert len(rec.calls) == 2
    assert rec.calls[0]["kwargs"]["extra_body"] == {"reasoning_effort": "low"}
    # The retry is today's request, byte for byte.
    assert json.dumps(rec.calls[1]["kwargs"]) == json.dumps(_expected(KIMI, TODAY[KIMI], None))
    # Nothing from the failed attempt reached the caller: one reply, once.
    assert [e["type"] for e in events] == ["text", "_final"]
    assert events[0]["text"] == REPLY
    assert KIMI in agent._NO_REASONING_KNOB

    # Later calls go straight to the legacy request: one provider call, no 400.
    rec.calls.clear()
    rec.failures = []
    _call(KIMI, effort="low", light=True)
    assert len(rec.calls) == 1
    assert rec.calls[0]["kwargs"]["extra_body"] == {"reasoning_effort": "max"}


def test_400_on_the_nim_thinking_toggle_falls_back_without_the_flag(rec: Recorder) -> None:
    """light's boolean flip needs no flag — and still has the safety net."""
    rec.failures = [_bad_request("chat_template_kwargs.enable_thinking: unexpected value")]
    events = _call(NEMOTRON, light=True)
    assert len(rec.calls) == 2
    assert rec.calls[0]["kwargs"]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert json.dumps(rec.calls[1]["kwargs"]) == json.dumps(_expected(NEMOTRON, TODAY[NEMOTRON], None))
    assert [e["type"] for e in events] == ["text", "_final"]
    assert NEMOTRON in agent._NO_REASONING_KNOB


def test_422_naming_the_knob_also_falls_back(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    rec.failures = [_bad_request("reasoning_effort: extra inputs are not permitted", status=422)]
    _call("gpt-5.4-mini", effort="low", light=True)
    assert len(rec.calls) == 2
    assert "reasoning_effort" in rec.calls[0]["kwargs"]
    assert "reasoning_effort" not in rec.calls[1]["kwargs"]


def test_400_about_something_else_is_raised_unchanged(
    rec: Recorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    rec.failures = [_bad_request("messages: context length exceeded")]
    with pytest.raises(openai.BadRequestError):
        _call(KIMI, effort="low", light=True)
    assert len(rec.calls) == 1
    assert KIMI not in agent._NO_REASONING_KNOB


def test_400_on_a_legacy_request_is_not_retried(rec: Recorder) -> None:
    """Defaults carry no knob, so a 400 that happens to say "thinking" is a
    real error: raised as before, no second request, nothing remembered."""
    rec.failures = [_bad_request("enable_thinking is not supported for this model")]
    with pytest.raises(openai.BadRequestError):
        _call(NEMOTRON)
    assert len(rec.calls) == 1
    assert NEMOTRON not in agent._NO_REASONING_KNOB


def test_fallback_retries_only_once(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_PROVIDER_EFFORT", "on")
    rec.failures = [
        _bad_request("reasoning_effort not supported"),
        _bad_request("reasoning_effort not supported"),
    ]
    with pytest.raises(openai.BadRequestError):
        _call(KIMI, effort="low", light=True)
    assert len(rec.calls) == 2


def test_known_bad_model_skips_knobs_for_light_too(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once in _NO_REASONING_KNOB a model gets the legacy request for every
    knob, the proven boolean flip included."""
    agent._NO_REASONING_KNOB.add(NEMOTRON)
    _call(NEMOTRON, light=True)
    _assert_request(rec, _expected(NEMOTRON, TODAY[NEMOTRON], None))


# ---- stream_turn hands the resolved effort to both protocols ---------------
@pytest.mark.parametrize("model", [HAIKU, KIMI])
def test_stream_turn_passes_resolved_effort_and_never_light(
    model: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    credits.clear()
    seen: list[dict[str, Any]] = []

    def provider(model_id, tools, system, messages, *args, **kwargs):
        seen.append({"args": args, "kwargs": kwargs})
        yield {"type": "text", "text": REPLY}
        yield {
            "type": "_final", "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(Config, "EFFORT", "medium")
    monkeypatch.setattr(models, "_configured", lambda: model)
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(apps_access, "enabled", lambda: True)
    monkeypatch.setattr(agent, "build_tools", lambda *a, **k: [TOOL])
    monkeypatch.setattr(agent, "_system_prompt", lambda *a, **k: "system")
    monkeypatch.setattr(health, "guarded", lambda signal, fn, default="": default)
    monkeypatch.setattr(agent, "store", ConversationStore())
    monkeypatch.setattr(agent, "_anthropic_turn", provider)
    monkeypatch.setattr(agent, "_openai_turn", provider)

    for text, explicit, resolved in (("hi", None, "low"), ("hi", "high", "high"), ("Hi!", None, "medium")):
        seen.clear()
        list(agent.stream_turn(f"effort-{model}-{text}-{explicit}", text, effort=explicit))
        assert len(seen) == 1
        call = seen[0]
        if model == HAIKU:
            assert call["args"][0] == resolved  # positional, as before 0.4
        else:
            assert call["kwargs"].get("effort") == resolved
        # No CHAT tier yet (step 1.2): stream_turn never asks for a light call,
        # and no route reaches the matcher (step 1.3) — "Hi!" stays medium.
        assert "light" not in call["kwargs"]
    credits.clear()


# ---- _auto_effort: route auto vs route full --------------------------------
def _effort(text: str, route: str | None = None, attachments: bool = False) -> str:
    content = [{"type": "text", "text": text}]
    if route is None:
        return agent.resolve_effort(None, content, attachments)
    return agent.resolve_effort(None, content, attachments, route=route)


@pytest.fixture
def medium_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config.EFFORT is "low" by default, which would make "not low" and
    "the platform default" indistinguishable. Pin it to medium."""
    monkeypatch.setattr(Config, "EFFORT", "medium")


@pytest.mark.parametrize("text", [
    "Hi!", "thanks!", "good morning", "Good morning!", "good afternoon", "Good evening.",
    "gm", "GM!", "hey :)", "Hi, how are you?", "thank you!!", "ok.", "yes 🙂",
])
def test_route_auto_greetings_are_low(text: str, medium_default: None) -> None:
    assert _effort(text, "auto") == "low"


@pytest.mark.parametrize("text", ["explanation", "remodel", "Can you give me an explanation of the remodel?"])
def test_route_auto_mid_word_hints_are_not_high(text: str, medium_default: None) -> None:
    assert _effort(text, "auto") != "high"
    assert _effort(text, "auto") == "medium"


@pytest.mark.parametrize("text", [
    "analyze my P&L", "strategy", "optimize", "Optimise the rota", "why is rent late",
    "what is the plan", "explain how this works", "give me a trade-off",
])
def test_route_auto_leading_hints_are_high(text: str, medium_default: None) -> None:
    assert _effort(text, "auto") == "high"


@pytest.mark.parametrize("text", ["history", "island", "yesterday's numbers", "himalaya"])
def test_route_auto_hint_prefixes_inside_words_are_not_low(text: str, medium_default: None) -> None:
    assert _effort(text, "auto") != "low"


@pytest.mark.parametrize("text", ["👍", "?", "...", "🙏🙏"])
def test_route_auto_punctuation_only_keeps_the_platform_default(text: str, medium_default: None) -> None:
    """Empty only after stripping is not an empty message (it may be an
    approval or a nudge) — today's outcome, the platform default, stands."""
    assert _effort(text, "auto") == "medium"


def test_route_auto_empty_message_is_low_as_today(medium_default: None) -> None:
    assert _effort("", "auto") == "low"
    assert _effort("", "full") == "low"


def test_route_auto_attachments_use_the_anchored_high_match(medium_default: None) -> None:
    assert _effort("explanation please", "auto", attachments=True) == "medium"
    assert _effort("explanation please", "full", attachments=True) == "high"
    assert _effort("analyze this", "auto", attachments=True) == "high"


#: route="full" outputs — today's matcher, quirks and all. Captured from the
#: pre-0.4 _auto_effort with Config.EFFORT = "medium".
TODAY_FULL = [
    ("Hi!", "medium"), ("hi", "low"), ("thanks!", "medium"), ("thanks", "low"),
    ("good morning", "medium"), ("Good morning!", "medium"), ("gm", "medium"),
    ("explanation", "high"), ("remodel", "high"), ("analyze my P&L", "high"),
    ("strategy", "high"), ("optimize", "high"), ("history", "medium"),
    ("Can you give me an explanation of the remodel?", "high"), ("", "low"),
    ("👍", "medium"), ("hey", "low"), ("yes", "low"), ("ok.", "medium"),
    ("what time is it", "low"), ("why is rent late", "high"),
    ("Hi, how are you?", "medium"), ("what is the plan", "high"),
    ("explain how this works", "high"),
]


@pytest.mark.parametrize("text,expected", TODAY_FULL)
def test_route_full_is_todays_matcher(text: str, expected: str, medium_default: None) -> None:
    assert _effort(text, "full") == expected
    # The default route IS full, and anything but exactly "auto" reads as full.
    assert _effort(text) == expected
    assert _effort(text, "AUTO") == expected
    assert _effort(text, "bogus") == expected


def test_explicit_effort_wins_under_either_route(medium_default: None) -> None:
    content = [{"type": "text", "text": "Hi!"}]
    for route in ("full", "auto"):
        assert agent.resolve_effort("high", content, False, route=route) == "high"
        assert agent.resolve_effort(" LOW ", content, False, route=route) == "low"
    assert agent.resolve_effort("auto", content, False, route="auto") == "low"
    assert agent.resolve_effort("auto", content, False, route="full") == "medium"


@pytest.mark.parametrize("title,full,auto", [
    # "strateg" is a HIGH stem under both matchers.
    ("Strategy sync", "high", "high"),
    # Today's substring match fires on "model" inside "Remodel"; route full
    # must keep that — the unattended caller's effort does not move.
    ("Remodel kickoff", "high", "medium"),
    # No hint at all, ~50 words: the platform default under both.
    ("Weekly ops sync", "medium", "medium"),
])
def test_meeting_notes_prompt_keeps_todays_effort(
    title: str, full: str, auto: str, monkeypatch: pytest.MonkeyPatch, medium_default: None,
) -> None:
    """generate_ai_notes is an unattended caller: it passes no route, so its
    real prompt resolves through route="full" exactly as before 0.4."""
    from hermes import meeting_copilot

    captured: dict[str, Any] = {}

    def fake_run_turn(session_id, user_content, *args, **kwargs):
        captured["prompt"] = user_content
        captured["kwargs"] = kwargs
        return {"reply": REPLY, "tool_calls": []}

    monkeypatch.setattr(agent, "run_turn", fake_run_turn)
    out = meeting_copilot.generate_ai_notes(
        business_slug="nh", business_name="Nour Hammam", meeting_title=title,
    )
    assert out["notes"] == REPLY
    assert "route" not in captured["kwargs"]
    prompt = captured["prompt"]
    assert title in prompt
    assert _effort(prompt) == full
    assert _effort(prompt, "full") == full
    assert _effort(prompt, "auto") == auto
