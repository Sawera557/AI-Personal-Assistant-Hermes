"""Prompt invariants across every combination of the two Settings switches.

TURN_COST_PLAN step 0.3 splits one switch into two (Apps access decides the
tools, Business context the retrieval blocks) and adds a fourth prompt variant
for each pairing. That multiplies the ways a turn can go out wrong — and the
dangerous one is quiet: a turn that OFFERS tools while missing the rules that
make tools safe. So this file drives the real `stream_turn` through every
combination of

    apps × context × HERMES_SPLIT_TOGGLES × HERMES_STANDING_RULES_CORE
         × interactive × voice × business scope (None / 'all' / locked) × pack

and checks the request the provider would actually receive, against the hard
invariants in TURN_COST_PLAN §2:

  * any turn that sends a dispatchable tool (a `tools.LOCAL_TOOLS` member)
    carries GROUNDING & ATTRIBUTION, the truncation-honesty rule and the
    confirm-before-write / approval_token paragraph;
  * it also carries the owner's Hard rules and Security sections — from the
    pack block when that is loaded, else from the standing-rules core — in
    every state where the core applies (Context off with the split on, or an
    empty pack with HERMES_STANDING_RULES_CORE on). The one state where they are
    still missing by default (Context on, pack empty, core flag off) is pinned
    here too, so the default payload can't change by accident (it is B24, an
    owner decision: §8 Q5);
  * the business-scope note appears exactly when `active_business` is set;
  * Business context off means no pack body, memory, retrieved documents or
    recall in the prompt — for interactive turns. Unattended turns
    (interactive=False, the scheduler) always get full context;
  * a turn that sends NO tools has no tool guidance at all, and says which
    switch to flip.

Offline: the provider clients are faked at their factories (so the real
`_anthropic_turn` builds the real kwargs), retrieval returns fixed samples, and
the DB-backed pack/memory readers are stubbed.
"""
from __future__ import annotations

import itertools
import types
from dataclasses import dataclass
from typing import Any

import pytest

from hermes import agent, apps_access, context_mode, context_packs, credits, models
from hermes.memory import ConversationStore
from hermes.tools import LOCAL_TOOLS

HAIKU = "claude-haiku-4-5-20251001"
KIMI = "moonshotai/kimi-k3"
REPLY = "Hello."

# Sample retrieval blocks, each with a marker no prompt text could contain.
PACK_BODY = "PACKBODY: billing runs on the 1st."
GLOBAL_PACK = (
    "<!-- provenance comment that must never reach a prompt -->\n"
    "# OWNER — STANDING FACTS\n\n"
    "## Who you work for\nPACKBODY owner profile.\n\n"
    "## Hard rules (non-negotiable)\n1. SAMPLE-HARD-RULE: money only from live data.\n\n"
    "## Calendar anchors\nPACKBODY cadence.\n\n"
    "## Security\nSAMPLE-SECURITY-RULE: never write out credentials.\n"
)
SEMANTIC_MEMORY = "SEMANTICMEM: prefers short answers."
ALWAYS_MEMORY = "Standing rules & preferences:\n  - ALWAYSMEM: CC the owner on invoices."
RAG = "RETRIEVED CONTEXT:\n- RAGDOC: unit 4B rent due on the 1st."
RECALL = "EARLIER CONVERSATIONS:\n- RECALLED: remind me about rent on Mondays."
#: Business data that must never reach a turn with Business context off.
CONTEXT_MARKERS = ("PACKBODY", "SEMANTICMEM", "RAGDOC", "RECALLED")

GROUNDING = "GROUNDING & ATTRIBUTION"
TRUNCATION = "_truncation"
APPROVAL = "approval_token"
SCOPE_NOTE = "Business scope:"
APPS_OFF = "apps access is switched off"
LEGACY_CONTEXT_OFF = "business context is switched off for speed and cost"


# ---- Harness --------------------------------------------------------------
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


