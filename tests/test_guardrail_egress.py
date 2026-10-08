"""Guardrails #3 and #4: refusals that hold even with confirmations off and in
unattended runs.

  #3 secret_egress — a send or external write carrying a credential or a card
     number is refused outright.
  #4 send_limit    — at most Config.GUARDRAIL_MAX_SENDS_PER_TURN sends/deletes
     run in one turn.

Offline: no DB, no providers; the guardrail module is exercised directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import guardrails as g
from hermes.config import Config

SESSION = "guardrail-egress-tests"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"  # split so scanners don't flag this file
CARD = "4111 1111 1111 1111"       # the standard Visa test number, passes Luhn


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("HERMES_GUARDRAIL_SECRET", "test-secret-not-a-real-one")
    monkeypatch.setenv("HERMES_DISABLE_DB", "1")
    g._secret_cache = None
    saved = dict(g._overrides)
    g._overrides.clear()
    g.clear_session(SESSION)
    g.set_confirm_writes(False)  # isolate the refusals from the confirm gate
    yield
    g._overrides.clear()
    g._overrides.update(saved)
    g._secret_cache = None
    g.clear_session(SESSION)


def _email(body: str) -> dict:
    return {"to": "client@example.com", "subject": "Hi", "body": body}


def _check(name, args, turn=1, interactive=True):
    return g.check(SESSION, name, args, "nh", interactive=interactive, turn=turn)


# ---- #3 secret_egress --------------------------------------------------------
@pytest.mark.parametrize("body,kind", [
    (f"the key is {AWS}", "AWS access key"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE...", "private key"),
    ("use sk-ant-" + "a" * 40, "API secret key"),
    ("token ghp_" + "b" * 36, "GitHub token"),
    (f"card {CARD} exp 12/29", "payment card number"),
])
def test_secret_in_send_is_blocked(body, kind):
    d = _check("send_email", _email(body))
    assert d.allow is False and d.reason == "blocked" and d.approval_token is None
    assert kind in d.summary


def test_secret_nested_in_maton_write_is_blocked():
    d = _check("maton_request", {"method": "POST", "path": "/gmail/send", "body": {"raw": {"text": AWS}}})
    assert d.reason == "blocked"


def test_blocked_in_unattended_runs_too():
    assert _check("send_email", _email(AWS), interactive=False).reason == "blocked"


@pytest.mark.parametrize("body", [
    "Invoice 1234 5678 9012 3456 attached",       # not a card prefix + fails Luhn
    "password: please reset it",                   # prose, not a credential
    "Order #4111111111111112 shipped",             # card-shaped but fails Luhn
    "Your reference is AKIA-1234",                 # too short for an AWS key
])
def test_ordinary_text_is_not_blocked(body):
    assert _check("send_email", _email(body)).allow is True


def test_reads_and_internal_writes_are_not_scanned():
    assert _check("drive_read_file", {"query": AWS}).allow is True
    assert _check("create_file", {"content": AWS}).allow is True


def test_secret_egress_can_be_switched_off():
    g.set_guardrail_enabled("secret_egress", False)
    assert _check("send_email", _email(AWS)).allow is True


def test_secret_egress_on_for_one_business_only():
    g.set_guardrail_enabled("secret_egress", False)
    g.set_guardrail_enabled("secret_egress", True, "nh")
    assert _check("send_email", _email(AWS)).reason == "blocked"


# ---- #4 send_limit -----------------------------------------------------------
def test_sends_capped_per_turn_and_reset_next_turn(monkeypatch):
    monkeypatch.setattr(Config, "GUARDRAIL_MAX_SENDS_PER_TURN", 3)
    for i in range(3):
        assert _check("send_email", _email(f"note {i}")).allow is True
    d = _check("send_email", _email("one too many"))
    assert d.allow is False and d.reason == "blocked" and "3 sends or deletes" in d.summary
    assert _check("send_email", _email("next turn"), turn=2).allow is True


def test_deletes_count_reads_do_not(monkeypatch):
    monkeypatch.setattr(Config, "GUARDRAIL_MAX_SENDS_PER_TURN", 1)
    for _ in range(5):
        assert _check("maton_request", {"method": "GET", "path": "/x"}).allow is True
    assert _check("maton_request", {"method": "DELETE", "path": "/x/1"}).allow is True
    assert _check("maton_request", {"method": "DELETE", "path": "/x/2"}).reason == "blocked"


def test_held_actions_do_not_count(monkeypatch):
    monkeypatch.setattr(Config, "GUARDRAIL_MAX_SENDS_PER_TURN", 1)
    g.set_confirm_writes(True)
    for i in range(3):
        assert _check("send_email", _email(f"held {i}")).reason == "confirm"
    assert g._sends_so_far(SESSION, 1) == 0


def test_draft_prepare_does_not_count(monkeypatch):
    monkeypatch.setattr(Config, "GUARDRAIL_MAX_SENDS_PER_TURN", 1)
    for _ in range(3):
        assert _check("email_with_attachment", _email("draft")).allow is True
    assert g._sends_so_far(SESSION, 1) == 0


def test_send_limit_applies_unattended_and_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(Config, "GUARDRAIL_MAX_SENDS_PER_TURN", 1)
    assert _check("send_email", _email("a"), interactive=False).allow is True
    assert _check("send_email", _email("b"), interactive=False).reason == "blocked"
    g.set_guardrail_enabled("send_limit", False)
    assert _check("send_email", _email("c"), interactive=False).allow is True


# ---- registry ----------------------------------------------------------------
def test_new_guardrails_listed_for_settings_ui():
    rows = {r["key"]: r for r in g.list_guardrails()}
    assert rows["secret_egress"]["enabled"] is True
    assert rows["send_limit"]["enabled"] is True


def test_turning_them_off_via_tool_still_needs_confirmation():
    g.set_confirm_writes(False)
    for key in ("secret_egress", "send_limit"):
        d = _check("set_guardrail", {"guardrail": key, "enabled": False})
        assert d.allow is False and d.reason == "confirm"
