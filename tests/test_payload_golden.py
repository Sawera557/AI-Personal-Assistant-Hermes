"""Golden snapshots of the EXACT wire request Hermes sends each provider.

Why this file exists: docs/architecture/TURN_COST_PLAN.md trims prompts, tools
and reasoning per turn, and every Phase 0 step promises that with no new flag
set the request is byte-identical to today's. "Looks the same" is not a proof —
a reordered tool, a dropped kwarg or one changed sentence in the system prompt
silently changes what every caller pays for and what the model is told. This
test is the proof: it drives the real `stream_turn` / `run_turn` (and the real
meeting-notes caller) for a matrix of models, scopes and callers, records every
kwarg each provider call is handed, and compares it byte-for-byte against a
fixture captured from the unmodified code.

How it stays honest and offline:

  * Stubs sit at the CLIENT factories (`agent._anthropic`, `_openai`,
    `_gemini`, `_nvidia`), not at `_anthropic_turn` / `_openai_turn`. So the
    kwargs those two functions build — thinking/output_config, the OpenAI
    message translation, NOT_GIVEN tools, NIM extra_body, stream_options — are
    exactly what is recorded. The fake clients reply "Hello." with usage, in the
    same streamed shapes the real SDKs produce (Anthropic: a context manager
    yielding `text` events then `get_final_message()`; OpenAI protocol: content,
    finish_reason and a trailing usage-only chunk).
  * Retrieval is pinned: memory, context pack, RAG and past-conversation recall
    return fixed sample strings (tagged with the scope they were asked for, so a
    change in how scope reaches retrieval shows up too), and today's date is
    frozen. Each scenario gets a fresh in-memory conversation store, a forced
    active model and a clean credit state.
  * The environment is pinned too. Tools and the base prompt depend on Config
    values loaded from the environment (HERMES_APPS, which businesses have a
    Maton key, web search, token and NIM knobs — from .env on a developer's
    machine, from nothing at all in CI). Every scenario runs with exactly
    GOLDEN_ENV, the world the fixtures were captured in: Config is
    monkeypatched to it (fake, never-used Maton keys for the connected slugs)
    whatever the machine's .env says. So the snapshots compare byte-for-byte on
    every machine and in CI — they used to SKIP wherever the environment
    differed, which meant CI, the one place a reviewer can't hand-wave,
    silently never enforced the byte-identical guarantee, and an owner editing
    HERMES_APPS or adding a business key quietly turned every golden into a
    skip. Each fixture still records `env_fingerprint`, read back AFTER
    pinning, so a pin that stops working shows up as a fixture diff.

Regenerate ONLY on purpose, from code whose payload change you mean to ship,
and review the fixture diff like code:

    UPDATE_GOLDEN=1 python -m pytest tests/test_payload_golden.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hermes import agent, apps_access, context_mode, credits, models
from hermes.config import Config
from hermes.memory import ConversationStore

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE = os.getenv("UPDATE_GOLDEN", "").strip().lower() in {"1", "true", "yes", "on"}

#: The date every system prompt is built on. Matches the plan's measurement date.
FROZEN_TODAY = datetime.date(2026, 10, 7)

#: What every fake provider says. Short, tool-free, and finishes cleanly, so each
#: scenario is exactly one provider call per user message.
REPLY = "Hello."


# ---- Scenarios ------------------------------------------------------------
@dataclass(frozen=True)
class Scenario:
    """One recorded turn sequence. Field defaults are the web chat's defaults:
    `/api/chat/stream` with no business picked ('all'), Context on, no voice."""

    name: str
    model: str
    description: str
    #: "stream_turn" (/api/chat/stream), "run_turn" (/api/chat, voice, scheduler)
    #: or "meeting_notes" (meeting_copilot.generate_ai_notes, the real caller).
    caller: str = "stream_turn"
    messages: tuple[str, ...] = ("hi",)
    active_business: str | None = "all"
    voice: bool = False
    #: context_mode.enabled() for the turn.
    context: bool = True
    #: Whether the pinned retrieval stubs return their sample blocks or "".
    retrieval_blocks: bool = True
    allowed_apps: tuple[str, ...] | None = None
    interactive: bool = True
    autonomy: str | None = None
    session: str = "golden-session"
    extra: dict[str, Any] = field(default_factory=dict)


HAIKU = "claude-haiku-4-5-20251001"
# Adaptive-thinking Claude. claude-sonnet-5 (the HERMES_MODEL env default) is not
# in models.MODEL_OPTIONS; claude-opus-5 is, and takes the same thinking branch.
OPUS = "claude-opus-5"
GPT = "gpt-4o-mini"
FLASH_LITE = "gemini-3.5-flash-lite"
KIMI = "moonshotai/kimi-k3"
NEMOTRON = "nvidia/nemotron-3.5-lightning-30b-a3b"

SCENARIOS: list[Scenario] = [
    # -- one "hi" per provider / protocol branch, in the default 'all' scope --
    Scenario("haiku_all_hi", HAIKU, "Anthropic, no thinking (Haiku branch)."),
    Scenario("opus5_all_hi", OPUS, "Anthropic with adaptive thinking + output_config effort."),
    Scenario("gpt4omini_all_hi", GPT, "OpenAI protocol on api.openai.com."),
    Scenario("flashlite_all_hi", FLASH_LITE, "OpenAI protocol on Gemini's endpoint."),
    Scenario("nemotron_all_hi", NEMOTRON, "NIM with thinking toggle + reasoning_budget."),
    Scenario("kimi_all_hi", KIMI, "NIM with reasoning_effort=max in extra_body."),
    # -- business scope: None (legacy, no business awareness) and a locked slug --
    Scenario(
        "kimi_none_hi", KIMI, "Scope None via run_turn's default (legacy, no business).",
        caller="run_turn", active_business=None,
    ),
    Scenario(
        "kimi_nh_hi", KIMI, "Locked to one business: no `business` enum on tools.",
        active_business="nh",
    ),
    # -- the realistic production "hi": nothing retrieved in 'all' scope --
    Scenario(
        "kimi_all_hi_no_blocks", KIMI,
        "Rich prompt and tools with every retrieval block empty.",
        retrieval_blocks=False,
    ),
    # -- voice, Context off, history replay --
    Scenario("kimi_all_voice_hi", KIMI, "Voice turn via run_turn.", caller="run_turn", voice=True),
    Scenario("haiku_context_off_hi", HAIKU, "Context off: plain prompt, tools=[] on Anthropic.", context=False),
    Scenario("kimi_context_off_hi", KIMI, "Context off: plain prompt, tools NOT_GIVEN on NIM.", context=False),
    Scenario(
        "kimi_all_two_turns", KIMI, "Second turn replays the first exchange as history.",
        messages=("hi", "thanks"),
    ),
    # -- a business question --
    Scenario(
        "kimi_gak_rent_status", KIMI, "Business question in a GAK-locked chat.",
        messages=("rent status for GAK?",), active_business="gak",
    ),
    Scenario(
        "opus5_gak_rent_status", OPUS,
        "Business question on adaptive-thinking Claude (effort falls to Config.EFFORT).",
        messages=("rent status for GAK?",), active_business="gak",
    ),
    # -- unattended callers --
    Scenario(
        "kimi_scheduled_readonly", KIMI,
        "Scheduler-style run: interactive=False, autonomy=readonly, no app restriction.",
        caller="run_turn", messages=("Summarise any overdue rent and report it.",),
        active_business="nh", interactive=False, autonomy="readonly",
        session="__scheduled__:golden-task",
    ),
    Scenario(
        "kimi_scheduled_allowed_apps", KIMI,
        "Scheduler-style run restricted to two apps, autonomy=draft.",
        caller="run_turn", messages=("Draft a rent reminder for overdue tenants.",),
        active_business="gak", allowed_apps=("google-mail", "google-sheets"),
        interactive=False, autonomy="draft", session="__scheduled__:golden-task-apps",
    ),
    Scenario(
        "kimi_meeting_notes_nh", KIMI,
        "meeting_copilot.generate_ai_notes, the real unattended caller.",
        caller="meeting_notes", messages=(), active_business="nh",
        extra={"business_name": "Nour Hammam", "meeting_title": "Weekly ops sync"},
    ),
]


# ---- Environment fingerprint ----------------------------------------------
def env_fingerprint() -> dict[str, Any]:
    """The Config values (all loaded from the environment) that change the
    payload of a turn under the stubs above. Non-secret by construction: the
    business-key entry is the list of slugs that HAVE a key, never a key.

    If a later step adds an env knob that changes the default payload, add it
    here, to GOLDEN_ENV and to _pin_environment, AND regenerate —
    `test_golden_fingerprint_keys_match` fails until all of that is done, so a
    new knob can never be left floating with the machine's environment."""
    return {
        "HERMES_APPS": Config.apps(),
        "connected_business_slugs": Config.connected_business_slugs(),
        "HERMES_WEB_SEARCH": Config.WEB_SEARCH,
        "HERMES_MAX_TOKENS": Config.MAX_TOKENS,
        "HERMES_OUTPUT_CAP": Config.OUTPUT_CAP,
        "HERMES_EFFORT": Config.EFFORT,
        "NVIDIA_REASONING_BUDGET": Config.NVIDIA_REASONING_BUDGET,
        "NVIDIA_TEMPERATURE": Config.NVIDIA_TEMPERATURE,
        "NVIDIA_TOP_P": Config.NVIDIA_TOP_P,
    }


