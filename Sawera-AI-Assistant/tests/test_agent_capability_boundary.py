"""The tool list a provider call is offered is the capability boundary.

TURN_COST_PLAN step 0.2 (bug B7). Before it, the agent loop ran ANY name in
LOCAL_TOOLS the model asked for, whether or not that call had been offered the
tool — so leaving `send_email` out of a request (an app a scheduled task isn't
allowed, Context off, a later per-turn tool subset) only hid it; a model that
named it anyway, from an earlier turn or a prompt injection in a tool result,
still sent the email. And a name that wasn't local at all was skipped with no
tool_result, leaving a dangling tool_use in the history that the next provider
call rejects.

What must hold:

  * A tool_use whose name was not offered on that call, or is not a tool Hermes
    runs, gets an is_error tool_result and never reaches guardrails.check or
    dispatch.
  * The model is told once per name per turn. Asking again ends the turn (with
    the last assistant text) instead of resending the whole ~16k-token prefix
    up to HERMES_MAX_TOOL_ITERATIONS times.
  * Offered tools still go through the guardrail and run, and Anthropic's
    server-side tools (server_tool_use blocks, executed by Anthropic) are left
    exactly as before.
  * No path leaves a tool_use without its tool_result in the stored history.

Offline: providers, retrieval, dispatch and the guardrail are all stubbed, and
each case runs on both wire protocols (Anthropic, and OpenAI-protocol via NIM).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import agent, context_mode, credits, guardrails, health, models
from hermes.memory import ConversationStore

HAIKU = "claude-haiku-4-5-20251001"  # Anthropic wire protocol
KIMI = "moonshotai/kimi-k3"  # OpenAI wire protocol (NVIDIA NIM)
SESSION = "capability-boundary"

#: The one local tool every turn here is offered.
OFFERED = {
    "name": "search_business_knowledge",
    "description": "Search the business knowledge base.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}
#: Anthropic's server-side web search, offered the way build_tools offers it.
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}

EMAIL_ARGS = {"to": "tenant@example.com", "subject": "Rent", "body": "Please pay."}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use(name: str, block_id: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def _unavailable(block_id: str, name: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": block_id,
        "content": f"Tool {name} is not available on this turn.",
        "is_error": True,
    }


class Harness:
    """Scripted provider plus spies on everything a tool call could reach."""

    def __init__(self, model: str):
        self.model = model
        self.store = ConversationStore()
        #: Each provider response, in order. Once the script runs out the last
        #: entry repeats — a model that keeps doing the same thing.
        self.script: list[dict[str, Any]] = []
        #: What each provider call was handed (deep copies: the loop keeps
        #: appending to the live `messages` list after the call returns).
        self.calls: list[dict[str, Any]] = []
        self.dispatched: list[str] = []
        self.checked: list[str] = []

    def provider(self, model_id, tools, system, messages, *args, **kwargs):
        """Stands in for both `_anthropic_turn` and `_openai_turn`."""
        self.calls.append({
            "model": model_id,
            "tools": [t.get("name") for t in tools],
            "messages": copy.deepcopy(messages),
        })
        step = copy.deepcopy(self.script[min(len(self.calls), len(self.script)) - 1])
        for block in step["content"]:
            if block.get("type") == "text":
                yield {"type": "text", "text": block["text"]}
        yield {
            "type": "_final",
            "content": step["content"],
            "stop_reason": step["stop_reason"],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

    def dispatch(self, name, tool_input, active_business, autonomy=None,
                 session_id=None):
        self.dispatched.append(name)
        return '{"results": []}', False, None, None

    def check(self, session_id, tool_name, tool_input, active_business=None,
              interactive=True, turn=None, approved_tokens=None):
        self.checked.append(tool_name)
        return guardrails.Decision(True, "", None, "allowed")

    def run(self, message: str = "Email the overdue tenants a rent reminder."):
        events = list(agent.stream_turn(SESSION, message))
        assert events[-1]["type"] == "done"
        return events, events[-1]["result"]

    def stored(self) -> list[dict[str, Any]]:
        return self.store.get(SESSION)


def _harness(monkeypatch, model: str) -> Harness:
    credits.clear()
    h = Harness(model)
    monkeypatch.setattr(models, "_configured", lambda: model)
    monkeypatch.setattr(context_mode, "enabled", lambda: True)
    monkeypatch.setattr(agent, "build_tools", lambda *a, **k: [OFFERED, WEB_SEARCH])
    monkeypatch.setattr(agent, "_system_prompt", lambda *a, **k: "system")
    # Retrieval is not under test: every block comes back empty without running.
    monkeypatch.setattr(health, "guarded", lambda signal, fn, default="": default)
    monkeypatch.setattr(agent, "store", h.store)
    monkeypatch.setattr(agent, "_anthropic_turn", h.provider)
    monkeypatch.setattr(agent, "_openai_turn", h.provider)
    monkeypatch.setattr(agent, "dispatch", h.dispatch)
    monkeypatch.setattr(guardrails, "check", h.check)
    return h


@pytest.fixture(params=[HAIKU, KIMI], ids=["anthropic", "openai-protocol"])
def h(request, monkeypatch):
    harness = _harness(monkeypatch, request.param)
    yield harness
    credits.clear()


def _results_sent(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tool_result block in the history one provider call was handed."""
    return [
        block
        for m in call["messages"]
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_result"
    ]