def _openai_chunks() -> list[Any]:
    def choice(content: str | None, finish: str | None) -> Any:
        return types.SimpleNamespace(
            delta=types.SimpleNamespace(content=content, tool_calls=None),
            finish_reason=finish,
        )

    return [
        types.SimpleNamespace(usage=None, choices=[choice(REPLY, None)]),
        types.SimpleNamespace(usage=None, choices=[choice(None, "stop")]),
        types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=2), choices=[],
        ),
    ]


@dataclass(frozen=True)
class Sent:
    """What one provider call was handed, normalised across both protocols."""

    system: str
    tool_names: list[str]
    messages: list[dict[str, Any]]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = HAIKU,
    apps: bool = True,
    context: bool = True,
    split: bool = False,
    core: bool = False,
    pack: bool = True,
) -> tuple[list[Sent], ConversationStore]:
    import hermes.conversation_retrieve as conversation_retrieve
    import hermes.db.conversation_memory as conversation_memory
    import hermes.memory as memory
    import hermes.rag as rag

    sent: list[Sent] = []

    def anthropic_stream(**kw: Any) -> _FakeAnthropicStream:
        sent.append(Sent(
            system=kw["system"],
            tool_names=[t.get("name") for t in kw["tools"]],
            messages=[dict(m) for m in kw["messages"]],
        ))
        return _FakeAnthropicStream()

    def openai_create(**kw: Any):
        msgs = kw["messages"]
        tools = kw["tools"]
        sent.append(Sent(
            system=msgs[0]["content"],
            tool_names=[] if not isinstance(tools, list) else [t["function"]["name"] for t in tools],
            messages=[dict(m) for m in msgs[1:]],
        ))
        return iter(_openai_chunks())

    anthropic_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=anthropic_stream))
    openai_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=openai_create))
    )
    monkeypatch.setattr(agent, "_anthropic", lambda: anthropic_client)
    monkeypatch.setattr(agent, "_openai", lambda: openai_client)
    monkeypatch.setattr(agent, "_gemini", lambda: openai_client)
    monkeypatch.setattr(agent, "_nvidia", lambda model_id: openai_client)

    # Retrieval: fixed samples, never a DB or network call.
    monkeypatch.setattr(agent, "build_memory_block", lambda **kw: SEMANTIC_MEMORY)
    monkeypatch.setattr(agent, "build_always_block", lambda **kw: ALWAYS_MEMORY)
    monkeypatch.setattr(
        context_packs, "packs_for", lambda *a, **kw: (GLOBAL_PACK + "\n" + PACK_BODY) if pack else "",
    )
    # The standing-rules core reads the LOADED global pack; let it "load" ours.
    monkeypatch.setattr(context_packs, "_db_off", lambda: False)
    monkeypatch.setattr(
        context_packs, "_cached_pack", lambda slug: GLOBAL_PACK if (pack and slug == "global") else "",
    )
    monkeypatch.setattr(rag, "build_context_block", lambda **kw: RAG)
    monkeypatch.setattr(conversation_retrieve, "build_history_block", lambda **kw: RECALL)
    monkeypatch.setattr(conversation_memory, "owner_user_id", lambda: "owner")

    credits.clear()
    monkeypatch.setattr(models, "_configured", lambda: model)
    monkeypatch.setattr(apps_access, "enabled", lambda: apps)
    monkeypatch.setattr(context_mode, "enabled", lambda: context)
    for name, on in (("HERMES_SPLIT_TOGGLES", split), ("HERMES_STANDING_RULES_CORE", core)):
        if on:
            monkeypatch.setenv(name, "on")
        else:
            monkeypatch.delenv(name, raising=False)

    store = ConversationStore()
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(memory, "store", store)
    return sent, store


def _turn(*, session: str = "inv", text: str = "hi", **kw: Any) -> dict[str, Any]:
    events = list(agent.stream_turn(session, [{"type": "text", "text": text}], **kw))
    result = events[-1]["result"]
    assert result["reply"] == REPLY, result
    return result