#: The environment every golden payload is captured in AND compared in — the
#: developer's .env on the day the fixtures were first captured from unmodified
#: code (no wati/notion/zoom-admin in HERMES_APPS; all six businesses keyed).
#: Non-secret: app slugs, business slugs and numbers only. Changing a value here
#: changes the payload of every scenario, so it is a fixture regeneration like
#: any other payload change: on purpose, with the fixture diff reviewed.
GOLDEN_ENV: dict[str, Any] = {
    "HERMES_APPS": [
        "quickbooks", "pdf-co", "google-contacts", "fathom", "google-meet", "google-forms",
        "google-sheets", "google-docs", "google-drive", "google-calendar", "google-mail",
        "zoom", "jira",
    ],
    "connected_business_slugs": ["afk", "gak", "gif", "gakc", "nh", "per"],
    "HERMES_WEB_SEARCH": True,
    "HERMES_MAX_TOKENS": 16000,
    "HERMES_OUTPUT_CAP": 0,
    "HERMES_EFFORT": "medium",
    "NVIDIA_REASONING_BUDGET": 16384,
    "NVIDIA_TEMPERATURE": 1.0,
    "NVIDIA_TOP_P": 0.95,
}

#: Fingerprint keys that are plain Config class attributes (read at call time
#: by agent/tools, never copied at import), pinned by setattr.
_PINNED_ATTRS = {
    "HERMES_WEB_SEARCH": "WEB_SEARCH",
    "HERMES_MAX_TOKENS": "MAX_TOKENS",
    "HERMES_OUTPUT_CAP": "OUTPUT_CAP",
    "HERMES_EFFORT": "EFFORT",
    "NVIDIA_REASONING_BUDGET": "NVIDIA_REASONING_BUDGET",
    "NVIDIA_TEMPERATURE": "NVIDIA_TEMPERATURE",
    "NVIDIA_TOP_P": "NVIDIA_TOP_P",
}