def _assert_every_tool_use_answered(messages: list[dict[str, Any]]) -> None:
    """Each tool_use is answered, in the very next message, by a tool_result
    carrying its id — the pairing both wire protocols enforce on the next call."""
    for i, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        ids = {b["id"] for b in message["content"] if b.get("type") == "tool_use"}
        if not ids:
            continue
        assert i + 1 < len(messages), f"history ends on unanswered tool_use {sorted(ids)}"
        answered = {
            b.get("tool_use_id")
            for b in messages[i + 1]["content"]
            if b.get("type") == "tool_result"
        }
        assert ids <= answered, f"dangling tool_use {sorted(ids - answered)}"


# ---- the boundary ---------------------------------------------------------
def test_dispatchable_set_is_offered_and_local():
    """Server-side tools are offered but run by Anthropic; a local tool that
    wasn't offered is exactly the leak. Neither may be dispatched."""
    assert "send_email" in agent.LOCAL_TOOLS  # local, so the old loop would run it
    assert agent._dispatchable_tools([OFFERED, WEB_SEARCH]) == {"search_business_knowledge"}
    assert agent._dispatchable_tools([]) == frozenset()


def test_an_unoffered_send_email_is_refused_and_never_runs(h):
    h.script = [
        {"content": [_text("I'll email them now."), _tool_use("send_email", "tu_1", **EMAIL_ARGS)],
         "stop_reason": "tool_use"},
        {"content": [_text("I can't send email on this turn.")], "stop_reason": "end_turn"},
    ]

    events, result = h.run()

    assert "send_email" not in h.calls[0]["tools"]
    assert h.checked == []
    assert h.dispatched == []
    assert len(h.calls) == 2
    assert _results_sent(h.calls[1]) == [_unavailable("tu_1", "send_email")]
    # Nothing ran, so nothing may tell the UI a tool is running or ran.
    assert [e for e in events if e["type"] == "tool_call"] == []
    assert result["tool_calls"] == []
    assert result["reply"] == "I can't send email on this turn."
    _assert_every_tool_use_answered(h.stored())


@pytest.mark.parametrize("name", ["web_search", "launch_rockets"])
def test_an_unknown_name_gets_an_error_result_not_a_dangling_tool_use(h, name):
    """`web_search` as a plain tool_use is a model inventing it (on the OpenAI
    path it is not even offered); `launch_rockets` exists nowhere. The old loop
    skipped both silently, broke out, and stored the dangling tool_use."""
    h.script = [
        {"content": [_tool_use(name, "tu_1", query="rent law")], "stop_reason": "tool_use"},
        {"content": [_text("Answered without it.")], "stop_reason": "end_turn"},
    ]

    _, result = h.run()

    assert h.checked == []
    assert h.dispatched == []
    assert len(h.calls) == 2
    assert _results_sent(h.calls[1]) == [_unavailable("tu_1", name)]
    assert result["reply"] == "Answered without it."
    _assert_every_tool_use_answered(h.stored())


def test_a_repeated_unavailable_tool_is_refused_once_then_the_turn_stops(h):
    """Three attempts at the same unoffered tool: one is_error reaches the
    model, the second attempt ends the turn, and there is no third call."""
    h.script = [
        {"content": [_text("Sending the reminder."), _tool_use("send_email", f"tu_{n}", **EMAIL_ARGS)],
         "stop_reason": "tool_use"}
        for n in (1, 2, 3)
    ]

    _, result = h.run()

    assert len(h.calls) <= 2
    sent_errors = {
        (b["tool_use_id"], b["content"])
        for call in h.calls
        for b in _results_sent(call)
        if b.get("is_error")
    }
    assert sent_errors == {("tu_1", "Tool send_email is not available on this turn.")}
    assert h.checked == []
    assert h.dispatched == []
    # Ends like the old "nothing to dispatch" break: with the last assistant text.
    assert result["reply"] == "Sending the reminder."
    # The stopped attempt is still closed out, so the history stays valid...
    stored = h.stored()
    _assert_every_tool_use_answered(stored)
    assert stored[-1]["content"] == [_unavailable("tu_2", "send_email")]

    # ...and the next turn's request carries no dangling tool_use either.
    h.calls.clear()
    h.script = [{"content": [_text("Hello again.")], "stop_reason": "end_turn"}]
    _, result = h.run("hi")
    assert result["reply"] == "Hello again."
    assert len(h.calls) == 1
    _assert_every_tool_use_answered(h.calls[0]["messages"])


