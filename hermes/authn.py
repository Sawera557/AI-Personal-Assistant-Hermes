"""Passwordless email-OTP authentication for the single-user Hermes app.

Flow:
  1. POST /api/auth/request {email}  → if `email` is the one allowlisted address,
     a 6-digit code is emailed to it (AWS SES). Rate-limited; generic response.
  2. POST /api/auth/verify {email, code} → on match, a random session token is
     minted, its HMAC stored server-side, and the raw token set as an HttpOnly,
     Secure, SameSite=Strict cookie.
  3. Every gated request re-validates that cookie against the DB (revocable,
     survives serverless cold starts — no in-memory session state).

At-rest, only HMACs are stored: leaking the DB reveals neither a live code nor a
forgeable session. `AUTH_SESSION_SECRET` is the HMAC key; without it the app
fails closed (see `is_configured`).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response

from .config import Config
from .db import auth as auth_repo

log = logging.getLogger("hermes.authn")

# Status codes returned by request_code(), mapped to HTTP responses by the route.
SENT = "sent"
NOT_ALLOWED = "not_allowed"
RATE_LIMITED = "rate_limited"


def is_enabled() -> bool:
    """Whether the auth gate is switched on at all."""
    return Config.AUTH_ENABLED


def is_configured() -> bool:
    """Whether auth has the secret it needs to operate. When enabled but not
    configured, the middleware fails CLOSED — a missing secret never means open."""
    return bool(Config.AUTH_SESSION_SECRET)


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def is_allowed(email: str) -> bool:
    """True only for the single allowlisted address (constant-time compare so we
    don't leak it through timing)."""
    allowed = Config.AUTH_ALLOWED_EMAIL
    return bool(allowed) and hmac.compare_digest(normalize_email(email), allowed)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hmac(*parts: str) -> str:
    """Keyed HMAC-SHA256 over the joined parts, hex. The key is the session
    secret, so hashes are useless to anyone who only has the database."""
    key = Config.AUTH_SESSION_SECRET.encode("utf-8")
    msg = "\x1f".join(parts).encode("utf-8")  # unit-separator delimiter
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# ── Login codes ───────────────────────────────────────────────────────────────
def request_code(email: str, *, sender=None) -> str:
    """Issue and email a login code if `email` is allowed and under the send cap.

    Returns SENT / NOT_ALLOWED / RATE_LIMITED. Callers keep the NOT_ALLOWED case
    indistinguishable from SENT in the HTTP response (no enumeration). `sender`
    is injectable for tests; defaults to the real SES sender.
    """
    email = normalize_email(email)
    if not is_allowed(email):
        return NOT_ALLOWED

    window_start = _now() - timedelta(hours=1)
    if auth_repo.count_recent_codes(email, window_start) >= Config.AUTH_SEND_MAX_PER_HOUR:
        log.warning("Login code rate limit hit for allowed email")
        return RATE_LIMITED

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = _now() + timedelta(seconds=Config.AUTH_CODE_TTL_SECONDS)
    auth_repo.create_login_code(email, _hmac(email, code), expires_at)

    (sender or _send_code_email)(email, code)
    return SENT


def verify_code(email: str, code: str) -> bool:
    """True iff `code` is the current live code for `email`. Consumes the code on
    success; counts the try and locks the code past the attempt cap on failure."""
    email = normalize_email(email)
    code = (code or "").strip()
    if not is_allowed(email) or not code:
        return False

    active = auth_repo.get_active_code(email)
    if not active:
        return False

    # Too many wrong tries already → treat as dead (a fresh code is required).
    if active["attempts"] >= Config.AUTH_MAX_CODE_ATTEMPTS:
        auth_repo.consume_code(active["id"])
        return False

    expected = active["code_hash"]
    if hmac.compare_digest(expected, _hmac(email, code)):
        auth_repo.consume_code(active["id"])
        return True

    attempts = auth_repo.bump_code_attempt(active["id"])
    if attempts >= Config.AUTH_MAX_CODE_ATTEMPTS:
        auth_repo.consume_code(active["id"])  # burn it; no brute-force runway
    return False


def _code_email_content(code: str) -> tuple[str, str, str]:
    """(subject, plaintext, html) for a login-code email."""
    ttl_min = max(1, Config.AUTH_CODE_TTL_SECONDS // 60)
    subject = "Your Hermes login code"
    text = (
        f"Your Hermes login code is: {code}\n\n"
        f"It expires in {ttl_min} minutes and can be used once.\n"
        "If you didn't request this, you can ignore this email."
    )
    html = (
        "<div style=\"font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:420px;margin:0 auto;color:#111\">"
        "<h2 style=\"margin:0 0 12px\">Hermes login</h2>"
        "<p style=\"margin:0 0 16px;color:#555\">Enter this code to sign in:</p>"
        f"<div style=\"font-size:34px;font-weight:700;letter-spacing:8px;"
        f"padding:16px 0;text-align:center;background:#f4f4f5;border-radius:10px\">{code}</div>"
        f"<p style=\"margin:16px 0 0;color:#888;font-size:13px\">Expires in {ttl_min} minutes. "
        "One-time use. If you didn't request this, ignore this email.</p></div>"
    )
    return subject, text, html


def _send_code_email(email: str, code: str) -> None:
    """Deliver the login code. Picks a provider by what's configured; raises on
    failure so the route can report it to the (single, legitimate) user rather
    than silently swallowing it.

    Order: dev-echo → SMTP → SES. SMTP wins over SES when both are set, since
    it's the simpler path for a single-user manual test (e.g. Gmail App Password).
    """
    # Dev-only escape hatch: print the code to the server console instead of
    # emailing. Hard-gated off in production — refuses to run when Vercel's VERCEL
    # env var is present, so a stray AUTH_DEV_ECHO can never leak a real code.
    if Config.AUTH_DEV_ECHO and not os.getenv("VERCEL"):
        log.warning("AUTH_DEV_ECHO: login code for %s is %s", email, code)
        return

    subject, text, html = _code_email_content(code)

    if Config.SMTP_HOST:
        _send_via_smtp(email, subject, text, html)
    elif Config.SES_SENDER:
        _send_via_ses(email, subject, text, html)
    else:
        raise RuntimeError(
            "No email delivery is configured — set SMTP_HOST (+ SMTP_USER/SMTP_PASSWORD) "
            "or SES_SENDER, or AUTH_DEV_ECHO=true for local testing."
        )


def _send_via_smtp(email: str, subject: str, text: str, html: str) -> None:
    """Send over SMTP (e.g. Gmail with an App Password). Uses SSL for port 465,
    otherwise STARTTLS."""
    import smtplib
    from email.message import EmailMessage

    from_addr = Config.SMTP_FROM or Config.SMTP_USER
    if not from_addr:
        raise RuntimeError("SMTP_FROM or SMTP_USER must be set to send login codes.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if Config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as server:
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as server:
            if Config.SMTP_STARTTLS:
                server.starttls()
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)


def _send_via_ses(email: str, subject: str, text: str, html: str) -> None:
    """Send over AWS SES (the production path)."""
    import boto3  # lazy: only needed when actually sending

    client = boto3.client("ses", region_name=Config.SES_REGION)
    client.send_email(
        Source=Config.SES_SENDER,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )


# ── Sessions ──────────────────────────────────────────────────────────────────
def start_session(email: str, *, user_agent: str | None = None, ip: str | None = None) -> tuple[str, datetime]:
    """Mint a new session for `email`; return (raw_token, expires_at). Only the
    token's HMAC is persisted — the raw token goes to the cookie and nowhere else."""
    email = normalize_email(email)
    raw = secrets.token_urlsafe(32)  # 256 bits
    expires_at = _now() + timedelta(seconds=Config.AUTH_SESSION_TTL_SECONDS)
    auth_repo.create_session(_hmac(raw), email, expires_at, user_agent=user_agent, ip=ip)
    return raw, expires_at


def current_session(request: Request) -> dict | None:
    """The live session for this request's cookie, or None. Never raises — a DB
    hiccup during validation denies rather than crashes the gate."""
    raw = request.cookies.get(Config.AUTH_COOKIE_NAME)
    if not raw:
        return None
    try:
        return auth_repo.get_session(_hmac(raw))
    except Exception:  # noqa: BLE001
        log.exception("Session validation failed")
        return None


def end_session(request: Request) -> None:
    """Revoke the current request's session (logout)."""
    raw = request.cookies.get(Config.AUTH_COOKIE_NAME)
    if not raw:
        return
    try:
        auth_repo.revoke_session(_hmac(raw))
    except Exception:  # noqa: BLE001
        log.exception("Session revoke failed")


# ── Cookie helpers ────────────────────────────────────────────────────────────
def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=Config.AUTH_COOKIE_NAME,
        value=raw_token,
        max_age=Config.AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        secure=Config.AUTH_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=Config.AUTH_COOKIE_NAME, path="/")