def _pin_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Config report exactly GOLDEN_ENV for the rest of this test.

    HERMES_APPS is re-read from the environment on every Config.apps() call,
    so it is pinned with setenv. Which businesses are "connected" is just which
    MATON_BUSINESS_KEYS entries are non-empty: the connected slugs get a fake
    key, in GOLDEN_ENV's order (connected_business_slugs() keeps dict order,
    and that order reaches the `business` enum), and every other known slug an
    empty one. The fake keys can't reach anything: the provider clients are
    stubbed, the fake replies never call a tool, and conftest blocks the
    network — and on a developer's machine they replace, for this test only,
    the real keys .env loaded."""
    monkeypatch.setenv("HERMES_APPS", ",".join(GOLDEN_ENV["HERMES_APPS"]))
    keys = {slug: f"golden-fake-maton-key-{slug}" for slug in GOLDEN_ENV["connected_business_slugs"]}
    for slug in Config.MATON_BUSINESS_KEYS:
        keys.setdefault(slug, "")
    monkeypatch.setattr(Config, "MATON_BUSINESS_KEYS", keys)
    for key, attr in _PINNED_ATTRS.items():
        monkeypatch.setattr(Config, attr, GOLDEN_ENV[key])
    # Guards the pin itself: a fingerprint key added without a pin here would
    # otherwise float with the machine's environment again.
    assert env_fingerprint() == GOLDEN_ENV, (
        "the golden environment pin is incomplete: env_fingerprint() after pinning is "
        f"{env_fingerprint()!r}, expected GOLDEN_ENV"
    )


# ---- Serialisation --------------------------------------------------------
def _jsonable(value: Any) -> Any:
    """A JSON-safe, independent copy of a kwarg value.

    Copying here (rather than holding a reference) matters: `_anthropic_turn`
    hands the SDK the live `messages` list, which stream_turn appends the
    assistant reply to right after the call returns. SDK sentinels (NOT_GIVEN,
    Omit) become a stable marker string. Anything else unexpected raises, so a
    new kind of value can't slip into a fixture as an address-bearing repr."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    type_name = type(value).__name__
    if type_name in {"NotGiven", "Omit"}:
        return f"<{type_name}>"
    raise TypeError(f"golden harness can't serialise {type_name}: {value!r}")