def test_a_repeat_stops_the_turn_before_an_offered_tool_beside_it_runs(h):
    """The retry cap ends the turn without another call, so the model would
    never see an offered tool's result — that tool is not run either, and is
    answered so the history stays valid."""
    h.script = [
        {"content": [_tool_use("send_email", "tu_1", **EMAIL_ARGS)], "stop_reason": "tool_use"},
        {"content": [_text("Trying both."),
                     _tool_use("search_business_knowledge", "tu_2", query="rent"),
                     _tool_use("send_email", "tu_3", **EMAIL_ARGS)],
         "stop_reason": "tool_use"},
    ]

    _, result = h.run()

    assert len(h.calls) == 2
    assert h.checked == []
    assert h.dispatched == []
    assert result["reply"] == "Trying both."
    closing = h.stored()[-1]["content"]
    assert [b["tool_use_id"] for b in closing] == ["tu_2", "tu_3"]
    assert all(b["is_error"] for b in closing)
    assert closing[1] == _unavailable("tu_3", "send_email")
    _assert_every_tool_use_answered(h.stored())


def test_two_blocks_for_the_same_unavailable_tool_in_one_message_are_not_a_repeat(h):
    """The cap counts round trips, not blocks: both parallel calls must be
    answered (the protocol requires a result per tool_use), and the model has
    not yet been told, so it gets its one chance to answer without the tool."""
    h.script = [
        {"content": [_tool_use("send_email", "tu_1", **EMAIL_ARGS),
                     _tool_use("send_email", "tu_2", **EMAIL_ARGS)],
         "stop_reason": "tool_use"},
        {"content": [_text("I can't send email here.")], "stop_reason": "end_turn"},
    ]

    _, result = h.run()

    assert len(h.calls) == 2
    assert _results_sent(h.calls[1]) == [
        _unavailable("tu_1", "send_email"),
        _unavailable("tu_2", "send_email"),
    ]
    assert h.dispatched == []
    assert result["reply"] == "I can't send email here."


# ---- what must NOT change -------------------------------------------------
def test_an_offered_tool_still_passes_the_guardrail_and_runs(h):
    """Positive control: the spies above would also be empty if nothing ran
    at all."""
    h.script = [
        {"content": [_tool_use("search_business_knowledge", "tu_1", query="rent")],
         "stop_reason": "tool_use"},
        {"content": [_text("Rent is up to date.")], "stop_reason": "end_turn"},
    ]

    events, result = h.run()

    assert h.checked == ["search_business_knowledge"]
    assert h.dispatched == ["search_business_knowledge"]
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == ["search_business_knowledge"]
    assert _results_sent(h.calls[1]) == [{
        "type": "tool_result", "tool_use_id": "tu_1",
        "content": '{"results": []}', "is_error": False,
    }]
    assert [t["tool"] for t in result["tool_calls"]] == ["search_business_knowledge"]
    assert result["reply"] == "Rent is up to date."


def test_offered_and_unoffered_in_one_message(h):
    """Each block is judged on its own: the offered one runs, the other is
    refused, and both results go back together in the block order."""
    h.script = [
        {"content": [_tool_use("search_business_knowledge", "tu_1", query="arrears"),
                     _tool_use("send_email", "tu_2", **EMAIL_ARGS)],
         "stop_reason": "tool_use"},
        {"content": [_text("Here are the arrears; I can't email them.")], "stop_reason": "end_turn"},
    ]

    _, result = h.run()

    assert h.checked == ["search_business_knowledge"]
    assert h.dispatched == ["search_business_knowledge"]
    sent = _results_sent(h.calls[1])
    assert [b["tool_use_id"] for b in sent] == ["tu_1", "tu_2"]
    assert sent[0]["is_error"] is False
    assert sent[1] == _unavailable("tu_2", "send_email")
    assert result["reply"] == "Here are the arrears; I can't email them."


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use"])
def test_server_side_tool_blocks_are_left_to_anthropic(monkeypatch, stop_reason):
    """Anthropic runs web_search itself and returns `server_tool_use` plus its
    result in the same content. Those are never dispatched, refused or
    answered; a tool_use stop carrying only them still just ends the loop."""
    h = _harness(monkeypatch, HAIKU)
    content = [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "rent law"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
        _text("Here is what I found."),
    ]
    h.script = [{"content": content, "stop_reason": stop_reason}]

    _, result = h.run()

    credits.clear()
    assert len(h.calls) == 1
    assert h.checked == []
    assert h.dispatched == []
    assert result["reply"] == "Here is what I found."
    stored = h.stored()
    assert stored[-1] == {"role": "assistant", "content": content}
