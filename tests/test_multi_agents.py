"""Multi-agents: an agent turn is constrained, not just instructed.

Plan: docs/hermes/MULTI_AGENTS_PLAN.md §9.1 (P1–P9). What must hold:

  * Only the agent's tools (narrowed by the mode's) are offered, so only they
    can run — a forced call to anything else is refused before dispatch.
  * Agent-only tools (reviews_data, weather_forecast) are offered to an agent
    that lists them, run through dispatch, and are never offered to an
    ordinary turn (whose tool list and prompt stay byte-identical).
  * The mode's autonomy reaches build_tools: readonly/draft remove writes;
    `full` in a live chat keeps the approve-every-write flow (autonomy None);
    an unattended caller's stricter autonomy always wins.
  * The mode's allowed_apps reaches build_tools.
  * The ACTIVE AGENT prompt block carries the role and the CURRENT mode only.
  * Every built-in preset validates against the live tool registry.

Offline: providers, retrieval, dispatch and the guardrail are stubbed.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import agent, agent_tools, agents, context_mode, credits, guardrails, health, models
from hermes.agent_presets import BUILTIN_AGENTS, SHARED_RULES, preset_by_slug
from hermes.memory import ConversationStore
from hermes.tools import write_blocked_reason

HAIKU = "claude-haiku-4-5-20251001"
SESSION = "multi-agents"


def _schema(name: str) -> dict[str, Any]:
    return {"name": name, "description": name,
            "input_schema": {"type": "object", "properties": {}}}


#: What the stubbed build_tools offers an ordinary turn.
BUILT = [_schema("search_business_knowledge"), _schema("send_email"),
         _schema("maton_request"), _schema("maton_list_connections"),
         {"type": "web_search_20260209", "name": "web_search"}]

AGENT = {
    "agent_id": "00000000-0000-0000-0000-000000000001",
    "name": "Test Analyst",
    "business": "nh",
    "system_prompt": "You analyse reviews.",
    "tools": ["search_business_knowledge", "maton_request", "reviews_data", "web_search"],
    "default_mode": "report",
    "modes": [
        {"key": "report", "label": "Report", "instructions": "Write the full report.",
         "autonomy": "readonly"},
        {"key": "act", "label": "Act", "instructions": "Update the tracker sheet.",
         "autonomy": "full", "allowed_apps": ["google-sheets"]},
        {"key": "draft", "label": "Draft", "instructions": "Draft only.", "autonomy": "draft"},
        {"key": "kb-only", "label": "KB only", "instructions": "Knowledge base only.",
         "autonomy": "readonly", "tools": ["search_business_knowledge"]},
    ],
    "enabled": True,
}


def _text(t):
    return {"type": "text", "text": t}


def _tool_use(name, block_id, **ti):
    return {"type": "tool_use", "id": block_id, "name": name, "input": ti}


class Harness:
    def __init__(self):
        self.store = ConversationStore()
        self.script: list[dict[str, Any]] = [
            {"content": [_text("done")], "stop_reason": "end_turn"}]
        self.calls: list[dict[str, Any]] = []
        self.dispatched: list[str] = []
        self.build_args: list[dict[str, Any]] = []
        self.prompt_kwargs: list[dict[str, Any]] = []

    def build_tools(self, active_business, allowed_apps=None, autonomy=None):
        self.build_args.append({"business": active_business, "allowed_apps": allowed_apps,
                                "autonomy": autonomy})
        return copy.deepcopy(BUILT)

    def system_prompt(self, **kwargs):
        self.prompt_kwargs.append(kwargs)
        return "system"

    def provider(self, model_id, tools, system, messages, *a, **k):
        self.calls.append({"tools": [t.get("name") for t in tools]})
        step = copy.deepcopy(self.script[min(len(self.calls), len(self.script)) - 1])
        for b in step["content"]:
            if b.get("type") == "text":
                yield {"type": "text", "text": b["text"]}
        yield {"type": "_final", "content": step["content"], "stop_reason": step["stop_reason"],
               "usage": {"input_tokens": 10, "output_tokens": 2}}

    def dispatch(self, name, tool_input, active_business, autonomy=None):
        self.dispatched.append(name)
        return '{"ok": true}', False, None, None

    def run(self, **kw):
        events = list(agent.stream_turn(SESSION, "go", active_business="nh", **kw))
        assert events[-1]["type"] == "done"
        return events[-1]["result"]


@pytest.fixture
def h(monkeypatch):
    credits.clear()
    harness = Harness()
    monkeypatch.setattr(models, "_configured", lambda: HAIKU)
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(agent, "build_tools", harness.build_tools)
    monkeypatch.setattr(agent, "_system_prompt", harness.system_prompt)
    monkeypatch.setattr(health, "guarded", lambda signal, fn, default="": default)
    monkeypatch.setattr(agent, "store", harness.store)
    monkeypatch.setattr(agent, "_anthropic_turn", harness.provider)
    monkeypatch.setattr(agent, "_openai_turn", harness.provider)
    monkeypatch.setattr(agent, "dispatch", harness.dispatch)
    monkeypatch.setattr(guardrails, "check",
                        lambda *a, **k: guardrails.Decision(True, "", None, "allowed"))
    yield harness
    credits.clear()


# ---- P1: the whitelist is the capability boundary ---------------------------
def test_agent_is_offered_only_its_tools_plus_agent_only_ones(h):
    h.run(agent=AGENT)
    offered = set(h.calls[0]["tools"])
    assert offered == {"search_business_knowledge", "maton_request", "maton_list_connections",
                       "web_search", "reviews_data"}
    assert "send_email" not in offered


def test_a_forced_send_email_is_refused_and_never_dispatched(h):
    h.script = [
        {"content": [_tool_use("send_email", "tu_1", to="x@y.z")], "stop_reason": "tool_use"},
        {"content": [_text("I can't send email.")], "stop_reason": "end_turn"},
    ]
    result = h.run(agent=AGENT)
    assert h.dispatched == []
    assert result["tool_calls"] == []


def test_agent_only_tool_is_dispatched_when_listed(h):
    h.script = [
        {"content": [_tool_use("reviews_data", "tu_1", action="dates")], "stop_reason": "tool_use"},
        {"content": [_text("ok")], "stop_reason": "end_turn"},
    ]
    h.run(agent=AGENT)
    assert h.dispatched == ["reviews_data"]


def test_mode_tools_narrow_the_agent_whitelist(h):
    h.run(agent=AGENT, agent_mode="kb-only")
    assert set(h.calls[0]["tools"]) == {"search_business_knowledge"}


def test_ordinary_turn_is_unchanged(h):
    h.run()
    assert set(h.calls[0]["tools"]) == {t["name"] for t in BUILT}
    assert "reviews_data" not in h.calls[0]["tools"]
    assert h.build_args[0] == {"business": "nh", "allowed_apps": None, "autonomy": None}
    assert "agent_block" not in h.prompt_kwargs[0]


# ---- autonomy and apps reach build_tools ------------------------------------
@pytest.mark.parametrize("mode,expected", [("report", "readonly"), ("draft", "draft"), ("act", None)])
def test_mode_autonomy_reaches_build_tools(h, mode, expected):
    h.run(agent=AGENT, agent_mode=mode)
    assert h.build_args[0]["autonomy"] == expected


def test_full_mode_never_loosens_an_unattended_callers_autonomy():
    mode = agents.resolve_mode(AGENT, "act")
    aut, _, _ = agents.turn_plan(AGENT, mode, "readonly", None)
    assert aut == "readonly"
    aut, _, _ = agents.turn_plan(AGENT, agents.resolve_mode(AGENT, "draft"), "full", None)
    assert aut == "draft"


def test_mode_allowed_apps_reach_build_tools(h):
    h.run(agent=AGENT, agent_mode="act")
    assert h.build_args[0]["allowed_apps"] == ["google-sheets"]


def test_unknown_mode_falls_back_to_default(h):
    h.run(agent=AGENT, agent_mode="nope")
    assert h.build_args[0]["autonomy"] == "readonly"
    assert "CURRENT MODE: Report" in h.prompt_kwargs[0]["agent_block"]


# ---- P2/P3: the write gate under agent modes --------------------------------
def test_readonly_mode_refuses_a_sheets_write():
    reason = write_blocked_reason(
        "maton_request", {"app": "google-sheets", "method": "POST", "path": "v4/spreadsheets/x"},
        "readonly")
    assert reason and "Refused" in reason


def test_draft_mode_allows_gmail_draft_but_not_send():
    draft = {"app": "google-mail", "method": "POST", "path": "gmail/v1/users/me/drafts"}
    send = {"app": "google-mail", "method": "POST", "path": "gmail/v1/users/me/drafts/send"}
    assert write_blocked_reason("maton_request", draft, "draft") is None
    assert write_blocked_reason("maton_request", send, "draft")
    assert write_blocked_reason("send_email", {"to": "a@b.c"}, "draft")


# ---- P7: prompt block -------------------------------------------------------
def test_prompt_block_carries_role_mode_and_rules():
    block = agents.prompt_block(AGENT, agents.resolve_mode(AGENT, "report"))
    assert "Test Analyst" in block and "You analyse reviews." in block
    assert "CURRENT MODE: Report" in block and "Write the full report." in block
    assert "READ-ONLY" in block
    assert SHARED_RULES in block
    other = agents.prompt_block(AGENT, agents.resolve_mode(AGENT, "act"))
    assert "Write the full report." not in other and "Update the tracker sheet." in other


def test_system_prompt_places_agent_block_only_when_given():
    plain = agent._system_prompt(active_business="nh")
    with_agent = agent._system_prompt(active_business="nh", agent_block="ACTIVE AGENT — test")
    assert "ACTIVE AGENT" not in plain
    assert "ACTIVE AGENT — test" in with_agent
    assert with_agent.replace("\n\nACTIVE AGENT — test", "") == plain


# ---- validation and presets -------------------------------------------------
def test_every_builtin_preset_validates():
    known = agents.known_tool_names()
    for p in BUILTIN_AGENTS:
        clean, err = agents.validate(p)
        assert err is None, f"{p['slug']}: {err}"
        assert set(clean["tools"]) <= known
        assert clean["default_mode"] == p["default_mode"]
        assert [m["key"] for m in clean["modes"]] == [m["key"] for m in p["modes"]]


def test_presets_never_pair_send_tools_with_draft_modes():
    """Draft mode strips send_email; a preset offering it would only confuse."""
    for p in BUILTIN_AGENTS:
        assert "send_email" not in p["tools"], p["slug"]


def test_validate_rejects_unknown_and_excluded_tools():
    base = {"name": "X", "system_prompt": "role", "modes": []}
    _, err = agents.validate({**base, "tools": ["launch_rockets"]})
    assert err and "launch_rockets" in err
    _, err = agents.validate({**base, "tools": ["set_guardrail"]})
    assert err and "set_guardrail" in err


def test_validate_rejects_mode_tools_outside_agent_tools():
    _, err = agents.validate({"name": "X", "system_prompt": "r", "tools": ["web_search"],
                              "modes": [{"label": "M", "tools": ["send_email"]}]})
    assert err and "send_email" in err


def test_validate_fills_a_default_mode_and_requires_a_prompt():
    clean, err = agents.validate({"name": "X", "system_prompt": "r", "tools": []})
    assert err is None and clean["modes"][0]["key"] == "default"
    assert clean["default_mode"] == "default"
    _, err = agents.validate({"name": "X", "system_prompt": "  ", "tools": []})
    assert err


def test_tool_catalog_groups_cover_agent_only_tools():
    names = {t["name"] for g in agents.tool_catalog() for t in g["tools"]}
    assert {"reviews_data", "weather_forecast"} <= names
    assert not names & agents.EXCLUDED_TOOLS


# ---- P9: reviews_data is Nour Hammam's ---------------------------------------
def test_reviews_data_refuses_other_businesses_before_touching_storage(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("storage must not be read")

    monkeypatch.setattr(agent_tools, "_available", boom)
    content, is_error = agent_tools.dispatch("reviews_data", {"action": "dates"}, "gak")
    assert is_error and "Nour Hammam" in json.loads(content)["error"]


# ---- P6: built-ins can't be deleted ------------------------------------------
def test_builtin_delete_is_refused(monkeypatch):
    from hermes.db import agents as agents_db

    monkeypatch.setattr(agents_db, "get_agent", lambda ref: {**AGENT, "is_builtin": True})
    with pytest.raises(agents_db.AgentError):
        agents_db.delete_agent(AGENT["agent_id"])


def test_reset_needs_a_builtin_preset(monkeypatch):
    from hermes.db import agents as agents_db

    monkeypatch.setattr(agents_db, "get_agent", lambda ref: {**AGENT, "is_builtin": False, "slug": "x"})
    with pytest.raises(agents_db.AgentError):
        agents_db.reset_agent(AGENT["agent_id"])
    assert preset_by_slug("nh-market-analyst")["business"] == "nh"