def _dumps(doc: dict[str, Any]) -> str:
    # Insertion order is kept (no sort_keys): the comparison is on this text, so
    # a reordered kwarg or schema key is a diff too — the wire bytes changed.
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---- Fake provider clients ------------------------------------------------
_USAGE_IN, _USAGE_OUT = 1234, 5


class _FakeAnthropicStream:
    """What `client.messages.stream(...)` returns: a context manager that
    iterates raw events and then serves the final message."""

    def __enter__(self) -> "_FakeAnthropicStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def __iter__(self):
        yield types.SimpleNamespace(type="text", text=REPLY)

    def get_final_message(self) -> Any:
        return types.SimpleNamespace(
            # Plain dicts: agent._dump passes non-SDK blocks through unchanged.
            content=[{"type": "text", "text": REPLY}],
            stop_reason="end_turn",
            usage=types.SimpleNamespace(
                input_tokens=_USAGE_IN, output_tokens=_USAGE_OUT,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


def _openai_chunks() -> list[Any]:
    """A minimal streamed chat completion: one content delta, the finish
    reason, then the usage-only chunk `stream_options.include_usage` produces."""

    def choice(content: str | None, finish: str | None) -> Any:
        return types.SimpleNamespace(
            delta=types.SimpleNamespace(content=content, tool_calls=None),
            finish_reason=finish,
        )

    return [
        types.SimpleNamespace(usage=None, choices=[choice(REPLY, None)]),
        types.SimpleNamespace(usage=None, choices=[choice(None, "stop")]),
        types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=_USAGE_IN, completion_tokens=_USAGE_OUT),
            choices=[],
        ),
    ]


def _install_fake_clients(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    def anthropic_stream(**kwargs: Any) -> _FakeAnthropicStream:
        calls.append({"client": "anthropic", "endpoint": "messages.stream", "kwargs": _jsonable(kwargs)})
        return _FakeAnthropicStream()

    def openai_protocol(client_name: str):
        def create(**kwargs: Any):
            calls.append({
                "client": client_name, "endpoint": "chat.completions.create",
                "kwargs": _jsonable(kwargs),
            })
            return iter(_openai_chunks())

        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )

    anthropic_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=anthropic_stream))
    openai_client, gemini_client, nvidia_client = (
        openai_protocol("openai"), openai_protocol("gemini"), openai_protocol("nvidia"),
    )
    monkeypatch.setattr(agent, "_anthropic", lambda: anthropic_client)
    monkeypatch.setattr(agent, "_openai", lambda: openai_client)
    monkeypatch.setattr(agent, "_gemini", lambda: gemini_client)
    monkeypatch.setattr(agent, "_nvidia", lambda model_id: nvidia_client)