# ---- The matrix -----------------------------------------------------------
BOOL = (False, True)
SCOPES = (None, "all", "nh")
MATRIX = list(itertools.product(BOOL, BOOL, BOOL, BOOL, BOOL, BOOL, SCOPES, BOOL))


def _id(case: tuple) -> str:
    apps, ctx, split, core, interactive, voice, scope, pack = case
    return "-".join([
        f"apps{'On' if apps else 'Off'}", f"ctx{'On' if ctx else 'Off'}",
        f"split{'On' if split else 'Off'}", f"core{'On' if core else 'Off'}",
        "chat" if interactive else "unattended", "voice" if voice else "text",
        f"scope{scope}", "pack" if pack else "nopack",
    ])


@pytest.mark.parametrize("case", MATRIX, ids=[_id(c) for c in MATRIX])
def test_prompt_invariants(case: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    apps, ctx, split, core, interactive, voice, scope, pack = case
    sent, _ = _install(monkeypatch, apps=apps, context=ctx, split=split, core=core, pack=pack)
    _turn(voice=voice, active_business=scope, interactive=interactive)
    assert len(sent) == 1
    call = sent[0]
    system = call.system

    # What the switches should mean, worked out independently of the agent.
    context_in_force = ctx or not interactive      # unattended ignores Context
    tools_expected = apps and (context_in_force or split)
    dispatchable = [n for n in call.tool_names if n in LOCAL_TOOLS]

    assert bool(call.tool_names) == tools_expected, (
        "Apps access decides the tools; Context off strips them only while "
        "HERMES_SPLIT_TOGGLES is off, and never for an unattended run"
    )

    if dispatchable:
        # Hard invariant 1: the rules that make tools safe travel with them.
        for marker in (GROUNDING, TRUNCATION, APPROVAL):
            assert marker in system, f"tools sent without {marker!r}"
        assert APPS_OFF not in system

        # Hard invariant 2: the owner's Hard rules and Security sections.
        core_applies = (not context_in_force) or (not pack and core)
        if context_in_force and pack:
            # Carried by the pack block itself; no duplicate core.
            assert "SAMPLE-HARD-RULE" in system and "SAMPLE-SECURITY-RULE" in system
            assert "STANDING RULES" not in system
        elif core_applies:
            assert "STANDING RULES" in system
            assert "Hard rules" in system and "Security" in system
            if pack:
                # Cut from the LOADED global pack, not the generic copy...
                assert "SAMPLE-HARD-RULE" in system and "SAMPLE-SECURITY-RULE" in system
                assert context_packs.FALLBACK_STANDING_RULES not in system
                # ...and only those two sections of it.
                assert "PACKBODY" not in system
                assert "provenance comment" not in system
            else:
                assert context_packs.FALLBACK_STANDING_RULES in system
        else:
            # Context on, pack empty, HERMES_STANDING_RULES_CORE off: today's
            # default payload, unchanged on purpose until the owner decides
            # (§8 Q5). Pinned so it can't change by accident either way.
            assert "STANDING RULES" not in system

        # The always-inject memories come with the core only when the memory
        # block (which already holds them) is off.
        if context_in_force:
            assert "ALWAYSMEM" not in system
        else:
            assert "ALWAYSMEM" in system and "STANDING MEMORIES" in system
    else:
        # No tools: no tool guidance at all, and no core either.
        assert not [n for n in LOCAL_TOOLS if n in system], "tool instructions with no tools"
        assert APPROVAL not in system and TRUNCATION not in system
        assert "STANDING RULES" not in system and "ALWAYSMEM" not in system
        # ADVISOR (context in force) keeps its own GROUNDING for the blocks it
        # carries; plain chat carries no blocks to ground.
        assert (GROUNDING in system) == context_in_force
        if not apps:
            assert APPS_OFF in system, "the prompt must name the switch that is off"
        else:
            # Apps on but no tools = the legacy Context-off coupling.
            assert LEGACY_CONTEXT_OFF in system

    # Business-scope note: exactly when a scope is set (and the prompt is not
    # the scope-free plain chat).
    plain = not call.tool_names and not context_in_force
    if plain:
        assert SCOPE_NOTE not in system
    else:
        assert (SCOPE_NOTE in system) == (scope is not None)

    # Business context: the blocks are there exactly when it is in force.
    for marker in CONTEXT_MARKERS:
        if marker == "PACKBODY" and not pack:
            assert marker not in system
            continue
        assert (marker in system) == context_in_force, (
            f"{marker}: Business context {'on' if context_in_force else 'off'} for this turn"
        )

    assert system.endswith(agent._VOICE_STYLE) == voice


def test_matrix_is_complete() -> None:
    """Guards the guard: 2^6 switch/flag combinations × 3 scopes × pack."""
    assert len(MATRIX) == 2 ** 7 * 3
    assert len(set(MATRIX)) == len(MATRIX)


# ---- Both wire protocols carry the same variant ---------------------------
@pytest.mark.parametrize("model", [HAIKU, KIMI], ids=["anthropic", "openai-protocol"])
@pytest.mark.parametrize(
    "apps,ctx,split",
    [(True, True, False), (True, False, False), (True, False, True), (False, True, False), (False, False, False)],
    ids=["full", "ctxOff-legacy", "ctxOff-split", "advisor", "plain"],
)
def test_each_protocol_sends_the_same_variant(
    model: str, apps: bool, ctx: bool, split: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, _ = _install(monkeypatch, model=model, apps=apps, context=ctx, split=split)
    _turn(active_business="all")
    expected = agent._system_prompt(
        False, "all",
        SEMANTIC_MEMORY if ctx else "", [],
        (GLOBAL_PACK + "\n" + PACK_BODY) if ctx else "",
        RAG if ctx else "", RECALL if ctx else "",
        tools_available=apps and (ctx or split), rich_context=ctx,
        standing_rules=context_packs.core_sections(GLOBAL_PACK) if (apps and not ctx and split) else "",
        standing_memories=ALWAYS_MEMORY if (apps and not ctx and split) else "",
        tools_off_by="apps" if not apps else "context",
    )
    assert sent[0].system == expected
    assert bool(sent[0].tool_names) == (apps and (ctx or split))


# ---- Tool history on a tool-free call (B25) -------------------------------
_TOOL_HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "who is overdue?"}]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "tu_1", "name": "maton_request",
         "input": {"app": "quickbooks", "method": "GET", "path": "/invoices"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"overdue": 2}', "is_error": False},
    ]},
    {"role": "assistant", "content": [{"type": "text", "text": "Two invoices are overdue."}]},
]


