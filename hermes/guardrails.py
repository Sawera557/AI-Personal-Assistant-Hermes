"""Guardrails — deterministic safety gates around the agent's tool calls.

Guardrail #1 (this module's first job): **confirm before write/send**. No
external send (email, WhatsApp, Zoom invite) or external data-write (Jira,
WATI contacts, a mutating `maton_request`) executes unless the user has been
shown exactly what will happen and approved it — *unless* the guardrail has
been turned off (globally or per-business, durably, or by the user saying
"stop asking me").

Why here and not just in the system prompt: a prompt instruction is advisory —
a jailbreak, a confused model, or a token-truncated turn can skip it. This gate
sits at the tool-dispatch chokepoint in `hermes.agent.stream_turn`, so the
guarantee holds regardless of what the model "decides".

How the confirmation works (a tool-agnostic generalization of the
`email_with_attachment` draft flow):

  1. The model calls a guarded tool WITHOUT a valid `approval_token`.
  2. `check()` returns REQUIRE_CONFIRMATION with a human summary and a token
     bound to a hash of the exact arguments. The loop skips dispatch and hands
     the summary + token back to the model as a tool_result.
  3. The model relays the summary; the user approves; the model re-calls the
     SAME tool with the SAME args plus `approval_token`.
  4. `check()` recomputes the hash from the re-sent args. Match -> allow and
     consume the token. Mismatch (args changed) -> re-block with a fresh token,
     so what is sent is byte-for-byte what was approved.

Config mirrors `hermes.models`: a DB-backed runtime toggle (Postgres
`hermes_settings`) with an in-process fallback when there's no database, and
`Config.GUARDRAIL_CONFIRM_WRITES` as the default. Scope is global or
per-business.

Fail-safe: any error resolving config or classifying a tool defaults to
*guarded*, never to *allowed*.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, NamedTuple

from .config import Config

log = logging.getLogger("hermes.guardrails")

# ---- Action classification -------------------------------------------------
# Which tools mutate the outside world, grouped by class. Read-only tools (and
# reads whose only write is an internal S3 cache — drive_read_file,
# wati_get_media) are absent on purpose: they are never guarded.
SEND = "send"                # external message/email egress
WRITE = "write"              # mutates a connected external system
DESTRUCTIVE = "destructive"  # deletes external/persistent state
INTERNAL = "internal"        # writes only to Hermes' own stores

# Static tool -> class map. `maton_request` is method-dependent and handled
# separately in `classify()`.
_TOOL_CLASS: dict[str, str] = {
    # SEND
    "send_email": SEND,
    "email_with_attachment": SEND,
    "zoom_send_invite": SEND,
    "wati_send_message": SEND,
    "wati_send_file": SEND,
    # WRITE
    "jira_create_issue": WRITE,
    "jira_assign_issue": WRITE,
    "wati_add_contact": WRITE,
    "wati_update_contact_attributes": WRITE,
    "wati_assign_operator": WRITE,
    # DESTRUCTIVE
    "delete_scheduled_task": DESTRUCTIVE,
    # INTERNAL
    "create_file": INTERNAL,
    "create_scheduled_task": INTERNAL,
    "update_scheduled_task": INTERNAL,
}

# Which classes this guardrail confirms for. Locked scope = external sends +
# external data writes. DESTRUCTIVE and INTERNAL are defined above but left out
# here; add them to flip those on.
GUARDED_CLASSES: frozenset[str] = frozenset({SEND, WRITE})

# Tools that run their own explicit prepare -> approve -> send flow (a Gmail
# draft held on a `draft_id`). Gating them again would double-confirm, so the
# generic gate defers to their built-in one.
_SELF_GATED: frozenset[str] = frozenset({"email_with_attachment"})

_WRITE_METHODS = {"POST", "PUT", "PATCH"}
_DELETE_METHODS = {"DELETE"}

# Tools that may carry an `approval_token` in their schema — the ones the generic
# gate can hold. Excludes self-gated tools (their own confirm/draft_id flow
# handles it); includes maton_request (guarded only for mutating methods, but the
# optional field is harmless on reads). `hermes.tools.build_tools` reads this.
APPROVAL_TOKEN_TOOLS: frozenset[str] = frozenset(
    ({n for n, c in _TOOL_CLASS.items() if c in GUARDED_CLASSES} - _SELF_GATED)
    | {"maton_request"}
)


def classify(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """The action class of a tool call, or None if it's read-only/unknown."""
    if tool_name == "maton_request":
        method = str((tool_input or {}).get("method", "GET")).upper()
        if method in _WRITE_METHODS:
            return WRITE
        if method in _DELETE_METHODS:
            return DESTRUCTIVE
        return None
    return _TOOL_CLASS.get(tool_name)