# ---- Deterministic retrieval, date, model and store -----------------------
def _install_pinned_world(
    monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> ConversationStore:
    import hermes.context_packs as context_packs
    import hermes.conversation_retrieve as conversation_retrieve
    import hermes.db.conversation_memory as conversation_memory
    import hermes.memory as memory
    import hermes.rag as rag

    _pin_environment(monkeypatch)
    on = scenario.retrieval_blocks

    def memory_stub(*args: Any, **kwargs: Any) -> str:
        scope = kwargs.get("active_business")
        return f"- [owner] Prefers short, direct answers. (sample memory; scope={scope!r})" if on else ""

    def pack_stub(*args: Any, **kwargs: Any) -> str:
        scope = args[0] if args else kwargs.get("active_business")
        return (
            f"Hard rules: money figures come only from live QuickBooks. "
            f"(sample context pack; scope={scope!r})" if on else ""
        )

    def rag_stub(*args: Any, **kwargs: Any) -> str:
        scope = kwargs.get("active_business")
        return (
            f"RETRIEVED CONTEXT (sample; scope={scope!r}):\n"
            "- [rent_roll.xlsx] Unit 4B rent due on the 1st." if on else ""
        )

    def recall_stub(*args: Any, **kwargs: Any) -> str:
        scope = kwargs.get("active_business")
        return (
            f"EARLIER CONVERSATIONS (sample; scope={scope!r}):\n"
            "- 2026-10-01 user: remind me about rent on Mondays." if on else ""
        )

    monkeypatch.setattr(agent, "build_memory_block", memory_stub)
    monkeypatch.setattr(context_packs, "packs_for", pack_stub)
    monkeypatch.setattr(rag, "build_context_block", rag_stub)
    monkeypatch.setattr(conversation_retrieve, "build_history_block", recall_stub)
    # Evaluated as an argument before the recall stub runs; the real one opens a
    # DB cursor, which must never happen in the suite.
    monkeypatch.setattr(conversation_memory, "owner_user_id", lambda: "golden-owner")

    class _FrozenDate(datetime.date):
        @classmethod
        def today(cls) -> "_FrozenDate":
            return cls(FROZEN_TODAY.year, FROZEN_TODAY.month, FROZEN_TODAY.day)

    frozen = types.SimpleNamespace(
        **{k: getattr(datetime, k) for k in dir(datetime) if not k.startswith("__")}
    )
    frozen.date = _FrozenDate
    monkeypatch.setattr(agent, "datetime", frozen)

    credits.clear()
    monkeypatch.setattr(models, "_configured", lambda: scenario.model)
    monkeypatch.setattr(context_mode, "enabled", lambda: scenario.context)
    # Pinned so the Apps switch (TURN_COST_PLAN step 0.3) can't make the
    # snapshot depend on another test's leftover flag.
    monkeypatch.setattr(apps_access, "enabled", lambda: True)
    # Step 0.3's env flags, pinned to their defaults (off): these snapshots ARE
    # the default-flag payload, so a developer's .env that turns either on must
    # not turn every fixture into a diff. Config reads both per call.
    monkeypatch.delenv("HERMES_SPLIT_TOGGLES", raising=False)
    monkeypatch.delenv("HERMES_STANDING_RULES_CORE", raising=False)
    # Step 0.4's flag, likewise. It only ever reaches `light` calls, which no
    # scenario here makes yet, but a CHAT-tier scenario added later must not
    # snapshot the developer's .env setting instead of the default.
    monkeypatch.delenv("HERMES_PROVIDER_EFFORT", raising=False)

    store = ConversationStore()
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(memory, "store", store)
    return store


# ---- Driving the real callers ---------------------------------------------
def _drive(scenario: Scenario) -> None:
    allowed = list(scenario.allowed_apps) if scenario.allowed_apps else None

    if scenario.caller == "meeting_notes":
        from hermes import meeting_copilot

        out = meeting_copilot.generate_ai_notes(
            business_slug=scenario.active_business,
            business_name=scenario.extra["business_name"],
            meeting_title=scenario.extra["meeting_title"],
        )
        assert out["notes"] == REPLY, out
        return

    for text in scenario.messages:
        # The chat endpoints hand the agent a list of content blocks, not a str.
        content = [{"type": "text", "text": text}]
        if scenario.caller == "stream_turn":
            events = list(agent.stream_turn(
                scenario.session, content, voice=scenario.voice,
                active_business=scenario.active_business, allowed_apps=allowed,
                effort=None, interactive=scenario.interactive, autonomy=scenario.autonomy,
            ))
            result = events[-1]["result"]
        elif scenario.caller == "run_turn":
            result = agent.run_turn(
                scenario.session, content, voice=scenario.voice,
                active_business=scenario.active_business, allowed_apps=allowed,
                effort=None, interactive=scenario.interactive, autonomy=scenario.autonomy,
            )
        else:  # pragma: no cover — a typo in SCENARIOS
            raise ValueError(f"unknown caller {scenario.caller!r}")
        # Guards the harness itself: the fakes must have been consumed exactly as
        # the real streams are, or the recorded kwargs prove nothing.
        assert result["reply"] == REPLY, result
        assert result["stop_reason"] == "end_turn", result


def _record(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    _install_pinned_world(monkeypatch, scenario)
    _install_fake_clients(monkeypatch, calls)
    _drive(scenario)
    assert calls, "no provider call was made"
    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "caller": scenario.caller,
        "inputs": {
            "messages": list(scenario.messages),
            "active_business": scenario.active_business,
            "voice": scenario.voice,
            "context_mode": scenario.context,
            "retrieval_blocks": scenario.retrieval_blocks,
            "allowed_apps": list(scenario.allowed_apps) if scenario.allowed_apps else None,
            "interactive": scenario.interactive,
            "autonomy": scenario.autonomy,
            **({"extra": scenario.extra} if scenario.extra else {}),
        },
        "provider": models.provider_of(scenario.model),
        "model": scenario.model,
        "frozen_date": FROZEN_TODAY.isoformat(),
        "env_fingerprint": env_fingerprint(),
        "calls": calls,
    }


def _fixture_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def _diff(expected: str, actual: str, name: str, limit: int = 300) -> str:
    lines = list(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=f"golden/{name}.json", tofile="recorded now", lineterm="", n=3,
    ))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines)


