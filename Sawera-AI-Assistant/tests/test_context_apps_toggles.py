"""Business context and apps access are two INDEPENDENT switches.

The bug this file exists to prevent: one toggle used to do both jobs, so
switching business context off also stripped every tool schema and left Hermes
unable to touch the user's apps. Turning off what the agent KNOWS must not turn
off what it can DO.

Self-contained and offline — no API keys, no database. Run directly:

    python tests/test_context_apps_toggles.py

or under pytest:

    pytest tests/test_context_apps_toggles.py -q
"""
from __future__ import annotations

import os
import sys

# Force the in-memory settings path BEFORE importing anything from hermes, so
# the flags use their process-global override and never touch a real Postgres.
os.environ["HERMES_DISABLE_DB"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from hermes import agent, apps_access, context_mode  # noqa: E402

client = TestClient(app_module.app)

# A representative line from each part of the full prompt, used to assert what a
# given combination of switches actually ships.
TOOL_MARKER = "maton_request"          # only present when tools are sent
NO_TOOLS_MARKER = "apps access is switched off"
PACK = "Billing runs on the 1st."
MEMORY = "Prefers short answers."


def _restore():
    """Both switches back to their default (on), for the next test."""
    context_mode.set_enabled(True)
    apps_access.set_enabled(True)


def _prompt(*, tools: bool, rich: bool) -> str:
    return agent._system_prompt(
        voice=False,
        active_business="acme",
        memory_block=MEMORY if rich else "",
        attachments=None,
        context_pack=PACK if rich else "",
        retrieved_context="",
        conversation_block="",
        tools_available=tools,
        rich_context=rich,
    )


# ---- The flags themselves -------------------------------------------------
def test_both_default_on():
    _restore()
    assert context_mode.enabled() is True
    assert apps_access.enabled() is True


def test_context_off_leaves_apps_on():
    """The headline requirement: business context off, apps still reachable."""
    _restore()
    context_mode.set_enabled(False)
    assert context_mode.enabled() is False
    assert apps_access.enabled() is True, "turning context off must not disable apps"
    _restore()


def test_apps_off_leaves_context_on():
    _restore()
    apps_access.set_enabled(False)
    assert apps_access.enabled() is False
    assert context_mode.enabled() is True, "turning apps off must not disable context"
    _restore()


def test_flags_do_not_share_storage():
    """Distinct setting keys — otherwise one would silently overwrite the other."""
    assert context_mode._SETTING_KEY != apps_access._SETTING_KEY


# ---- The prompt each combination produces ---------------------------------
def test_prompt_full_has_tools_and_context():
    p = _prompt(tools=True, rich=True)
    assert TOOL_MARKER in p
    assert PACK in p and MEMORY in p


def test_prompt_context_off_apps_on_keeps_tools():
    """Context off must NOT cost the agent its tools."""
    p = _prompt(tools=True, rich=False)
    assert TOOL_MARKER in p, "apps are on — tool instructions must survive context off"
    assert NO_TOOLS_MARKER not in p
    assert PACK not in p and MEMORY not in p


def test_prompt_apps_off_context_on_keeps_context():
    """Apps off must NOT cost the agent its business knowledge."""
    p = _prompt(tools=False, rich=True)
    assert NO_TOOLS_MARKER in p
    assert TOOL_MARKER not in p, "no tools are sent — tool instructions would be a lie"
    assert PACK in p and MEMORY in p, "context is on — the blocks must still be attached"


def test_prompt_both_off_is_plain_and_small():
    p = _prompt(tools=False, rich=False)
    assert NO_TOOLS_MARKER in p
    assert TOOL_MARKER not in p
    assert PACK not in p and MEMORY not in p
    full = _prompt(tools=True, rich=True)
    assert len(p) < len(full) / 4, "plain chat should be a fraction of the full prompt"


# ---- The API --------------------------------------------------------------
def test_context_endpoint_roundtrip():
    _restore()
    assert client.post("/api/settings/context", json={"enabled": False}).json() == {"enabled": False}
    got = client.get("/api/settings/context").json()
    assert got["enabled"] is False
    # The GET also tells the Settings UI what "off" does (TURN_COST_PLAN 0.3).
    assert isinstance(got["split_toggles"], bool)
    assert client.post("/api/settings/context", json={"enabled": True}).json() == {"enabled": True}


def test_apps_endpoint_roundtrip():
    _restore()
    assert client.post("/api/settings/apps", json={"enabled": False}).json() == {"enabled": False}
    assert client.get("/api/settings/apps").json() == {"enabled": False}
    assert client.post("/api/settings/apps", json={"enabled": True}).json() == {"enabled": True}


def test_context_endpoint_does_not_move_apps():
    _restore()
    client.post("/api/settings/context", json={"enabled": False})
    assert client.get("/api/settings/apps").json()["enabled"] is True
    _restore()


def test_index_renders_both_toggles():
    _restore()
    html = client.get("/").text
    assert 'id="context-toggle-input"' in html
    assert 'id="apps-toggle-input"' in html

    apps_access.set_enabled(False)
    html = client.get("/").text
    # The apps checkbox loses `checked`; the context one keeps it.
    apps_input = html.split('id="apps-toggle-input"')[1].split(">")[0]
    ctx_input = html.split('id="context-toggle-input"')[1].split(">")[0]
    assert "checked" not in apps_input
    assert "checked" in ctx_input
    _restore()


# ---- The turn: which switch decides the tool list -------------------------
def test_tools_are_built_from_apps_flag_not_context_flag():
    """stream_turn must consult apps_access for tools, context_mode for blocks."""
    calls: list[bool] = []
    real_build = agent.build_tools
    real_turns = (agent._anthropic_turn, agent._openai_turn)

    def spy(*a, **kw):
        calls.append(True)
        return real_build(*a, **kw)

    agent.build_tools = spy
    # Both wire protocols are stubbed, so the turn never leaves the process.
    # hermes/config.py loads the developer's REAL keys from .env, so an
    # unstubbed turn here was a real (and, once tools are built, ~16k-token)
    # paid call on whatever HERMES_MODEL names.
    agent._anthropic_turn = _fake_provider_turn
    agent._openai_turn = _fake_provider_turn
    # Context off + Apps on keeping the tools is the new state behind
    # HERMES_SPLIT_TOGGLES (TURN_COST_PLAN step 0.3); with the flag off, Context
    # off still strips the tools as it always has (tests/test_prompt_invariants.py
    # covers that default). Set for this test only — Config reads it per call.
    split_before = os.environ.get("HERMES_SPLIT_TOGGLES")
    os.environ["HERMES_SPLIT_TOGGLES"] = "on"
    try:
        _restore()
        context_mode.set_enabled(False)   # context off, apps still on
        calls.clear()
        list(agent.stream_turn("t-ctx-off", "hi"))
        assert calls, "context off but apps on: tools must still be built"

        apps_access.set_enabled(False)    # now apps off too
        calls.clear()
        list(agent.stream_turn("t-apps-off", "hi"))
        assert not calls, "apps off: no tools may be built"
    finally:
        agent.build_tools = real_build
        agent._anthropic_turn, agent._openai_turn = real_turns
        if split_before is None:
            os.environ.pop("HERMES_SPLIT_TOGGLES", None)
        else:
            os.environ["HERMES_SPLIT_TOGGLES"] = split_before
        _restore()


def _fake_provider_turn(model_id, tools, system, messages, *args, **kwargs):
    """Stand-in for both `_anthropic_turn` and `_openai_turn`: one clean,
    tool-free reply in the shared `_final` event shape. This test only cares
    which switch decided the tool list, not what a model would answer, so the
    turn completes offline and any exception it raises is a real failure."""
    yield {"type": "text", "text": "Hello."}
    yield {
        "type": "_final",
        "content": [{"type": "text", "text": "Hello."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
