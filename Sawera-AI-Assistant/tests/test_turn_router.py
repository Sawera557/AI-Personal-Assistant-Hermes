"""The deterministic per-turn tier router — TURN_COST_PLAN step 1.1, §2 rules.

What must hold (the plan's "Offline" acceptance bar, §6):

  * The SAFETY subset is 100% FULL: approval words, approval emoji (alone and
    with text), "thanks 👍" after a prose-only delete confirm, any reply after
    an open FULL reply (a question, a confirm/offer, a tool, a failure),
    ordinals and anaphora, every pending kind `pending_from` reports, nudges
    after a failed or truncated reply, empty-after-normalising, and every
    rule-1 bypass.
  * The CHAT set routes CHAT on a new chat and after a CHAT reply; the
    acknowledgements ("great", "cool") only there.
  * Short business questions are FULL however small-talky they look.
  * The prototype's 196 phrases (four suites, in
    %TEMP%/hermes_router_proto/) are ported here RE-LABELLED to the §2 rules,
    measured with LIGHT off (Phase 1): ≥90% exact and 0 FULL-needing phrases
    routed lower. The re-labels are the ones the plan's appendix names — a
    greeting or thanks after a tool turn or after an unrouted question is FULL
    under rule 5, "hello again" is FULL under rule 4 — plus every prototype
    LIGHT label, which is FULL while LIGHT is off.
  * LIGHT (rule 7) exists but is off unless HERMES_ROUTER_LIGHT=strict and the
    model is allow-listed; its allow-list is exactly the 9 published shapes.
  * Fail-open: any exception inside the rules gives FULL.
  * The decision log line carries category codes only — no message token, no
    session key, no matched substring.

Pure and offline: the router does no I/O, so nothing here is stubbed except the
environment (pinned per test) and, for the fail-open tests, the router itself.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import turn_router as tr
from hermes.businesses import BUSINESS_DISPLAY_NAMES, BUSINESS_TO_ENTITY_SLUG
from hermes.config import Config
from hermes.turn_router import (
    CHAT,
    FULL,
    LIGHT,
    REASON_CODES,
    Decision,
    RouterInput,
    decide,
    decide_and_log,
    decision_log_record,
    log_decision,
)
from hermes.turn_state import (
    PENDING_DRAFT,
    PENDING_GUARDRAIL,
    PENDING_OAUTH,
    TurnState,
    pending_from,
)

# Every env knob the router (or its vocabulary) reads. Cleared before each test
# so the developer's .env — loaded by hermes.config at import — cannot steer a
# decision; a test that needs one sets it itself.
_ROUTER_ENV = (
    "HERMES_ROUTER_MODE", "HERMES_ROUTER_LIGHT", "HERMES_ROUTER_LIGHT_MODELS",
    "HERMES_ROUTER_EXTRA_TERMS", "HERMES_ROUTER_CHAT_MAX_TOKENS",
    "HERMES_ROUTER_LITE_HISTORY", "HERMES_ROUTER_LITE_HISTORY_CHARS",
    "HERMES_ROUTER_SHADOW_SAMPLE",
)
# The code default of Config.apps() — pinned so the app vocabulary does not
# depend on the local HERMES_APPS.
_DEFAULT_APPS = (
    "quickbooks,pdf-co,google-contacts,fathom,google-meet,google-forms,"
    "google-sheets,google-docs,google-drive,google-calendar,google-mail,wati,notion,"
    "zoom,zoom-admin,jira"
)
LIGHT_MODEL = "test-light-model"


@pytest.fixture(autouse=True)
def _pinned_env(monkeypatch):
    for name in _ROUTER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_APPS", _DEFAULT_APPS)
    # The LIGHT model allow-list is env-only; listing the test model here does
    # not switch LIGHT on (HERMES_ROUTER_LIGHT stays unset → off), it only
    # lets light_strict() below exercise rule 7.
    monkeypatch.setenv("HERMES_ROUTER_LIGHT_MODELS", LIGHT_MODEL)
    tr._dynamic_vocab.cache_clear()
    yield
    tr._dynamic_vocab.cache_clear()


def route(text: Any, state: TurnState | None = None, **kw: Any) -> Decision:
    """One decision as the chat endpoints will ask for it in step 1.3:
    route="auto", interactive, no autonomy, no app restriction, router on."""
    kw.setdefault("route", "auto")
    kw.setdefault("mode", "on")
    return decide(RouterInput(text=text, state=FRESH if state is None else state, **kw))


def light_strict(text: str, state: TurnState | None = None, **kw: Any) -> Decision:
    """A decision with Phase 3 LIGHT switched on for an allow-listed model."""
    kw.setdefault("light_mode", "strict")
    kw.setdefault("model", LIGHT_MODEL)
    return route(text, state, **kw)


# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
def _route_record(tier: str) -> dict[str, Any]:
    """The step 1.2 route record as it will be persisted on an assistant row."""
    return {"mode": "on", "tier": tier, "final": tier, "escalated": False, "reason_codes": []}


FRESH = TurnState()

# A CHAT reply. LITE_CHAT replies often end with a question — rule 5's "?"
# check must not apply after one (§2 rule 6), or CHAT history is unusable.
AFTER_CHAT = TurnState(
    last_assistant_text="Hi! How can I help you today?",
    last_stop_reason="end_turn",
    last_route=_route_record("chat"),
    trailing_chat_exchanges=(("hi", "Hi! How can I help you today?"),),
    user_turns=1,
)
AFTER_CHAT_PLAIN = TurnState(
    last_assistant_text="You're welcome — have a good evening.",
    last_stop_reason="end_turn",
    last_route=_route_record("chat"),
    trailing_chat_exchanges=(("thanks", "You're welcome — have a good evening."),),
    user_turns=1,
)
AFTER_LIGHT = TurnState(
    last_assistant_text="Inflation is the rate at which prices rise over time.",
    last_stop_reason="end_turn",
    last_route=_route_record("light"),
    user_turns=1,
)
# A CLOSED FULL reply: no tool, no question or offer, end_turn, real text.
# Greetings and closers may go CHAT after it; acknowledgements may not.
CLOSED_FULL = TurnState(
    last_assistant_text="Paris is the capital of France.",
    last_stop_reason="end_turn",
    last_route=_route_record("full"),
    user_turns=1,
)
# The same reply written before routing existed: no route at all.
CLOSED_UNROUTED = TurnState(
    last_assistant_text="Paris is the capital of France.",
    last_stop_reason="end_turn",
    user_turns=1,
)
# An escalated CHAT turn: the router picked chat, the reply came from FULL.
ESCALATED_CHAT = TurnState(
    last_assistant_text="I've checked — 2 tenants are behind on rent.",
    last_stop_reason="end_turn",
    last_route={"mode": "on", "tier": "chat", "final": "full", "escalated": True,
                "reason_codes": ["greeting"]},
    user_turns=1,
)

# Prose-only delete confirmations: no pending metadata at all, the approval is
# in the text. One after a read tool, one with no tool trace whatsoever.
PROSE_DELETE_AFTER_TOOL = TurnState(
    last_assistant_text="I found 3 stale tasks: A, B, C. Confirm and I'll delete A, B, C.",
    last_tool_calls=({"tool": "list_scheduled_tasks", "ok": True},),
    last_stop_reason="end_turn",
)
PROSE_DELETE_NO_TOOL = TurnState(
    last_assistant_text="Confirm and I'll delete A, B, C.",
    last_stop_reason="end_turn",
)

# Pending workflows — one per kind pending_from reports, built from the same
# trace shapes stream_turn persists.
GUARD = TurnState(
    last_assistant_text="I'll send an email to amal@x.com — subject Renewal. Confirm?",
    last_tool_calls=({"tool": "send_email", "awaiting_confirmation": True},),
    last_stop_reason="end_turn",
)
DRAFT = TurnState(
    last_assistant_text="Draft ready (renewal.pdf attached).",
    last_tool_calls=({"tool": "email_with_attachment", "input": {"to": "a@b.c"}, "ok": True},),
    last_stop_reason="end_turn",
)
OAUTH = TurnState(
    last_assistant_text="Done.",
    last_tool_calls=({"tool": "maton_create_connection", "ok": True},),
    last_stop_reason="end_turn",
)
# A pending trace on a row that claims to be CHAT-routed can't happen (CHAT has
# no tools) — but if it ever did, the pending must still win.
GUARD_ON_CHAT_ROW = TurnState(
    last_assistant_text="Hello!",
    last_tool_calls=({"tool": "send_email", "awaiting_confirmation": True},),
    last_stop_reason="end_turn",
    last_route=_route_record("chat"),
)
PENDING_STATES: dict[str, TurnState] = {
    PENDING_GUARDRAIL: GUARD,
    PENDING_DRAFT: DRAFT,
    PENDING_OAUTH: OAUTH,
}

# A tool-using FULL reply that asked nothing — still open (rule 5).
TOOLED = TurnState(
    last_assistant_text="You have 3 meetings today: standup, rent review, payroll.",
    last_tool_calls=({"tool": "calendar_list_upcoming", "ok": True},),
    last_stop_reason="end_turn",
    last_route=_route_record("full"),
)
TOOLED_UNROUTED = TurnState(
    last_assistant_text="You have 3 meetings today: standup, rent review, payroll.",
    last_tool_calls=({"tool": "calendar_list_upcoming", "ok": True},),
    last_stop_reason="end_turn",
)

# Failed or truncated previous replies — a nudge after one is never small talk.
FAILED_STATES: dict[str, TurnState] = {
    "max_tokens": TurnState(last_assistant_text="Here is the summary of the week: rent",
                            last_stop_reason="max_tokens"),
    "no_response": TurnState(last_assistant_text="(no response)", last_stop_reason="end_turn"),
    "empty_text": TurnState(last_assistant_text="", last_stop_reason="end_turn"),
    "credits": TurnState(last_assistant_text="Out of credits on every model.",
                         last_stop_reason="credits_exhausted"),
    "refusal": TurnState(last_assistant_text="I can't help with that.", last_stop_reason="refusal"),
    "pause_turn": TurnState(last_assistant_text="Working on it", last_stop_reason="pause_turn"),
    "no_stop_reason": TurnState(last_assistant_text="Here are the figures."),
    "chat_max_tokens": TurnState(last_assistant_text="Hi! How can", last_stop_reason="max_tokens",
                                 last_route=_route_record("chat")),
    "chat_no_response": TurnState(last_assistant_text="(no response)", last_stop_reason="end_turn",
                                  last_route=_route_record("chat")),
}

# Open FULL replies that asked or offered something (rule 5). Each is tried
# both unrouted (every row written before this feature) and FULL-routed.
OPEN_REPLY_TEXTS: tuple[str, ...] = (
    "Here's the draft. Shall I send it to Amal?",
    "Which business should this task run for?",
    "I can also draft the reply for you. Want me to?",
    "Want the PDF too? I can attach it.",
    "Done — 3 tasks listed. Would you like me to pause any of them",
    "Let me know if you'd like any changes.",
    "Reply yes to proceed.",
    "I'll go ahead once you approve.",
    "Are you sure",
    "OK to send",
    "Do you want the full list",
    "Should I\nbook the 3pm slot",  # wrapped across a line break
    "Is that ok",
    "Shall I send it? 🙂",
    "Ready when you are — confirm and I'll file it.",
    "Send now or hold？",  # full-width question mark
)

# Words and phrases the task and §2 list as approvals; FULL on every state.
APPROVALS: tuple[str, ...] = (
    "yes", "ok", "go ahead", "do it", "send it", "approve", "confirm", "sure",
    "fine", "done", "no", "cancel",
    # §2's full list
    "yeah", "yep", "okay", "proceed", "send", "nope", "stop", "wait", "not now",
    "sounds good", "looks good",
    # spellings and padding
    "Yes!", "OK.", "Go ahead", "yesss", "okayyy", "Sure thing", "yes please",
    "ok thanks", "thanks, go ahead", "great, do it", "hi, yes", "cool, send it",
    "nah", "k", "kk", "alright", "yes 3pm works",
)
APPROVAL_EMOJI: tuple[str, ...] = (
    "\U0001F44D", "\U0001F44C", "✅", "✔", "☑", "\U0001F197",
    "\U0001F4AF", "\U0001F64F", "\U0001F91D",
)
EMOJI_VARIANTS: tuple[str, ...] = (
    "\U0001F44D\U0001F3FD",  # thumbs up, medium skin tone
    "✔️",          # heavy check mark + emoji presentation selector
    "\U0001F44D\U0001F44D",
)
ORDINALS_ANAPHORA: tuple[str, ...] = (
    "the second one", "that one", "same again", "the first", "the last one",
    "#2", "2", "option 3", "number 2", "the 3rd", "first one please", "this",
    "them", "also", "it", "same", "again", "that", "hello again", "thanks again",
    "thanks for that", "thank you for this", "hi again", "the other ones",
    "second", "both",
)
EMPTY_AFTER_NORMALISING: tuple[str, ...] = (
    "", " ", "\n\t", "?", "...", "!!!", "?!", "\U0001F642", "\U0001F602\U0001F602",
    "—", "❤️", "*", "( )",
)

# The task's CHAT set: CHAT on a new chat and after a CHAT reply.
CHAT_SET: tuple[str, ...] = (
    "hi", "Hi!", "hiii", "hello hermes", "hey there", "good morning", "gm",
    "salaam", "habari", "how are you", "thanks", "thank you so much", "thx", "ty",
    "cheers", "bye", "good night",
)
ACKS: tuple[str, ...] = ("great", "cool", "perfect", "nice", "awesome")
# Short business questions: FULL however small-talky they look.
BUSINESS_SHORT: tuple[str, ...] = (
    "AFK?", "rent?", "GIF balance", "rent status for GAK?",
    "hi, what's GAK's rent status", "hello can you email Amal", "thanks, now send it",
    "nh", "gak", "per?", "PER", "balance?", "status", "hi gak", "thanks nour hammam",
    "morning, anything urgent?", "hey can you text the plumber", "hello, is the generator fixed?",
    "good morning, any new emails?", "thanks for checking the calendar",
)

# Every rule-1 bypass, as RouterInput overrides on a "hi" that would be CHAT.
BYPASSES: dict[str, dict[str, Any]] = {
    "interactive_false": {"interactive": False},
    "autonomy_readonly": {"autonomy": "readonly"},
    "autonomy_full": {"autonomy": "full"},
    "autonomy_empty_string": {"autonomy": ""},
    "allowed_apps": {"allowed_apps": ["google-mail"]},
    "allowed_apps_empty": {"allowed_apps": []},
    "scheduled_session": {"session_key": "__scheduled__:task-123"},
    "attachments": {"has_attachments": True},
    "state_read_failed": {"state": TurnState(ok=False)},
    "state_missing": {"state": None, "_raw_state": True},
    "route_full": {"route": "full"},
    "route_none": {"route": None},
    "route_typo": {"route": "AUTO "},
    "mode_off": {"mode": "off"},
    "mode_unknown": {"mode": "true"},
    "mode_blank": {"mode": ""},
    "scheduler_shape": {"interactive": False, "autonomy": "full", "route": "full",
                        "allowed_apps": ["google-mail"], "session_key": "__scheduled__:t1"},
}


def _bypass_input(text: str, overrides: dict[str, Any]) -> RouterInput:
    kw = dict(overrides)
    raw_state = kw.pop("_raw_state", False)
    state = kw.pop("state", FRESH) if not raw_state else kw.pop("state")
    kw.setdefault("route", "auto")
    kw.setdefault("mode", "on")
    return RouterInput(text=text, state=state, **kw)


# ---------------------------------------------------------------------------
# Safety subset — must be 100% FULL
# ---------------------------------------------------------------------------
_ANY_STATE = {
    "fresh": FRESH, "after_chat": AFTER_CHAT, "after_chat_plain": AFTER_CHAT_PLAIN,
    "closed_full": CLOSED_FULL, "closed_unrouted": CLOSED_UNROUTED, "after_light": AFTER_LIGHT,
}


@pytest.mark.parametrize("state_name", sorted(_ANY_STATE))
@pytest.mark.parametrize("text", APPROVALS)
def test_approval_words_are_full_on_every_state(text, state_name):
    d = route(text, _ANY_STATE[state_name])
    assert d.tier == FULL, (text, state_name, d)
    assert "affirmation" in d.reason_codes
    assert d.affirmation is True


@pytest.mark.parametrize("state_name", sorted(_ANY_STATE))
@pytest.mark.parametrize("emoji", APPROVAL_EMOJI + EMOJI_VARIANTS)
def test_approval_emoji_alone_and_with_text_are_full(emoji, state_name):
    state = _ANY_STATE[state_name]
    for text in (emoji, f"thanks {emoji}", f"{emoji} thanks", f"great{emoji}",
                 f"hi {emoji}", f"Good morning Hermes {emoji}", f"cheers!{emoji}"):
        d = route(text, state)
        assert d.tier == FULL, (text, state_name, d)
        assert "approval_emoji" in d.reason_codes
        assert d.affirmation is True


def test_non_approval_emoji_does_not_block_a_greeting():
    # Only the approval set is a rule-2 signal; other emoji are stripped.
    assert route("Good morning Hermes \U0001F642").tier == CHAT
    assert route("hi \U0001F44B").tier == CHAT  # waving hand


@pytest.mark.parametrize("state", [PROSE_DELETE_AFTER_TOOL, PROSE_DELETE_NO_TOOL],
                         ids=["after_tool", "no_tool"])
def test_thanks_thumbs_up_after_prose_only_delete_confirm_is_full(state):
    # No pending metadata exists for a prose-only confirm: the approval is in
    # the text, so rule 2 (the emoji) AND rule 5 (the confirm wording) fire.
    assert pending_from(state.last_tool_calls) is None
    d = route("thanks \U0001F44D", state)
    assert d.tier == FULL
    assert {"approval_emoji", "open:offer"} <= set(d.reason_codes)
    # And without the emoji, the confirm wording alone keeps it FULL.
    for text in ("thanks", "great", "cool", "perfect", "hi", "cheers", "ty"):
        assert route(text, state).tier == FULL, text


@pytest.mark.parametrize("routed", [False, True], ids=["unrouted", "full_routed"])
@pytest.mark.parametrize("reply", OPEN_REPLY_TEXTS)
def test_any_reply_after_a_question_or_offer_is_full(reply, routed):
    state = TurnState(
        last_assistant_text=reply, last_stop_reason="end_turn",
        last_route=_route_record("full") if routed else None,
    )
    for text in (*CHAT_SET, *ACKS, "lol", "hello", "hey", "hello again"):
        d = route(text, state)
        assert d.tier == FULL, (reply, text, d)
        assert {"open:question", "open:offer", "anaphora"} & set(d.reason_codes), (reply, text, d)


@pytest.mark.parametrize("state", [TOOLED, TOOLED_UNROUTED, PROSE_DELETE_AFTER_TOOL],
                         ids=["full_routed", "unrouted", "delete_listing"])
def test_any_reply_after_a_tool_using_full_reply_is_full(state):
    for text in (*CHAT_SET, *ACKS, "lol", "thank you hermes", "goodbye", "cheers mate"):
        d = route(text, state)
        assert d.tier == FULL, (text, d)
        assert "open:tool" in d.reason_codes


def test_escalated_chat_turn_counts_as_a_full_reply():
    # final="full" wins over the tier first picked: the reply came from FULL.
    assert route("great", ESCALATED_CHAT).tier == FULL
    assert route("cool", ESCALATED_CHAT).reason_codes == ("ack_after_full",)


@pytest.mark.parametrize("state_name", sorted(_ANY_STATE))
@pytest.mark.parametrize("text", ORDINALS_ANAPHORA)
def test_ordinals_and_anaphora_are_full(text, state_name):
    d = route(text, _ANY_STATE[state_name])
    assert d.tier == FULL, (text, state_name, d)
    assert {"ordinal", "anaphora", "affirmation"} & set(d.reason_codes), (text, d)


@pytest.mark.parametrize("kind", sorted(PENDING_STATES))
def test_every_pending_kind_is_full_whatever_the_text(kind):
    state = PENDING_STATES[kind]
    # The fixture really is the kind pending_from reports — so a new detector
    # rule in turn_state.py is exercised here too.
    assert pending_from(state.last_tool_calls) == kind
    for text in (*CHAT_SET, *ACKS, "lol", "what is 2+2", "tell me a joke"):
        d = route(text, state)
        assert d.tier == FULL, (kind, text, d)
        assert f"pending:{kind}" in d.reason_codes
        assert d.pending_kind == kind
    # LIGHT can't sneak past it either.
    assert light_strict("what is 2+2", state).tier == FULL


def test_pending_on_a_chat_routed_row_still_wins():
    d = route("hi", GUARD_ON_CHAT_ROW)
    assert d.tier == FULL
    assert "pending:guardrail" in d.reason_codes


def test_pending_kind_is_reported_with_other_reasons():
    d = route("yes \U0001F44D", GUARD)
    assert d.pending_kind == PENDING_GUARDRAIL
    assert {"approval_emoji", "pending:guardrail", "affirmation"} <= set(d.reason_codes)
    # A confirmed attachment send is not a pending draft any more.
    sent = TurnState(
        last_assistant_text="Sent.",
        last_tool_calls=({"tool": "email_with_attachment", "input": {"confirm": True}},),
        last_stop_reason="end_turn",
    )
    assert pending_from(sent.last_tool_calls) is None
    assert route("thanks", sent).pending_kind is None


@pytest.mark.parametrize("failure", sorted(FAILED_STATES))
def test_nudges_after_a_failed_or_truncated_reply_are_full(failure):
    state = FAILED_STATES[failure]
    for text in ("hello?", "hello", "hi", "hey", "thanks", "?", "great"):
        d = route(text, state)
        assert d.tier == FULL, (failure, text, d)
    assert {"open:stopped", "open:failed"} & set(route("hello?", state).reason_codes)


@pytest.mark.parametrize("text", EMPTY_AFTER_NORMALISING)
def test_empty_after_normalising_is_full(text):
    for state in (FRESH, AFTER_CHAT):
        d = route(text, state)
        assert d.tier == FULL, (text, d)
        assert "empty" in d.reason_codes


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_every_bypass_is_full(name):
    overrides = BYPASSES[name]
    for text in ("hi", "thanks", "great", "what is 2+2"):
        inp = _bypass_input(text, overrides)
        d = decide(inp)
        assert d.tier == FULL, (name, text, d)
        assert d.reason_codes and all(c.startswith("bypass:") for c in d.reason_codes), d
    # Even with LIGHT strict and an allow-listed model.
    inp = _bypass_input("what is 2+2", {**overrides, "light_mode": "strict", "model": LIGHT_MODEL})
    assert decide(inp).tier == FULL


def test_bypass_reads_nothing_about_the_text():
    # A bypassed turn is FULL before the text is looked at: no affirmation or
    # pending is reported, and even a non-string text cannot raise.
    d = decide(RouterInput(text=None, route="full", mode="on", state=GUARD))  # type: ignore[arg-type]
    assert d == Decision(FULL, ("bypass:route",))


def test_mode_defaults_to_off_and_unknown_values_read_as_off(monkeypatch):
    inp = RouterInput(text="hi", route="auto", state=FRESH)  # mode=None → env
    assert decide(inp).reason_codes == ("bypass:mode_off",)
    for value in ("true", "1", "yes", "enabled", "ON!", " "):
        monkeypatch.setenv("HERMES_ROUTER_MODE", value)
        assert decide(inp).tier == FULL, value
    for value in ("on", "ON", " shadow "):
        monkeypatch.setenv("HERMES_ROUTER_MODE", value)
        assert decide(inp).tier == CHAT, value


def test_shadow_mode_returns_the_would_be_tier():
    # Deciding is identical in shadow and on; the CALLER keeps shadow on FULL.
    for text, state in (("hi", FRESH), ("thanks", AFTER_CHAT), ("yes", GUARD)):
        assert route(text, state, mode="shadow") == route(text, state, mode="on")


# ---------------------------------------------------------------------------
# CHAT set
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state_name", ["fresh", "after_chat", "after_chat_plain", "closed_full", "closed_unrouted"])
@pytest.mark.parametrize("text", CHAT_SET)
def test_chat_set_is_chat_on_new_chat_and_after_chat_or_a_closed_reply(text, state_name):
    d = route(text, _ANY_STATE[state_name])
    assert d.tier == CHAT, (text, state_name, d)
    assert set(d.reason_codes) <= {"greeting", "closer"}
    assert d.pending_kind is None and d.affirmation is False


@pytest.mark.parametrize("text", CHAT_SET)
def test_chat_set_is_chat_with_voice(text):
    assert route(text, FRESH, voice=True).tier == CHAT


def test_greeting_after_a_light_reply_is_chat():
    # A LIGHT reply is closed (no tools, no business data): a greeting after it
    # is small talk. Only the acknowledgements are held back (next test).
    assert route("thanks", AFTER_LIGHT).tier == CHAT


@pytest.mark.parametrize("text", ACKS)
def test_acks_only_on_a_new_chat_or_after_a_chat_reply(text):
    assert route(text, FRESH).tier == CHAT
    assert route(text, AFTER_CHAT).tier == CHAT
    assert route(text, AFTER_CHAT_PLAIN).tier == CHAT
    for state in (CLOSED_FULL, CLOSED_UNROUTED, AFTER_LIGHT, ESCALATED_CHAT):
        d = route(text, state)
        assert d.tier == FULL, (text, d)
        assert d.reason_codes == ("ack_after_full",)
    # Mixed with a closer it is still an ack: FULL after a FULL reply.
    assert route(f"{text}, thanks", CLOSED_FULL).tier == FULL
    assert route(f"{text}, thanks", FRESH).tier == CHAT


@pytest.mark.parametrize("text", (
    "Hello Hermes!", "hey hey", "yo hermes", "good evening!", "thanks a ton",
    "how's your day going?", "hello?", "good night hermes", "thank u", "morning!",
    "bye for now", "good afternoon", "hi there", "hello hello", "goodbye", "hey!",
    "Good morning Hermes \U0001F642", "hey, how are you?", "asante sana", "thanks so much",
    "thank you very much", "THANKS!!!", "heyyy", "helloooo", "goooood morning",
    "thanksss buddy", "ｈｉ",  # full-width "hi" (NFKC)
))
def test_more_greetings_and_closers_on_a_new_chat(text):
    assert route(text).tier == CHAT, text


@pytest.mark.parametrize("text", (
    "thanks again", "hello all", "hi too", "good morning everyone, quick one",
    "hi hermes how is the farm", "thanks hermes for the report", "hi " * 9,
    "hermes", "there", "so much", "hi hermes hi hermes hi hermes hi hermes hi",
))
def test_not_whole_message_small_talk_is_full(text):
    # "again"/"all"/"too" were fillers in the prototype; §2 does not allow
    # them. More than 8 words, or fillers alone, are not a greeting.
    assert route(text).tier == FULL, text


@pytest.mark.parametrize("state_name", ["fresh", "after_chat"])
@pytest.mark.parametrize("text", BUSINESS_SHORT)
def test_short_business_questions_are_full(text, state_name):
    d = route(text, _ANY_STATE[state_name])
    assert d.tier == FULL, (text, d)
    # And never LIGHT either, with LIGHT on.
    assert light_strict(text, _ANY_STATE[state_name]).tier == FULL, text


def test_business_display_names_force_full():
    assert BUSINESS_DISPLAY_NAMES["nh"] == "Nour Hammam"
    # Every businesses-table slug has a display name, plus Comfort PCA, which
    # has no slug (businesses.py lacks it — the reason the constant exists).
    assert set(BUSINESS_TO_ENTITY_SLUG) | {"comfort_pca"} == set(BUSINESS_DISPLAY_NAMES)
    for name in BUSINESS_DISPLAY_NAMES.values():
        for text in (f"hi {name}", f"thanks {name.lower()}", f"what is {name.lower()}"):
            d = light_strict(text)
            assert d.tier == FULL, text
            assert "business" in d.reason_codes, (text, d)
    for text in ("how is afro farm doing", "comfort pca", "gak's rent", "GAKC",
                 "afk's balance", "nh?", "for per", "rent for PER"):
        assert "business" in route(text).reason_codes, text


def test_per_is_a_business_only_as_uppercase_or_in_a_slug_position():
    assert "business" in route("what is PER").reason_codes
    assert "business" in route("rent for per").reason_codes
    assert "business" not in route("how much per night").reason_codes
    assert "business" not in route("what is per capita income").reason_codes


def test_apps_from_config_force_full_with_no_code_change(monkeypatch):
    # "define slack" is LIGHT-shaped and names no known app…
    assert light_strict("define slack").tier == LIGHT
    # …until slack is enabled: Config.apps() is the vocabulary.
    monkeypatch.setenv("HERMES_APPS", _DEFAULT_APPS + ",slack")
    d = light_strict("define slack")
    assert d.tier == FULL and "app" in d.reason_codes
    # The labels count too, so a disabled app's name still forces FULL.
    monkeypatch.setenv("HERMES_APPS", "jira")
    for text in ("define gmail", "what is a notion", "define quickbooks"):
        assert "app" in light_strict(text).reason_codes, text


def test_extra_terms_force_full_everywhere(monkeypatch):
    assert route("hi wanjiku").tier == FULL  # an unknown name is never CHAT
    assert light_strict("define wanjiku").tier == LIGHT
    monkeypatch.setenv("HERMES_ROUTER_EXTRA_TERMS", "Wanjiku, Mama Njeri,  ,")
    for text in ("define wanjiku", "what is a mama njeri", "Define Wanjiku's"):
        d = light_strict(text)
        assert d.tier == FULL and "extra_term" in d.reason_codes, text
    # The env is read per decision: removing the term reopens LIGHT.
    monkeypatch.delenv("HERMES_ROUTER_EXTRA_TERMS")
    assert light_strict("define wanjiku").tier == LIGHT


def test_router_vocabulary_is_its_own_not_low_effort_hints():
    # §2: hermes/agent.py's _LOW_EFFORT_HINTS holds yes/no/ok/list/show me/
    # status — the router must not import it (or agent.py at all).
    source = Path(tr.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+[\w.]*agent\b", source, re.M)
    assert not re.search(r"^\s*from\s+\.\s+import\s+.*\bagent\b", source, re.M)
    assert not hasattr(tr, "_LOW_EFFORT_HINTS")


# ---------------------------------------------------------------------------
# Rule 5 details
# ---------------------------------------------------------------------------
def test_question_after_a_chat_reply_does_not_open_it():
    # §2 rule 6: the trailing-"?" part of rule 5 is skipped after CHAT.
    assert AFTER_CHAT.last_assistant_text.endswith("?")
    assert route("thanks", AFTER_CHAT).tier == CHAT
    # …but the same text unrouted is an open FULL reply.
    unrouted = TurnState(last_assistant_text=AFTER_CHAT.last_assistant_text,
                         last_stop_reason="end_turn")
    assert "open:question" in route("thanks", unrouted).reason_codes


def test_offer_wording_matches_the_published_pattern_unanchored():
    # The §2 pattern is published unanchored: inflected forms count.
    for reply in ("Awaiting your confirmation.", "Proceeding unless you object.",
                  "This needs your approval.", "Please reconfirm the address."):
        state = TurnState(last_assistant_text=reply, last_stop_reason="end_turn")
        assert "open:offer" in route("thanks", state).reason_codes, reply


def test_offer_outside_the_last_400_chars_does_not_count():
    reply = "Shall I send it? " + ("Here are the figures in full. " * 30) + "That is all."
    state = TurnState(last_assistant_text=reply, last_stop_reason="end_turn")
    assert route("thanks", state).tier == CHAT


# ---------------------------------------------------------------------------
# The prototype phrase suites, re-labelled to the §2 rules
# ---------------------------------------------------------------------------
# Prototype states, with the end_turn stop the real stores persist (the
# prototype's TurnState had no stop_reason; a missing one now reads as open).
def _st(text: str, tools: tuple[dict[str, Any], ...] = ()) -> TurnState:
    return TurnState(last_assistant_text=text, last_tool_calls=tools, last_stop_reason="end_turn")


_MT = {"tool": "maton_request", "ok": True}
P_ASKED = _st("Here's the draft. Shall I send it to Amal?")
P_ASKED_Q = _st("Which business should this task run for?")
P_GUARD = GUARD
P_DRAFT = _st("Draft ready (renewal.pdf attached). Approve to send.",
              ({"tool": "email_with_attachment", "input": {"to": "a@b.c"}, "ok": True},))
P_OAUTH = _st("Open this link to connect Jira: https://...",
              ({"tool": "maton_create_connection", "ok": True},))
P_TOOLED = _st("You have 3 meetings today: ...", ({"tool": "calendar_list_upcoming", "ok": True},))
P_LISTED = _st("Found 3 tasks:\n1. Morning farm briefing\n2. Rent chase\n3. NH revenue",
               ({"tool": "list_scheduled_tasks", "ok": True},))
P_PLAIN = _st("Paris is the capital of France.")
P_BROKEN = TurnState(ok=False)
H1_ASKED = _st("I can also draft the reply for you. Want me to?")
H1_TOOLED = _st("Rent roll for GAK: 12 units paid, 2 in arrears.", (_MT,))
H2_ASKED = _st("Should I book the 3pm slot with the plumber?")
H2_TOOLED = _st("Here are this week's NH bookings: ...", (_MT,))
H2_CHATTY = _st("Hi! How can I help today?")  # unrouted: written before routing
H3_ASKED = _st("I've prepared the invoice for Mama Njeri. Send now or hold?")
H3_TOOLED = _st("3 unread emails from KRA ...", (_MT,))

# (text, state, kwargs, expected tier under §2 with LIGHT off, relabel note)
# A non-empty note marks a label changed from the prototype's; "light" means
# the prototype said LIGHT, which is FULL while LIGHT is off (Phase 1).
Case = tuple[str, TurnState, dict[str, Any], str, str]
MAIN: list[Case] = [
    ("hi", FRESH, {}, CHAT, ""),
    ("Hi!", FRESH, {}, CHAT, ""),
    ("thanks", P_TOOLED, {}, FULL, "rule5:tool"),
    ("thanks!", FRESH, {}, CHAT, ""),
    ("what is 2+2", FRESH, {}, FULL, "light"),
    ("rent status for GAK?", FRESH, {}, FULL, ""),
    ("send Amal the renewal letter", FRESH, {}, FULL, ""),
    ("yes", P_GUARD, {}, FULL, ""),
    ("hello hermes", FRESH, {}, CHAT, ""),
    ("good morning", FRESH, {}, CHAT, ""),
    ("Good morning Hermes \U0001F642", FRESH, {}, CHAT, ""),
    ("hey, how are you?", FRESH, {}, CHAT, ""),
    ("thank you so much", P_TOOLED, {}, FULL, "rule5:tool"),
    ("bye", FRESH, {}, CHAT, ""),
    ("asante sana", FRESH, {}, CHAT, ""),
    ("lol", FRESH, {}, CHAT, ""),
    ("hi", FRESH, {"voice": True}, CHAT, ""),
    ("what's the capital of France?", FRESH, {}, FULL, "light"),
    ("explain how photosynthesis works", FRESH, {}, FULL, "light"),
    ("tell me a joke", FRESH, {}, FULL, "light"),
    ("translate 'good evening' to French", FRESH, {}, FULL, "light"),
    ("how many continents are there?", FRESH, {}, FULL, "light"),
    ("what is 15 * 12", FRESH, {}, FULL, "light"),
    ("define amortization", P_PLAIN, {}, FULL, "light"),
    ("ok", P_GUARD, {}, FULL, ""),
    ("ok", FRESH, {}, FULL, ""),
    ("yes please", P_ASKED, {}, FULL, ""),
    ("go ahead", P_ASKED, {}, FULL, ""),
    ("do it", P_ASKED, {}, FULL, ""),
    ("no", P_GUARD, {}, FULL, ""),
    ("send it", P_DRAFT, {}, FULL, ""),
    ("looks good", P_DRAFT, {}, FULL, ""),
    ("perfect", P_ASKED, {}, FULL, ""),
    ("great", P_GUARD, {}, FULL, ""),
    ("thanks", P_GUARD, {}, FULL, ""),
    ("hi", P_GUARD, {}, FULL, ""),
    ("done", P_OAUTH, {}, FULL, ""),
    ("ok I've connected it", P_OAUTH, {}, FULL, ""),
    ("nh", P_ASKED_Q, {}, FULL, ""),
    ("7am weekdays", P_ASKED_Q, {}, FULL, ""),
    ("readonly", P_ASKED_Q, {}, FULL, ""),
    ("the second one", P_LISTED, {}, FULL, ""),
    ("2", P_LISTED, {}, FULL, ""),
    ("pause it", P_LISTED, {}, FULL, ""),
    ("what about gif?", P_TOOLED, {}, FULL, ""),
    ("and tomorrow?", P_TOOLED, {}, FULL, ""),
    ("stop asking me to confirm", FRESH, {}, FULL, ""),
    ("AFK?", FRESH, {}, FULL, ""),
    ("any new emails?", FRESH, {}, FULL, ""),
    ("what's on my calendar today", FRESH, {}, FULL, ""),
    ("who is Amal?", FRESH, {}, FULL, ""),
    ("balance?", FRESH, {}, FULL, ""),
    ("how much did Nour Hammam make in July?", FRESH, {}, FULL, ""),
    ("KAN-12 status", FRESH, {}, FULL, ""),
    ("is the rent paid", FRESH, {}, FULL, ""),
    ("remind me at 5pm", FRESH, {}, FULL, ""),
    ("hi, check my email", FRESH, {}, FULL, ""),
    ("remember that I prefer short answers", FRESH, {}, FULL, ""),
    ("I'm vegetarian, what is a good protein source?", FRESH, {}, FULL, ""),
    ("what's the weather in Nairobi", FRESH, {}, FULL, ""),
    ("what's the latest news", FRESH, {}, FULL, ""),
    ("what is KES 900k in USD", FRESH, {}, FULL, ""),
    ("explain that again", P_PLAIN, {}, FULL, ""),
    ("what is our strategy for Q4", FRESH, {}, FULL, ""),
    ("The user wants to know their upcoming meetings", FRESH, {"voice": True}, FULL, ""),
    ("what's on my calendar", FRESH, {"voice": True}, FULL, ""),
    ("hi", FRESH, {"route": "full"}, FULL, ""),
    ("Morning farm briefing", FRESH,
     {"interactive": False, "autonomy": "readonly", "route": "full"}, FULL, ""),
    ("hi", FRESH, {"interactive": False}, FULL, ""),
    ("hi", FRESH, {"has_attachments": True}, FULL, ""),
    ("hi", P_BROKEN, {}, FULL, ""),
]
HELDOUT_1: list[Case] = [
    ("hey hey", FRESH, {}, CHAT, ""),
    ("yo hermes", FRESH, {}, CHAT, ""),
    ("good evening!", FRESH, {}, CHAT, ""),
    ("thanks a ton", FRESH, {}, CHAT, ""),
    ("cheers mate", H1_TOOLED, {}, FULL, "rule5:tool"),
    ("nice, thanks", H1_TOOLED, {}, FULL, "rule5:tool"),
    ("how's your day going?", FRESH, {}, CHAT, ""),
    ("hello?", FRESH, {}, CHAT, ""),
    ("ty!", H1_TOOLED, {}, FULL, "rule5:tool"),
    ("good night hermes", FRESH, {}, CHAT, ""),
    ("hiii", FRESH, {}, CHAT, ""),
    ("thank u", FRESH, {}, CHAT, ""),
    ("what is the square root of 144", FRESH, {}, FULL, "light"),
    ("what does EBITDA stand for?", FRESH, {}, FULL, "light"),
    ("how do I say thank you in Swahili", FRESH, {}, FULL, "light"),
    ("why is the sky blue?", FRESH, {}, FULL, "light"),
    ("give me a synonym for happy", FRESH, {}, FULL, "light"),
    ("what's 17% of 250?", FRESH, {}, FULL, "light"),
    ("who wrote Hamlet?", FRESH, {}, FULL, "light"),
    ("convert 5 miles to km", FRESH, {}, FULL, "light"),
    ("what's a good name for a dog", FRESH, {}, FULL, "light"),
    ("explain compound interest simply", FRESH, {}, FULL, "light"),
    ("sure thing", H1_ASKED, {}, FULL, ""),
    ("yeah go for it", H1_ASKED, {}, FULL, ""),
    ("please", H1_ASKED, {}, FULL, ""),
    ("yes but cc Sarah", H1_ASKED, {}, FULL, ""),
    ("nope, leave it", H1_ASKED, {}, FULL, ""),
    ("which tenants are late?", FRESH, {}, FULL, ""),
    ("anything from the bank?", FRESH, {}, FULL, ""),
    ("what did Peter say", FRESH, {}, FULL, ""),
    ("how's the farm doing", FRESH, {}, FULL, ""),
    ("summarise yesterday", FRESH, {}, FULL, ""),
    ("what's due this week", FRESH, {}, FULL, ""),
    ("ping Amal on whatsapp", FRESH, {}, FULL, ""),
    ("any updates?", FRESH, {}, FULL, ""),
    ("and the other units?", H1_TOOLED, {}, FULL, ""),
    ("why so many in arrears?", H1_TOOLED, {}, FULL, ""),
    ("hi! can you pull the July P&L", FRESH, {}, FULL, ""),
    ("thanks, now email it to me", H1_TOOLED, {}, FULL, ""),
    ("what's my schedule", FRESH, {}, FULL, ""),
    ("what do I owe KRA", FRESH, {}, FULL, ""),
    ("hello, is the generator fixed?", FRESH, {}, FULL, ""),
    ("how many rooms are booked tonight", FRESH, {}, FULL, ""),
    ("did the payment go through", FRESH, {}, FULL, ""),
    ("Grace called, call her back", FRESH, {}, FULL, ""),
    ("what is the plan for Friday", FRESH, {}, FULL, ""),
    ("can you help", FRESH, {}, FULL, ""),
]
HELDOUT_2: list[Case] = [
    ("hey", H2_CHATTY, {}, FULL, "rule5:question"),
    ("morning!", FRESH, {}, CHAT, ""),
    ("thx hermes", H2_TOOLED, {}, FULL, "rule5:tool"),
    ("salaam", FRESH, {}, CHAT, ""),
    ("thank you!!", H2_TOOLED, {}, FULL, "rule5:tool"),
    ("bye for now", FRESH, {}, CHAT, ""),
    ("hello again", H2_CHATTY, {}, FULL, "rule4:anaphora"),
    ("good afternoon", FRESH, {}, CHAT, ""),
    ("what's the boiling point of water", FRESH, {}, FULL, "light"),
    ("how long is a marathon?", FRESH, {}, FULL, "light"),
    ("is a tomato a fruit?", FRESH, {}, FULL, "light"),
    ("what's 12 x 8", FRESH, {}, FULL, "light"),
    ("explain what an API is", FRESH, {}, FULL, "light"),
    ("tell me something interesting", FRESH, {}, FULL, "light"),
    ("what does 'pro rata' mean", FRESH, {}, FULL, "light"),
    ("how do vaccines work", FRESH, {}, FULL, "light"),
    ("book it", H2_ASKED, {}, FULL, ""),
    ("yes 3pm works", H2_ASKED, {}, FULL, ""),
    ("hmm maybe 4pm instead", H2_ASKED, {}, FULL, ""),
    ("not yet", H2_ASKED, {}, FULL, ""),
    ("who's in room 4?", H2_TOOLED, {}, FULL, ""),
    ("cancel the friday one", H2_TOOLED, {}, FULL, ""),
    ("ok and next week?", H2_TOOLED, {}, FULL, ""),
    ("great, export that", H2_TOOLED, {}, FULL, ""),
    ("how is cash flow looking", FRESH, {}, FULL, ""),
    ("did Jane reply?", FRESH, {}, FULL, ""),
    ("what's pending", FRESH, {}, FULL, ""),
    ("whats outstanding for afro", FRESH, {}, FULL, ""),
    ("any WhatsApp messages from tenants?", FRESH, {}, FULL, ""),
    ("upload the lease", FRESH, {}, FULL, ""),
    ("hey can you text the plumber", FRESH, {}, FULL, ""),
    ("is the water bill paid?", FRESH, {}, FULL, ""),
    ("who owes me money", FRESH, {}, FULL, ""),
    ("what happened with the generator", FRESH, {}, FULL, ""),
    ("how many guests checked in", FRESH, {}, FULL, ""),
    ("what's left to do", FRESH, {}, FULL, ""),
    ("can you look into the leak at unit 3", FRESH, {}, FULL, ""),
    ("summary please", FRESH, {}, FULL, ""),
    ("morning, anything urgent?", FRESH, {}, FULL, ""),
    ("what's the occupancy rate", FRESH, {}, FULL, ""),
]
HELDOUT_3: list[Case] = [
    ("hi there", FRESH, {}, CHAT, ""),
    ("hello hello", FRESH, {}, CHAT, ""),
    ("thank you hermes", H3_TOOLED, {}, FULL, "rule5:tool"),
    ("goodbye", H3_TOOLED, {}, FULL, "rule5:tool"),
    ("cheers", FRESH, {}, CHAT, ""),
    ("hey!", FRESH, {}, CHAT, ""),
    ("what is inflation", FRESH, {}, FULL, "light"),
    ("define liquidity", FRESH, {}, FULL, "light"),
    ("what is 100/4", FRESH, {}, FULL, "light"),
    ("what's a balance sheet", FRESH, {}, FULL, "light"),
    ("explain how interest rates work", FRESH, {}, FULL, "light"),
    ("tell me a riddle", FRESH, {}, FULL, "light"),
    ("what does ROI stand for", FRESH, {}, FULL, "light"),
    ("how does a heat pump work", FRESH, {}, FULL, "light"),
    ("send now", H3_ASKED, {}, FULL, ""),
    ("hold", H3_ASKED, {}, FULL, ""),
    ("send", H3_ASKED, {}, FULL, ""),
    ("hold till monday", H3_ASKED, {}, FULL, ""),
    ("yes send", H3_ASKED, {}, FULL, ""),
    ("\U0001F44D", H3_ASKED, {}, FULL, ""),
    ("open the first one", H3_TOOLED, {}, FULL, ""),
    ("reply saying we'll pay friday", H3_TOOLED, {}, FULL, ""),
    ("what's urgent", FRESH, {}, FULL, ""),
    ("what's new", FRESH, {}, FULL, ""),
    ("whats happening", FRESH, {}, FULL, ""),
    ("what is outstanding", FRESH, {}, FULL, ""),
    ("what's the situation with the borehole", FRESH, {}, FULL, ""),
    ("explain the delay", FRESH, {}, FULL, ""),
    ("what is the total", FRESH, {}, FULL, ""),
    ("how many units are vacant", FRESH, {}, FULL, ""),
    ("what's a good time to meet Ali", FRESH, {}, FULL, ""),
    ("define the scope for the website project", FRESH, {}, FULL, ""),
    ("convert this to a pdf", FRESH, {}, FULL, ""),
    ("tell me a summary of the week", FRESH, {}, FULL, ""),
    ("how much is left", FRESH, {}, FULL, ""),
    ("what is my net position", FRESH, {}, FULL, ""),
    ("give me a quote for 20 bags", FRESH, {}, FULL, ""),
    ("what is the population of the farm", FRESH, {}, FULL, ""),
]
SUITES: dict[str, list[Case]] = {
    "main": MAIN, "heldout_1": HELDOUT_1, "heldout_2": HELDOUT_2, "heldout_3": HELDOUT_3,
}
ALL_CASES: list[tuple[str, int, Case]] = [
    (suite, i, case) for suite, cases in SUITES.items() for i, case in enumerate(cases)
]


def _suite_decision(case: Case) -> Decision:
    text, state, kw, _expected, _note = case
    return route(text, state, **kw)


def test_suites_are_the_196_prototype_phrases():
    assert {k: len(v) for k, v in SUITES.items()} == {
        "main": 71, "heldout_1": 47, "heldout_2": 40, "heldout_3": 38,
    }
    # The appendix's named re-labels are in: greetings/thanks after a tool turn
    # or after an unrouted question go FULL, "hello again" goes FULL.
    relabelled = {(c[0], c[4]) for _, _, c in ALL_CASES if c[4] and c[4] != "light"}
    assert ("hey", "rule5:question") in relabelled
    assert ("hello again", "rule4:anaphora") in relabelled
    assert ("thank you hermes", "rule5:tool") in relabelled
    assert ("goodbye", "rule5:tool") in relabelled


@pytest.mark.parametrize(
    "suite,idx,case",
    [c for c in ALL_CASES if c[2][3] == FULL],
    ids=[f"{s}-{i}" for s, i, c in ALL_CASES if c[3] == FULL],
)
def test_suite_full_labels_are_never_routed_lower(suite, idx, case):
    # The acceptance bar: 0 FULL-needing phrases routed to CHAT (or LIGHT —
    # LIGHT is off by default, and must stay unreachable here).
    d = _suite_decision(case)
    assert d.tier == FULL, (suite, case[0], d)


def test_suite_full_labels_stay_full_with_light_strict():
    # Phase 3 preview: with LIGHT switched on, none of the FULL-needing suite
    # phrases may leak into LIGHT (the prototype leaked 3 of held-out set 3
    # through its wider allow-list). Prototype-LIGHT phrases are excluded —
    # those are LIGHT's to take.
    leaks = []
    for suite, _idx, (text, state, kw, expected, note) in ALL_CASES:
        if expected != FULL or note == "light":
            continue
        d = light_strict(text, state, **kw)
        if d.tier != FULL:
            leaks.append((suite, text, d))
    assert leaks == []


def test_suite_accuracy_meets_the_bar(capsys):
    per_suite: dict[str, tuple[int, int]] = {}
    misses: list[tuple[str, str, str, str]] = []
    for suite, cases in SUITES.items():
        ok = 0
        for case in cases:
            got = _suite_decision(case).tier
            if got == case[3]:
                ok += 1
            else:
                misses.append((suite, case[0], case[3], got))
        per_suite[suite] = (ok, len(cases))
    total_ok = sum(ok for ok, _ in per_suite.values())
    total = sum(n for _, n in per_suite.values())
    with capsys.disabled():
        print("\nturn_router suites (LIGHT off): "
              + ", ".join(f"{s} {ok}/{n}" for s, (ok, n) in per_suite.items())
              + f"; overall {total_ok}/{total} = {total_ok / total:.1%}")
        for miss in misses:
            print("  MISS", miss)
    # No miss may be a downgrade (that is the safety test above); a miss here
    # can only be a CHAT phrase sent FULL, which costs money, not safety.
    assert all(expected == CHAT and got == FULL for _, _, expected, got in misses), misses
    assert total_ok / total >= 0.90


# ---------------------------------------------------------------------------
# Rule 7 — LIGHT (Phase 3): present, off by default, exactly 9 shapes
# ---------------------------------------------------------------------------
LIGHT_SHAPED: tuple[tuple[str, str], ...] = (
    ("what is 2+2", "arith"),
    ("What is 15 * 12?", "arith"),
    ("what's 12 x 8", "arith"),
    ("what is a ledger", "what_is_a"),
    ("define liquidity", "define"),
    ("what is inflation", "abstract_noun"),
    ("what does pro rata mean", "what_does_mean"),
    ("how does a heat pump work", "how_does_work"),
    ("translate good evening", "translate"),
    ("convert 5 miles to km", "unit_conversion"),
    ("tell me a joke", "joke"),
    ("Tell me a riddle!", "joke"),
)


@pytest.mark.parametrize("text,pattern", LIGHT_SHAPED)
def test_light_is_off_by_default(text, pattern):
    assert route(text).tier == FULL
    assert route(text, light_mode=None).tier == FULL  # env unset → off


@pytest.mark.parametrize("text,pattern", LIGHT_SHAPED)
def test_light_strict_on_an_allow_listed_model(text, pattern):
    d = light_strict(text)
    assert d.tier == LIGHT, (text, d)
    assert d.reason_codes == (f"light:{pattern}",)
    # Also after a CHAT or a LIGHT reply…
    assert light_strict(text, AFTER_CHAT).tier == LIGHT
    assert light_strict(text, AFTER_LIGHT).tier == LIGHT
    # …but never after a FULL reply, closed or not.
    for state in (CLOSED_FULL, CLOSED_UNROUTED, ESCALATED_CHAT, TOOLED):
        assert light_strict(text, state).tier == FULL, (text, state)


def test_light_needs_strict_and_an_allow_listed_model(monkeypatch):
    assert light_strict("what is 2+2", model="other-model").reason_codes[0] == "light:model"
    assert light_strict("what is 2+2", model=None).tier == FULL
    d = light_strict("what is 2+2", light_mode="shadow")
    assert d.tier == FULL and "light:shadow" in d.reason_codes
    assert light_strict("what is 2+2", light_mode="on").tier == FULL  # unknown → off
    # From the environment: strict but an empty model list keeps LIGHT off.
    monkeypatch.delenv("HERMES_ROUTER_LIGHT_MODELS")
    monkeypatch.setenv("HERMES_ROUTER_LIGHT", "strict")
    inp = dict(model=LIGHT_MODEL, light_mode=None)
    assert route("what is 2+2", **inp).tier == FULL
    monkeypatch.setenv("HERMES_ROUTER_LIGHT_MODELS", f"x, {LIGHT_MODEL}")
    assert route("what is 2+2", **inp).tier == LIGHT


@pytest.mark.parametrize("text", (
    # The prototype's measured leaks and the plan's named FULL-needing shapes.
    "explain the delay", "how much is left", "give me a quote for 20 bags",
    "what is the total", "define the scope for the website project",
    "tell me a summary of the week", "what is my net position",
    "what's a good time to meet Ali", "what is the population of the farm",
    "what's a balance sheet", "what is the status", "what is a rent roll",
    "define our margin", "what does KRA mean", "what does my lease mean",
    "translate my email to french", "what is 5 kg in usd", "convert 100 usd to kes",
    "what is 10% of 300", "what is a quote", "define arrears", "what is a payment",
    "what is a tenant", "how does the booking work", "what is a jira",
    "what does gak stand for", "who is a landlord", "what is a meeting at 5pm",
    "what is a deadline", "tell me a joke about Amal", "what are the latest news",
    "what is a new email", "what is 2+2 for afk", "define nour hammam",
))
def test_light_strict_never_takes_a_full_needing_phrase(text):
    assert light_strict(text).tier == FULL, text


def test_light_allow_list_is_exactly_the_nine_published_patterns():
    # TURN_COST_PLAN step 3.2, verbatim (8 is "unit conversion on a fixed unit
    # list", published as prose, so only its presence is checked).
    published = {
        "arith": r"^(what is |whats |calculate |compute )?[\d\s.+\-*/x×÷^()=]+$",
        "what_is_a": r"^(what is|whats|what are) (a|an) [a-z][a-z\s-]{0,40}$",
        "define": r"^(define|definition of|meaning of) [a-z][a-z\s-]{0,40}$",
        "abstract_noun": r"^(what is|whats) [a-z-]*(ism|tion|sion|ology|ity|ance|ence|ment|sis|ics)$",
        "what_does_mean": r"^what does [a-z0-9\s-]{1,30} (mean|stand for)$",
        "how_does_work": r"^how (do|does) (a |an )?[a-z\s-]{1,40} work$",
        "translate": r"^(translate|how do you say) .{1,80}$",
        "joke": r"^(tell me|give me) (a |an |another )?(joke|fun fact|riddle)$",
    }
    names = [name for name, _ in tr.LIGHT_ALLOW_LIST]
    assert names == ["arith", "what_is_a", "define", "abstract_noun", "what_does_mean",
                     "how_does_work", "translate", "unit_conversion", "joke"]
    patterns = dict(tr.LIGHT_ALLOW_LIST)
    for name, source in published.items():
        assert patterns[name].pattern == source, name
    # Currency is not on the unit list.
    assert not patterns["unit_conversion"].match("convert 5 usd to kes")
    assert patterns["unit_conversion"].match("convert 5 miles to km")


# ---------------------------------------------------------------------------
# Fail-open: any exception → FULL
# ---------------------------------------------------------------------------
def test_an_exception_in_the_rules_gives_full(monkeypatch, caplog):
    secret = "Zanzibar-tenant-Wanjiku"

    def boom(_inp):
        raise ValueError(f"boom on {secret}")

    monkeypatch.setattr(tr, "_decide", boom)
    with caplog.at_level(logging.WARNING, logger="hermes.turn_router"):
        d = decide(RouterInput(text=f"hi {secret}", route="auto", mode="on", state=FRESH))
    assert d == Decision(FULL, ("router_error",))
    # Only the exception TYPE is logged, never its message (it may quote text).
    assert "ValueError" in caplog.text
    assert secret not in caplog.text and "Zanzibar" not in caplog.text


@pytest.mark.parametrize("target", ["pending_from", "_normal_forms", "_segment", "_vocab"])
def test_an_exception_deep_in_a_helper_gives_full(monkeypatch, target):
    def boom(*_a, **_k):
        raise RuntimeError("helper failed")

    monkeypatch.setattr(tr, target, boom)
    assert route("hi").tier == FULL
    assert route("hi").reason_codes == ("router_error",)


def test_a_config_lookup_error_gives_full(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(Config, "apps", classmethod(lambda cls: boom()))
    assert route("hi").tier == FULL
    monkeypatch.undo()
    monkeypatch.setattr(Config, "router_mode", classmethod(lambda cls: boom()))
    assert decide(RouterInput(text="hi", route="auto", state=FRESH)).tier == FULL


@pytest.mark.parametrize("bad_state", [
    TurnState(last_assistant_text=123, last_stop_reason="end_turn"),  # type: ignore[arg-type]
    TurnState(last_tool_calls="garbage", last_stop_reason="end_turn"),  # type: ignore[arg-type]
    TurnState(last_route=["chat"], last_stop_reason=5, last_assistant_text="Hi"),  # type: ignore[arg-type]
    TurnState(ok="yes"),  # type: ignore[arg-type]  # truthy but not True → unknown
    object(),  # not a TurnState at all
])
def test_malformed_state_never_raises_and_never_routes_cheaper(bad_state):
    for text in ("hi", "thanks", "great"):
        d = decide(RouterInput(text=text, route="auto", mode="on", state=bad_state))  # type: ignore[arg-type]
        assert d.tier == FULL, (bad_state, text, d)


@pytest.mark.parametrize("bad_text", [None, 42, b"hi", ["hi"], {"text": "hi"}])
def test_non_string_text_is_full(bad_text):
    d = route(bad_text)
    assert d.tier == FULL and d.reason_codes == ("bad_input",)


def test_a_pasted_document_is_full_without_a_word_scan():
    d = route("hi " * 2000)
    assert d.tier == FULL and "too_long" in d.reason_codes
    d = route("thanks " * 500, GUARD)
    assert {"too_long", "pending:guardrail"} <= set(d.reason_codes)


# ---------------------------------------------------------------------------
# Reason codes and the decision log — category codes only, never text
# ---------------------------------------------------------------------------
_LOG_PROBES: tuple[tuple[str, TurnState], ...] = (
    ("hi Zanzibar", FRESH),
    ("rent status for Wanjikuville?", FRESH),
    ("email amal.mwangi@example.com about KAN-1234 by 5pm", FRESH),
    ("what is KES 900k in Quuxdollars", FRESH),
    ("Grace called, call her back on +254 712 345 678", FRESH),
    ("https://flibbertigibbet.example.org/path?q=secret", FRESH),
    ("thanks Mbuguanator \U0001F44D", GUARD),
    ("the second Snorkelwhistle one", TOOLED),
    ("hello Hermes", AFTER_CHAT),
    ("define Pemberton", FRESH),
    ("how is Nour Hammam doing", FRESH),
)
# Tokens from the probes above that must never reach a log line. (Short
# function words — "hi", "the" — are not listed: they can occur inside the
# line's own fixed keys and codes, e.g. "the" in nothing, "hi" in "shadow"…)
_SECRET_TOKENS: tuple[str, ...] = (
    "zanzibar", "wanjikuville", "amal", "mwangi", "example", "kan-1234", "1234",
    "quuxdollars", "900k", "grace", "254", "712", "345", "678", "flibbertigibbet",
    "secret", "mbuguanator", "snorkelwhistle", "pemberton", "nour", "hammam",
    "rent", "status", "called", "second", "define", "hermes", "thanks",
)
# The line's own closed vocabulary: its keys, the category codes and the fixed
# values. A message word that IS one of these ("email" is both a word and the
# `email` code) can appear in a line legitimately, as the category name.
_LOG_VOCAB: frozenset[str] = frozenset(
    part for code in REASON_CODES for part in code.split(":")
) | {
    "event", "turn_router", "decision", "tier", "reasons", "pending", "affirmation",
    "words", "session", "mode", "light", "route", "voice", "effort", "chat", "full",
    "auto", "other", "shadow", "strict", "unknown", "null", "true", "false", "low",
    "medium", "high",
}


def _all_decisions() -> list[Decision]:
    out = [route(t, s) for t, s in _LOG_PROBES]
    out += [_suite_decision(c) for _, _, c in ALL_CASES]
    out += [light_strict(t) for t, _ in LIGHT_SHAPED]
    out += [route("what is 2+2", light_mode="shadow", model=LIGHT_MODEL)]
    out += [route("what is 2+2", light_mode="strict", model="nope")]
    out += [decide(_bypass_input("hi", o)) for o in BYPASSES.values()]
    out += [route(t, s) for t in ("hello?", "great") for s in FAILED_STATES.values()]
    out += [route("a " * 3000), route(None), route("", GUARD), route("ok", DRAFT), route("hi", OAUTH)]
    return out


def test_every_reason_code_is_in_the_closed_vocabulary():
    seen: set[str] = set()
    for d in _all_decisions():
        assert d.tier in (CHAT, LIGHT, FULL)
        assert d.reason_codes, d
        for code in d.reason_codes:
            assert code in REASON_CODES, (code, d)
            assert re.fullmatch(r"[a-z_]+(:[a-z_]+)?", code), code
            seen.add(code)
        assert d.pending_kind in (None, PENDING_GUARDRAIL, PENDING_DRAFT, PENDING_OAUTH)
    # Sanity: the probes exercise a broad slice of the vocabulary.
    assert {"greeting", "closer", "ack", "business", "app", "cap_word", "email", "url",
            "phone", "jira_key", "currency", "time", "open:tool", "pending:guardrail",
            "approval_emoji", "ordinal", "anaphora", "empty", "bypass:state"} <= seen


@pytest.mark.parametrize("text,state", _LOG_PROBES)
def test_decision_log_line_carries_no_message_token(text, state, caplog):
    session = "sess-0f9e8d7c-Zanzibar"
    inp = RouterInput(text=text, route="auto", mode="on", state=state, session_key=session,
                      voice=True)
    with caplog.at_level(logging.INFO, logger="hermes.turn_router"):
        decision = decide_and_log(inp, effort="low")
    records = [r for r in caplog.records if r.name == "hermes.turn_router"]
    assert len(records) == 1, "exactly one JSON log line per decision"
    line = records[0].getMessage()
    record = json.loads(line)
    assert set(record) == {"event", "tier", "reasons", "pending", "affirmation", "words",
                           "session", "mode", "light", "route", "voice", "effort"}
    assert record["tier"] == decision.tier
    assert record["reasons"] == list(decision.reason_codes)
    assert record["words"] == tr.word_count(text) and record["words"] > 0
    assert record["session"] and re.fullmatch(r"[0-9a-f]{12}", record["session"])
    assert record["effort"] == "low" and record["voice"] is True and record["mode"] == "on"
    lowered = line.lower()
    for token in _SECRET_TOKENS:
        assert token not in lowered, (token, line)
    assert session.lower() not in lowered and "sess-" not in lowered
    # No other message word of 4+ characters survives either.
    for word in re.findall(r"[A-Za-z0-9@.+:/-]{4,}", text):
        if word.lower() not in _LOG_VOCAB:
            assert word.lower() not in lowered, (word, line)


def test_log_record_maps_codes_outside_the_vocabulary_to_unknown():
    # Belt and braces against a future code built from text (the prototype
    # emitted `proper_noun:Nairobi`).
    inp = RouterInput(text="what's the weather in Nairobi", route="auto", mode="on", state=FRESH)
    rec = decision_log_record(inp, Decision(FULL, ("proper_noun:Nairobi", "business"), "Nairobi"))
    assert rec["reasons"] == ["unknown", "business"]
    assert rec["pending"] is None
    assert "nairobi" not in json.dumps(rec).lower()
    line = log_decision(inp, Decision("Nairobi", ("x",)))
    assert line is not None and "nairobi" not in line.lower()
    assert json.loads(line)["tier"] == "unknown"


def test_decide_and_log_is_silent_while_the_router_is_off(caplog, monkeypatch):
    inp = RouterInput(text="hi", route="auto", state=FRESH, session_key="s1")
    with caplog.at_level(logging.INFO, logger="hermes.turn_router"):
        assert decide_and_log(inp).reason_codes == ("bypass:mode_off",)
    assert not [r for r in caplog.records if r.name == "hermes.turn_router"]
    monkeypatch.setenv("HERMES_ROUTER_MODE", "shadow")
    with caplog.at_level(logging.INFO, logger="hermes.turn_router"):
        d = decide_and_log(inp)
    lines = [r.getMessage() for r in caplog.records if r.name == "hermes.turn_router"]
    assert d.tier == CHAT and len(lines) == 1
    assert json.loads(lines[0])["mode"] == "shadow"
    assert json.loads(lines[0])["tier"] == CHAT


def test_logging_failure_never_breaks_a_turn(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("log handler down")

    monkeypatch.setattr(tr.log, "info", boom)
    assert decide_and_log(RouterInput(text="hi", route="auto", mode="on", state=FRESH)).tier == CHAT


def test_session_hash_is_stable_and_not_reversible():
    assert tr.session_hash("abc") == tr.session_hash("abc")
    assert tr.session_hash("abc") != tr.session_hash("abd")
    assert "abc" not in tr.session_hash("abc")
    assert tr.session_hash(None) is None and tr.session_hash("") is None


def test_decision_as_dict_is_json_safe():
    d = route("yes \U0001F44D", GUARD)
    assert json.loads(json.dumps(d.as_dict())) == {
        "tier": FULL, "reason_codes": list(d.reason_codes),
        "pending_kind": PENDING_GUARDRAIL, "affirmation": True,
    }


# ---------------------------------------------------------------------------
# Config knobs (hermes/config.py) — defaults, and garbage fails to today
# ---------------------------------------------------------------------------
def test_router_knob_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_ROUTER_LIGHT_MODELS")
    assert Config.router_mode() == "off"
    assert Config.router_chat_max_tokens() == 1024
    assert Config.router_lite_history() == 2
    assert Config.router_lite_history_chars() == 600
    assert Config.router_extra_terms() == ()
    assert Config.router_shadow_sample() == 0.0
    assert Config.router_light() == "off"
    assert Config.router_light_models() == ()


@pytest.mark.parametrize("name,value,getter,expected", [
    ("HERMES_ROUTER_MODE", "ON", "router_mode", "on"),
    ("HERMES_ROUTER_MODE", " shadow ", "router_mode", "shadow"),
    ("HERMES_ROUTER_MODE", "true", "router_mode", "off"),
    ("HERMES_ROUTER_MODE", "1", "router_mode", "off"),
    ("HERMES_ROUTER_CHAT_MAX_TOKENS", "512", "router_chat_max_tokens", 512),
    ("HERMES_ROUTER_CHAT_MAX_TOKENS", "0", "router_chat_max_tokens", 1024),
    ("HERMES_ROUTER_CHAT_MAX_TOKENS", "lots", "router_chat_max_tokens", 1024),
    ("HERMES_ROUTER_LITE_HISTORY", "-3", "router_lite_history", 0),
    ("HERMES_ROUTER_LITE_HISTORY", "x", "router_lite_history", 2),
    ("HERMES_ROUTER_LITE_HISTORY_CHARS", "300", "router_lite_history_chars", 300),
    ("HERMES_ROUTER_EXTRA_TERMS", " Wanjiku, ,Mama Njeri ", "router_extra_terms",
     ("Wanjiku", "Mama Njeri")),
    ("HERMES_ROUTER_SHADOW_SAMPLE", "0.1", "router_shadow_sample", 0.1),
    ("HERMES_ROUTER_SHADOW_SAMPLE", "2", "router_shadow_sample", 1.0),
    ("HERMES_ROUTER_SHADOW_SAMPLE", "-1", "router_shadow_sample", 0.0),
    ("HERMES_ROUTER_SHADOW_SAMPLE", "nan", "router_shadow_sample", 0.0),
    ("HERMES_ROUTER_SHADOW_SAMPLE", "some", "router_shadow_sample", 0.0),
    ("HERMES_ROUTER_LIGHT", "STRICT", "router_light", "strict"),
    ("HERMES_ROUTER_LIGHT", "on", "router_light", "off"),
    ("HERMES_ROUTER_LIGHT_MODELS", "a, b,", "router_light_models", ("a", "b")),
])
def test_router_knob_parsing(monkeypatch, name, value, getter, expected):
    monkeypatch.setenv(name, value)
    assert getattr(Config, getter)() == expected
