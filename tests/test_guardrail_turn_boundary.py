"""Guardrail #1 must enforce human consent, not just unchanged arguments.

Before the turn boundary, `check()` only verified that the model echoed back a
token it had just been handed — something it can do unprompted inside the same
turn. That enforced "the arguments did not change" while the module's docstring
promised the guarantee "holds regardless of what the model decides".

Offline: no network, no database.
"""
import sys
from pathlib import Path

# CI runs each test file as a script (`python tests/foo.py`), which puts tests/
# on sys.path rather than the repo root — so a bare `from hermes import ...`
# raises ModuleNotFoundError there while passing under pytest.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import guardrails as g

SESSION = "turn-boundary-tests"
TOOL = "send_email"
ARGS = {"to": "client@example.com", "subject": "Invoice", "body": "Please pay."}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("HERMES_GUARDRAIL_SECRET", "test-secret-not-a-real-one")
    # Keep this offline: without it every check() round-trips to Postgres to
    # resolve the guardrail toggle, which is both slow and a live dependency.
    monkeypatch.setenv("HERMES_DISABLE_DB", "1")
    g._secret_cache = None
    g._pending.pop(SESSION, None)
    g._spent.pop(SESSION, None)
    yield
    g._secret_cache = None
    g._pending.pop(SESSION, None)
    g._spent.pop(SESSION, None)


def _check(args, turn):
    return g.check(SESSION, TOOL, args, "nh", interactive=True, turn=turn)


def test_first_call_is_held_and_returns_a_token():
    d = _check(ARGS, turn=1)
    assert d.allow is False
    assert d.reason == "confirm"
    assert d.approval_token


def test_the_model_cannot_approve_itself_in_the_same_turn():
    """THE regression. The model is handed a token and immediately replays it
    with no user input — previously this sent the email."""
    issued = _check(ARGS, turn=1).approval_token
    replay = _check({**ARGS, "approval_token": issued}, turn=1)
    assert replay.allow is False


def test_the_same_turn_replay_tells_the_model_to_stop():
    """Distinct from a fresh ask: retrying cannot succeed, so the model must be
    told to wait rather than burn its tool-iteration budget."""
    issued = _check(ARGS, turn=1).approval_token
    replay = _check({**ARGS, "approval_token": issued}, turn=1)
    assert replay.reason == "awaiting"


def test_approval_works_on_the_next_turn():
    """The legitimate flow must still function — this is the one that would
    break users if the fix were too strict."""
    issued = _check(ARGS, turn=1).approval_token
    assert _check({**ARGS, "approval_token": issued}, turn=2).allow is True


def test_approval_still_works_several_turns_later():
    """A user who approves after some back-and-forth is not punished for it."""
    issued = _check(ARGS, turn=3).approval_token
    assert _check({**ARGS, "approval_token": issued}, turn=9).allow is True


def test_changing_the_arguments_invalidates_the_token():
    """Pre-existing protection that must survive the change."""
    issued = _check(ARGS, turn=1).approval_token
    tampered = {**ARGS, "to": "attacker@evil.com", "approval_token": issued}
    assert _check(tampered, turn=2).allow is False


def test_a_token_forged_for_an_earlier_turn_is_rejected():
    """The turn is inside the signed payload, so it cannot be edited to claim
    the confirmation happened earlier than it did."""
    issued = _check(ARGS, turn=5).approval_token
    _turn, _, digest = issued.partition(".")
    forged = f"1.{digest}"  # same signature, earlier turn claimed
    assert _check({**ARGS, "approval_token": forged}, turn=6).allow is False


def test_a_token_is_not_derivable_without_the_secret(monkeypatch):
    """It used to be a bare SHA-256 anyone could recompute offline. With the
    turn now carried in the token, an unkeyed hash would let the model mint its
    own 'approved earlier' token."""
    mine = g.token_for(TOOL, ARGS, "nh", 1)
    monkeypatch.setenv("HERMES_GUARDRAIL_SECRET", "a-different-secret")
    g._secret_cache = None
    assert g.token_for(TOOL, ARGS, "nh", 1) != mine


def test_unattended_runs_are_unaffected():
    """Scheduled runs have no human to ask; the gate must not deadlock them."""
    assert g.check(SESSION, TOOL, ARGS, "nh", interactive=False, turn=1).allow is True


def test_read_only_tools_are_never_guarded():
    assert g.check(SESSION, "search_business_knowledge", {"query": "x"},
                   "nh", interactive=True, turn=1).allow is True


def test_callers_without_a_turn_keep_the_legacy_behaviour():
    """rag.py / tools.py import this module; a caller with no conversational
    turn must not deadlock because it cannot supply one."""
    issued = g.check(SESSION, TOOL, ARGS, "nh", interactive=True, turn=None).approval_token
    assert g.check(SESSION, TOOL, {**ARGS, "approval_token": issued},
                   "nh", interactive=True, turn=None).allow is True


def test_pending_overflow_evicts_one_not_all():
    """_issue used to clear() the whole bucket at the cap, silently cancelling
    every other approval the user had in flight."""
    for i in range(g._MAX_PENDING_PER_SESSION + 5):
        g.check(SESSION, TOOL, {**ARGS, "subject": f"n{i}"}, "nh",
                interactive=True, turn=1)
    assert len(g._pending[SESSION]) >= g._MAX_PENDING_PER_SESSION - 1


def test_an_approval_can_only_be_spent_once():
    """One approval, one execution. Without this the model could replay an
    approved token inside the later turn and send the same email twice."""
    issued = _check(ARGS, turn=1).approval_token
    assert _check({**ARGS, "approval_token": issued}, turn=2).allow is True
    second = _check({**ARGS, "approval_token": issued}, turn=2)
    assert second.allow is False
    assert second.reason == "spent"


def test_a_spent_approval_stays_spent_on_later_turns():
    issued = _check(ARGS, turn=1).approval_token
    assert _check({**ARGS, "approval_token": issued}, turn=2).allow is True
    assert _check({**ARGS, "approval_token": issued}, turn=7).allow is False
