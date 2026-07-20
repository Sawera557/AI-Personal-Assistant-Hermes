"""Persistence for passwordless email login: one-time codes + sessions.

Mirrors the rest of the DB layer (thin functions over ``cursor()``), and — like
``hermes.models`` and ``hermes.memory`` — falls back to a process-local in-memory
store when ``HERMES_DISABLE_DB`` is set, so the auth flow works in offline tests
and local dev without a database.

Only hashes are ever stored here; see 0016_auth.sql. The plaintext OTP and the
raw session token never touch this module — callers pass the HMACs.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from .connection import cursor


def _disabled() -> bool:
    return os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── In-memory fallback (HERMES_DISABLE_DB) ────────────────────────────────────
_MEM_LOCK = threading.Lock()
_MEM_CODES: list[dict] = []
_MEM_SESSIONS: list[dict] = []


# ── Login codes ───────────────────────────────────────────────────────────────
def create_login_code(email: str, code_hash: str, expires_at: datetime) -> None:
    """Store a fresh code hash and drop any prior live codes for this email, so
    only the most recent code can ever be redeemed."""
    email = email.lower()
    if _disabled():
        with _MEM_LOCK:
            for c in _MEM_CODES:
                if c["email"] == email and c["consumed_at"] is None:
                    c["consumed_at"] = _now()  # supersede older codes
            _MEM_CODES.append({
                "id": f"mem-{len(_MEM_CODES)}-{code_hash[:8]}",
                "email": email,
                "code_hash": code_hash,
                "expires_at": expires_at,
                "attempts": 0,
                "consumed_at": None,
                "created_at": _now(),
            })
        return
    with cursor() as cur:
        cur.execute(
            "UPDATE auth_login_codes SET consumed_at = now() "
            "WHERE email = %s AND consumed_at IS NULL;",
            (email,),
        )
        cur.execute(
            "INSERT INTO auth_login_codes (email, code_hash, expires_at) "
            "VALUES (%s, %s, %s);",
            (email, code_hash, expires_at),
        )


def count_recent_codes(email: str, since: datetime) -> int:
    """How many codes were requested for this email at/after `since` — the send
    rate-limit input."""
    email = email.lower()
    if _disabled():
        with _MEM_LOCK:
            return sum(1 for c in _MEM_CODES if c["email"] == email and c["created_at"] >= since)
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT count(*) FROM auth_login_codes WHERE email = %s AND created_at >= %s;",
            (email, since),
        )
        return int(cur.fetchone()[0])


def get_active_code(email: str) -> dict | None:
    """The newest un-consumed, un-expired code for this email, or None."""
    email = email.lower()
    if _disabled():
        with _MEM_LOCK:
            live = [
                c for c in _MEM_CODES
                if c["email"] == email and c["consumed_at"] is None and c["expires_at"] > _now()
            ]
            if not live:
                return None
            c = max(live, key=lambda r: r["created_at"])
            return {"id": c["id"], "code_hash": c["code_hash"], "attempts": c["attempts"]}
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, code_hash, attempts FROM auth_login_codes "
            "WHERE email = %s AND consumed_at IS NULL AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1;",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "code_hash": row[1], "attempts": row[2]}


def bump_code_attempt(code_id) -> int:
    """Increment a code's wrong-try counter and return the new count."""
    if _disabled():
        with _MEM_LOCK:
            for c in _MEM_CODES:
                if c["id"] == code_id:
                    c["attempts"] += 1
                    return c["attempts"]
        return 0
    with cursor() as cur:
        cur.execute(
            "UPDATE auth_login_codes SET attempts = attempts + 1 "
            "WHERE id = %s RETURNING attempts;",
            (code_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def consume_code(code_id) -> None:
    """Mark a code redeemed (single-use)."""
    if _disabled():
        with _MEM_LOCK:
            for c in _MEM_CODES:
                if c["id"] == code_id:
                    c["consumed_at"] = _now()
        return
    with cursor() as cur:
        cur.execute(
            "UPDATE auth_login_codes SET consumed_at = now() WHERE id = %s;",
            (code_id,),
        )


# ── Sessions ──────────────────────────────────────────────────────────────────
def create_session(
    token_hash: str, email: str, expires_at: datetime,
    user_agent: str | None = None, ip: str | None = None,
) -> None:
    email = email.lower()
    if _disabled():
        with _MEM_LOCK:
            _MEM_SESSIONS.append({
                "token_hash": token_hash,
                "email": email,
                "expires_at": expires_at,
                "revoked_at": None,
                "last_seen_at": _now(),
            })
        return
    with cursor() as cur:
        cur.execute(
            "INSERT INTO auth_sessions (token_hash, email, expires_at, user_agent, ip) "
            "VALUES (%s, %s, %s, %s, %s);",
            (token_hash, email, expires_at, (user_agent or "")[:500], (ip or "")[:100]),
        )


def get_session(token_hash: str) -> dict | None:
    """A live session (not revoked, not expired) for this token hash, or None.

    Refreshes last_seen_at at most every ~5 min to avoid a write on every
    request while still tracking recency.
    """
    if _disabled():
        with _MEM_LOCK:
            for s in _MEM_SESSIONS:
                if s["token_hash"] == token_hash and s["revoked_at"] is None and s["expires_at"] > _now():
                    s["last_seen_at"] = _now()
                    return {"email": s["email"], "expires_at": s["expires_at"]}
        return None
    with cursor() as cur:
        cur.execute(
            "SELECT email, expires_at, last_seen_at FROM auth_sessions "
            "WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now();",
            (token_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        email, expires_at, last_seen = row[0], row[1], row[2]
        if last_seen is None or (_now() - last_seen).total_seconds() > 300:
            cur.execute(
                "UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = %s;",
                (token_hash,),
            )
        return {"email": email, "expires_at": expires_at}


def revoke_session(token_hash: str) -> None:
    if _disabled():
        with _MEM_LOCK:
            for s in _MEM_SESSIONS:
                if s["token_hash"] == token_hash:
                    s["revoked_at"] = _now()
        return
    with cursor() as cur:
        cur.execute(
            "UPDATE auth_sessions SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL;",
            (token_hash,),
        )