def _tool_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        b for m in messages if isinstance(m.get("content"), list)
        for b in m["content"] if agent._is_tool_block(b)
    ]


@pytest.mark.parametrize(
    "apps,ctx", [(False, True), (False, False), (True, False)],
    ids=["advisor", "plain", "ctxOff-legacy"],
)
def test_tool_free_call_replays_tool_history_as_text(
    apps: bool, ctx: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, store = _install(monkeypatch, apps=apps, context=ctx)
    store.set("hist", [dict(m) for m in _TOOL_HISTORY])
    _turn(session="hist", text="thanks")

    assert sent[0].tool_names == []
    assert _tool_blocks(sent[0].messages) == [], "tool blocks sent with no tools declared"
    flat = " ".join(
        b.get("text", "") for m in sent[0].messages for b in m["content"] if isinstance(b, dict)
    )
    assert "[Earlier tool call: maton_request" in flat
    assert '[Earlier tool result: {"overdue": 2}]' in flat
    # The stored conversation keeps the real blocks for the next tool turn.
    assert len(_tool_blocks(store.get("hist"))) == 2


def test_tool_bearing_call_replays_tool_history_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sent, store = _install(monkeypatch)
    store.set("hist", [dict(m) for m in _TOOL_HISTORY])
    _turn(session="hist", text="and the third?")
    assert sent[0].tool_names
    assert sent[0].messages[:4] == _TOOL_HISTORY


def test_flatten_returns_the_same_list_when_nothing_to_rewrite() -> None:
    """A text-only history goes out as the very same object — the default
    tool-free payload (golden: *_context_off_hi) can't change shape."""
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert agent._flatten_tool_history(history) is history


def test_flatten_clips_long_results_and_marks_errors() -> None:
    flat = agent._tool_block_as_text({
        "type": "tool_result", "tool_use_id": "x", "is_error": True,
        "content": [{"type": "text", "text": "y" * 5000}, {"type": "image", "source": {}}],
    })["text"]
    assert flat.startswith("[Earlier tool result (error): ")
    assert "[truncated]" in flat and len(flat) < 2100


# ---- The prompt builder on its own ----------------------------------------
def test_context_off_drops_blocks_even_if_a_caller_passes_them() -> None:
    p = agent._system_prompt(
        False, "nh", memory_block=SEMANTIC_MEMORY, context_pack=PACK_BODY,
        retrieved_context=RAG, conversation_block=RECALL,
        tools_available=True, rich_context=False,
    )
    assert not [m for m in CONTEXT_MARKERS if m in p]
    assert "Business context is switched off" in p


def test_legacy_plain_prompt_is_todays_text() -> None:
    """Context off with the split flag off must keep sending the exact plain
    prompt it always has (the golden snapshots hold the full bytes)."""
    p = agent._system_prompt(False, None, tools_available=False, rich_context=False, tools_off_by="context")
    assert LEGACY_CONTEXT_OFF in p and APPS_OFF not in p


# ---- The standing-rules core itself ---------------------------------------
def test_core_sections_cuts_only_hard_rules_and_security() -> None:
    core = context_packs.core_sections(GLOBAL_PACK + "\n---\n\n## Hard rules (business)\n1. SECOND.\n")
    assert core.startswith("## Hard rules (non-negotiable)")
    assert "SAMPLE-HARD-RULE" in core and "SAMPLE-SECURITY-RULE" in core and "SECOND" in core
    assert "PACKBODY" not in core and "<!--" not in core


def test_core_sections_keeps_subheadings_inside_a_section() -> None:
    pack = "## Security\nTop.\n### Keys\nNever print keys.\n## Next\nOther."
    assert context_packs.core_sections(pack) == "## Security\nTop.\n### Keys\nNever print keys."


def test_core_sections_empty_without_the_headings() -> None:
    assert context_packs.core_sections("## Who\nSomeone.\n") == ""
    assert context_packs.core_sections("") == ""


def test_standing_rules_falls_back_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_packs, "_db_off", lambda: False)

    def boom(slug: str) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(context_packs, "_cached_pack", boom)
    assert context_packs.standing_rules() == context_packs.FALLBACK_STANDING_RULES
    monkeypatch.setattr(context_packs, "_cached_pack", lambda slug: "## Who\nNo rules here.")
    assert context_packs.standing_rules() == context_packs.FALLBACK_STANDING_RULES
    monkeypatch.setattr(context_packs, "_cached_pack", lambda slug: GLOBAL_PACK)
    assert "SAMPLE-HARD-RULE" in context_packs.standing_rules()


def test_standing_rules_skips_the_db_when_it_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(slug: str) -> str:  # pragma: no cover — failure path
        raise AssertionError("read the pack with the database switched off")

    monkeypatch.setattr(context_packs, "_cached_pack", must_not_run)
    monkeypatch.setenv("HERMES_DISABLE_DB", "1")
    assert context_packs.standing_rules() == context_packs.FALLBACK_STANDING_RULES


def test_fallback_rules_are_generic() -> None:
    """The fallback lives in tracked source, so it must not carry the
    gitignored pack's business specifics — only rules true for any owner."""
    text = context_packs.FALLBACK_STANDING_RULES
    assert text.startswith("## Hard rules") and "\n## Security\n" in text
    for word in ("QuickBooks", "Kenya", "Minneapolis", "GAK", "@", "$"):
        assert word not in text, word
