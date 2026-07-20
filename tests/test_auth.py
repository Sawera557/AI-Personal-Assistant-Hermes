"""Tests for the passwordless email-OTP auth gate (hermes/authn.py + the
/api/auth/* routes + the login middleware).

Self-contained and offline: HERMES_DISABLE_DB routes the auth store to its
in-memory fallback, and the SES send is monkeypatched to just capture the code —
no database, AWS creds, or network needed.

    python tests/test_auth.py     # or: pytest tests/test_auth.py -q
"""
from __future__ import annotations

import os
import sys

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from hermes import authn  # noqa: E402
from hermes.config import Config  # noqa: E402
from hermes.db import auth as auth_repo  # noqa: E402

ALLOWED = "gandikediye@gmail.com"

# The most recent code the (monkeypatched) email sender was asked to deliver.
_last_code: dict = {}


def _capture_sender(email: str, code: str) -> None:
    _last_code["email"] = email
    _last_code["code"] = code


def setup_module(module=None):
    """Turn auth on with test config, and route the email send to our capture."""
    Config.AUTH_ENABLED = True
    Config.AUTH_SESSION_SECRET = "test-secret-please-ignore"
    Config.AUTH_ALLOWED_EMAIL = ALLOWED
    Config.SES_SENDER = "Hermes <login@example.com>"
    Config.AUTH_COOKIE_SECURE = False  # TestClient speaks http; keep the cookie
    Config.AUTH_SEND_MAX_PER_HOUR = 5
    Config.AUTH_MAX_CODE_ATTEMPTS = 5
    authn._send_code_email = _capture_sender  # type: ignore[assignment]


def teardown_module(module=None):
    """Leave auth OFF so later test modules aren't gated."""
    Config.AUTH_ENABLED = False
    Config.AUTH_SESSION_SECRET = ""


def _reset():
    """Fresh in-memory store + capture between tests."""
    auth_repo._MEM_CODES.clear()
    auth_repo._MEM_SESSIONS.clear()
    _last_code.clear()


def _client() -> TestClient:
    return TestClient(app_module.app)


def _login(client: TestClient, email: str = ALLOWED) -> None:
    """Drive the full request→verify flow so `client` ends up with a session cookie."""
    r = client.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200, r.text
    code = _last_code["code"]
    r = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text


# --- the gate --------------------------------------------------------------

def test_public_paths_open():
    _reset()
    c = _client()
    assert c.get("/api/health").status_code == 200
    assert c.get("/login").status_code == 200


def test_gated_api_401_without_session():
    _reset()
    c = _client()
    r = c.get("/api/conversations")
    assert r.status_code == 401
    assert r.json()["error"]


def test_gated_page_redirects_to_login():
    _reset()
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_fails_closed_without_secret():
    _reset()
    saved = Config.AUTH_SESSION_SECRET
    Config.AUTH_SESSION_SECRET = ""  # enabled but unconfigured
    try:
        r = _client().get("/api/conversations")
        assert r.status_code == 503
    finally:
        Config.AUTH_SESSION_SECRET = saved


# --- request code ----------------------------------------------------------

def test_disallowed_email_is_generic_and_sends_nothing():
    _reset()
    c = _client()
    r = c.post("/api/auth/request", json={"email": "attacker@evil.com"})
    assert r.status_code == 200          # same generic success — no enumeration
    assert "code" not in _last_code       # ...but nothing was actually sent


def test_allowed_email_gets_a_code():
    _reset()
    c = _client()
    r = c.post("/api/auth/request", json={"email": ALLOWED})
    assert r.status_code == 200
    assert _last_code.get("email") == ALLOWED
    assert len(_last_code["code"]) == 6 and _last_code["code"].isdigit()


def test_send_rate_limited():
    _reset()
    c = _client()
    for _ in range(Config.AUTH_SEND_MAX_PER_HOUR):
        assert c.post("/api/auth/request", json={"email": ALLOWED}).status_code == 200
    r = c.post("/api/auth/request", json={"email": ALLOWED})
    assert r.status_code == 429


# --- verify + session ------------------------------------------------------

def test_wrong_code_rejected():
    _reset()
    c = _client()
    c.post("/api/auth/request", json={"email": ALLOWED})
    r = c.post("/api/auth/verify", json={"email": ALLOWED, "code": "000000"})
    assert r.status_code == 401


def test_correct_code_grants_session():
    _reset()
    c = _client()
    _login(c)
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["email"] == ALLOWED
    # A gated endpoint is now reachable.
    assert c.get("/api/conversations").status_code == 200


def test_code_is_single_use():
    _reset()
    c = _client()
    c.post("/api/auth/request", json={"email": ALLOWED})
    code = _last_code["code"]
    assert c.post("/api/auth/verify", json={"email": ALLOWED, "code": code}).status_code == 200
    # Re-using the same code fails — it was consumed.
    fresh = _client()
    assert fresh.post("/api/auth/verify", json={"email": ALLOWED, "code": code}).status_code == 401


def test_code_locks_after_max_attempts():
    _reset()
    c = _client()
    c.post("/api/auth/request", json={"email": ALLOWED})
    code = _last_code["code"]
    for _ in range(Config.AUTH_MAX_CODE_ATTEMPTS):
        assert c.post("/api/auth/verify", json={"email": ALLOWED, "code": "999999"}).status_code == 401
    # Even the CORRECT code is now dead — the code locked and must be re-requested.
    assert c.post("/api/auth/verify", json={"email": ALLOWED, "code": code}).status_code == 401


def test_logout_revokes_session():
    _reset()
    c = _client()
    _login(c)
    assert c.get("/api/auth/me").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    # Cookie cleared + session revoked → back to 401.
    assert c.get("/api/conversations").status_code == 401


def _run_all():
    setup_module()
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
    teardown_module()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