# ---- Tests ----------------------------------------------------------------
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_payload_matches_golden(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _fixture_path(scenario.name)

    # No environment-mismatch skip: _record pins Config to GOLDEN_ENV, so the
    # comparison is the same on every machine and in CI. A fixture captured in
    # some other environment differs in its recorded env_fingerprint and fails
    # like any other payload diff.
    if not UPDATE and not path.exists():
        pytest.fail(
            f"missing golden fixture {path.name}. Generate it from UNMODIFIED code with "
            "UPDATE_GOLDEN=1 python -m pytest tests/test_payload_golden.py -p no:cacheprovider"
        )

    doc = _record(scenario, monkeypatch)
    actual = _dumps(doc)

    if UPDATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(actual)
        return

    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"wire payload for scenario {scenario.name!r} changed. If this change is "
            "intended, regenerate with UPDATE_GOLDEN=1 and review the fixture diff.\n"
            + _diff(expected, actual, scenario.name),
            pytrace=False,
        )


def test_golden_fixture_set_matches_scenarios() -> None:
    """Every scenario has a fixture and no fixture is orphaned — a renamed or
    deleted scenario must not leave a stale snapshot that nobody compares."""
    if UPDATE:
        pytest.skip("regenerating")
    on_disk = {p.stem for p in GOLDEN_DIR.glob("*.json")}
    expected = {s.name for s in SCENARIOS}
    assert on_disk == expected, {
        "missing": sorted(expected - on_disk), "orphaned": sorted(on_disk - expected),
    }


def test_golden_fingerprint_keys_match() -> None:
    """A fingerprint key added in code but not in the fixtures or GOLDEN_ENV
    (or vice versa) fails loudly instead of quietly ignoring a knob."""
    if UPDATE:
        pytest.skip("regenerating")
    keys = set(env_fingerprint())
    assert set(GOLDEN_ENV) == keys, (
        f"GOLDEN_ENV keys {sorted(GOLDEN_ENV)} != env_fingerprint keys {sorted(keys)}; "
        "pin every fingerprinted knob (GOLDEN_ENV + _pin_environment)"
    )
    assert set(_PINNED_ATTRS) | {"HERMES_APPS", "connected_business_slugs"} == keys, (
        "_pin_environment doesn't pin every fingerprinted knob"
    )
    for scenario in SCENARIOS:
        path = _fixture_path(scenario.name)
        if not path.exists():
            continue  # reported by test_golden_fixture_set_matches_scenarios
        stored = json.loads(path.read_text(encoding="utf-8")).get("env_fingerprint") or {}
        assert set(stored) == keys, (
            f"{path.name}: fingerprint keys {sorted(stored)} != current {sorted(keys)}; "
            "regenerate the fixtures with UPDATE_GOLDEN=1 from unmodified code"
        )


def test_harness_records_independent_copies() -> None:
    """The recorder must snapshot kwargs, not alias them: _anthropic_turn passes
    the live history list, which stream_turn mutates right after the call."""
    import openai

    live = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    snap = _jsonable({"messages": live, "tools": openai.NOT_GIVEN})
    live.append({"role": "assistant", "content": []})
    live[0]["content"][0]["text"] = "changed"
    assert snap == {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": "<NotGiven>",
    }