def _is_guarded(tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    if tool_name in _SELF_GATED:
        return False
    return classify(tool_name, tool_input) in GUARDED_CLASSES


# ---- Config: DB-backed runtime toggles, global + per-business --------------
# One setting key per guardrail; per-business override is "<key>.<slug>".
_CONFIRM_WRITES_KEY = "guardrail.confirm_writes"
_ISOLATION_KEY = "guardrail.business_isolation"

_lock = threading.Lock()
# Process-global fallback when there's no DB. Keyed by "<setting_key>::<scope>"
# where scope is a business slug or "__global__" — namespaced by setting_key so
# two guardrails' fallbacks never collide.
_overrides: dict[str, bool] = {}


def _db_backed() -> bool:
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return bool(os.getenv("DBRDS_HOST", "").strip())


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return None


def _scope_key(business: str | None) -> str | None:
    """Normalize a business scope to a slug, or None for global. 'all'/'' -> global."""
    if not business:
        return None
    slug = business.strip().lower()
    if slug in {"all", "all_businesses", "*"}:
        return None
    return slug


def _resolve_enabled(setting_key: str, default: bool, business: str | None) -> bool:
    """Generic guardrail on/off resolution shared by every guardrail.

    Precedence: per-business setting -> global setting -> `default`. Reads
    `hermes_settings` when DB-backed, else the in-process fallback. Fail-safe: on
    any lookup error return True (a guardrail is safe ON), never weaken the gate.
    """
    slug = _scope_key(business)
    try:
        if _db_backed():
            from .db import settings as db_settings

            if slug:
                per = _as_bool(db_settings.get(f"{setting_key}.{slug}"))
                if per is not None:
                    return per
            glob = _as_bool(db_settings.get(setting_key))
            if glob is not None:
                return glob
        else:
            with _lock:
                if slug and f"{setting_key}::{slug}" in _overrides:
                    return _overrides[f"{setting_key}::{slug}"]
                if f"{setting_key}::__global__" in _overrides:
                    return _overrides[f"{setting_key}::__global__"]
    except Exception:  # noqa: BLE001 — never let a config read weaken the gate
        log.warning("guardrail config read failed for %s; defaulting to on", setting_key, exc_info=True)
        return True
    return bool(default)


def _apply_setting(setting_key: str, enabled: bool, business: str | None) -> None:
    """Generic durable on/off write shared by every guardrail. `business=None`
    sets the global default; a slug sets a per-business override. Persists to
    `hermes_settings` when DB-backed, and always updates the in-process fallback."""
    slug = _scope_key(business)
    key = f"{setting_key}.{slug}" if slug else setting_key
    value = "on" if enabled else "off"
    if _db_backed():
        try:
            from .db import settings as db_settings

            db_settings.set(key, value)
        except Exception:  # noqa: BLE001 — still honor it in-process this run
            log.warning("guardrail config write failed for %s", key, exc_info=True)
    with _lock:
        _overrides[f"{setting_key}::{slug or '__global__'}"] = enabled


def confirm_writes_enabled(business: str | None = None) -> bool:
    """Is the confirm-before-write guardrail active for this scope?"""
    return _resolve_enabled(_CONFIRM_WRITES_KEY, bool(Config.GUARDRAIL_CONFIRM_WRITES), business)


def set_confirm_writes(enabled: bool, business: str | None = None) -> None:
    """Turn the confirm-before-write guardrail on/off, durably (global or per-business)."""
    _apply_setting(_CONFIRM_WRITES_KEY, enabled, business)


def business_isolation_enabled(business: str | None = None) -> bool:
    """Is the business-data-isolation guardrail active for this scope? When on, the
    Pinecone reader verifies each match's business and drops cross-business leaks."""
    return _resolve_enabled(_ISOLATION_KEY, bool(Config.GUARDRAIL_BUSINESS_ISOLATION), business)


def set_business_isolation(enabled: bool, business: str | None = None) -> None:
    """Turn the business-data-isolation guardrail on/off, durably (global or per-business)."""
    _apply_setting(_ISOLATION_KEY, enabled, business)


# ---- User-facing registry (drives the Settings UI) -------------------------
# One row per guardrail the user can toggle. The Settings tab renders this list,
# so a new guardrail appears automatically once it's added here (plus a branch in
# _guardrail_enabled / set_guardrail_enabled below). Keep `key` stable — it's the
# API contract used by GET/POST /api/settings/guardrails.
GUARDRAILS_META: list[dict[str, str]] = [
    {
        "key": "confirm_writes",
        "label": "Confirm before write/send",
        "description": (
            "Pause and ask for your confirmation before Hermes sends or writes anything "
            "externally — email, WhatsApp, a Zoom invite, a Jira issue, or any data write "
            "to a connected app. When off, those actions run without asking."
        ),
    },
    {
        "key": "business_isolation",
        "label": "Business data isolation",
        "description": (
            "Verify that knowledge-base results retrieved from Pinecone belong to the business "
            "in scope, and drop anything that doesn't — so one business's documents can never "
            "leak into another business's context. When off, results are trusted by namespace only."
        ),
    },
]

_GUARDRAIL_KEYS = {m["key"] for m in GUARDRAILS_META}


def _guardrail_enabled(key: str) -> bool:
    """Global on/off state for a registered guardrail."""
    if key == "confirm_writes":
        return confirm_writes_enabled(None)
    if key == "business_isolation":
        return business_isolation_enabled(None)
    raise KeyError(key)


def list_guardrails() -> list[dict[str, Any]]:
    """Every user-facing guardrail with its current global `enabled` state, for
    the Settings UI."""
    return [{**meta, "enabled": _guardrail_enabled(meta["key"])} for meta in GUARDRAILS_META]


def set_guardrail_enabled(key: str, enabled: bool, business: str | None = None) -> None:
    """Turn a registered guardrail on/off (global, or per-business when a slug is
    given). Raises KeyError for an unknown key."""
    if key not in _GUARDRAIL_KEYS:
        raise KeyError(key)
    if key == "confirm_writes":
        set_confirm_writes(enabled, business)
        return
    if key == "business_isolation":
        set_business_isolation(enabled, business)
        return
    raise KeyError(key)  # registered but no setter wired — a bug, fail loudly


# ---- Approval-token store (per session) ------------------------------------
_pending_lock = threading.Lock()
_pending: dict[str, set[str]] = {}
_MAX_PENDING_PER_SESSION = 64  # soft cap so a long session can't grow unbounded


def _salient_args(tool_input: dict[str, Any]) -> dict[str, Any]:
    """The arguments that define the action, minus the approval field itself, so
    the token binds to *what will happen* and re-sending the same call reproduces
    the same token."""
    return {k: v for k, v in (tool_input or {}).items() if k != "approval_token"}


def token_for(
    tool_name: str, tool_input: dict[str, Any], business: str | None
) -> str:
    """Deterministic token for a specific action: same tool + business + args ->
    same token; any change -> a different token."""
    payload = json.dumps(
        {
            "tool": tool_name,
            "business": _scope_key(business) or "all",
            "args": _salient_args(tool_input),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _issue(session_id: str, token: str) -> None:
    with _pending_lock:
        bucket = _pending.setdefault(session_id, set())
        if len(bucket) >= _MAX_PENDING_PER_SESSION:
            bucket.clear()
        bucket.add(token)


def _consume(session_id: str, token: str) -> bool:
    """True if `token` was pending for this session; removes it (single-use)."""
    with _pending_lock:
        bucket = _pending.get(session_id)
        if bucket and token in bucket:
            bucket.discard(token)
            return True
        return False


def clear_session(session_id: str) -> None:
    """Drop any pending approvals for a session (e.g. on reset)."""
    with _pending_lock:
        _pending.pop(session_id, None)


# ---- Human-readable action summary -----------------------------------------
def _summarize(tool_name: str, ti: dict[str, Any]) -> str:
    """One-line description of exactly what the tool call would do, for the user
    to approve."""
    if tool_name == "send_email":
        return f"Send an email to {ti.get('to', '?')} — subject \"{ti.get('subject', '')}\""
    if tool_name == "zoom_send_invite":
        return f"Email a Zoom meeting invite to {ti.get('to', '?')}"
    if tool_name == "wati_send_message":
        return f"Send a WhatsApp message to {ti.get('whatsapp_number', '?')}"
    if tool_name == "wati_send_file":
        return f"Send a WhatsApp file to {ti.get('whatsapp_number', '?')}"
    if tool_name == "jira_create_issue":
        return f"Create a Jira issue: \"{ti.get('summary', '')}\""
    if tool_name == "jira_assign_issue":
        return f"Assign Jira issue {ti.get('issue_key', '?')} to {ti.get('assignee', '?')}"
    if tool_name == "wati_add_contact":
        return f"Add a WhatsApp contact: {ti.get('whatsapp_number', '?')}"
    if tool_name == "wati_update_contact_attributes":
        return f"Update WhatsApp contact attributes for {ti.get('whatsapp_number', '?')}"
    if tool_name == "wati_assign_operator":
        return f"Assign operator {ti.get('email', '?')} to {ti.get('whatsapp_number', '?')}"
    if tool_name == "maton_request":
        method = str(ti.get("method", "GET")).upper()
        return f"{method} {ti.get('app', '?')} {ti.get('path', '')}".strip()
    return f"Run {tool_name} (a write/send action)"


# ---- The gate --------------------------------------------------------------
class Decision(NamedTuple):
    allow: bool
    summary: str
    approval_token: str | None


_ALLOWED = Decision(True, "", None)


def check(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    active_business: str | None = None,
    interactive: bool = True,
) -> Decision:
    """Decide whether a tool call may execute now.

    Returns ALLOW, or REQUIRE_CONFIRMATION carrying a summary and an
    approval_token the model must echo back (with unchanged args) to proceed.

    `interactive=False` (unattended/scheduled runs) defers to the scheduler's
    autonomy tiers — the gate no-ops so a run with no human never deadlocks.
    """
    ti = tool_input or {}
    if not _is_guarded(tool_name, ti):
        return _ALLOWED
    if not interactive:
        return _ALLOWED
    if not confirm_writes_enabled(active_business):
        return _ALLOWED

    expected = token_for(tool_name, ti, active_business)
    supplied = str(ti.get("approval_token") or "").strip()
    if supplied and supplied == expected and _consume(session_id, expected):
        return _ALLOWED

    _issue(session_id, expected)
    return Decision(False, _summarize(tool_name, ti), expected)
