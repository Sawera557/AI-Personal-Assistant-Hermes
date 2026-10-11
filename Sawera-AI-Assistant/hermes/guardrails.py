"""Guardrails — deterministic safety gates around the agent's tool calls.

Guardrail #1 (this module's first job): **confirm before write/send**. No
external send (email, WhatsApp, Zoom invite), external data-write (Jira,
WATI contacts, a mutating `maton_request`) or delete (a `maton_request`
DELETE, `delete_scheduled_task`) executes unless the user has been shown
exactly what will happen and approved it — *unless* the guardrail has been
turned off (globally or per-business, durably, or by the user saying "stop
asking me").

Two kinds of action are confirmed even when that switch is off, because they
raise Hermes' authority rather than use it: turning a guardrail OFF
(`set_guardrail`), and granting a scheduled task FULL autonomy — a standing
authorization to send and modify unattended (`create_scheduled_task` /
`update_scheduled_task`). A model steered by a prompt injection in an email
or WhatsApp message must not be able to do either on its own.

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

Two more gates refuse outright (no approval can unlock them) and apply to
unattended runs as well: Guardrail #3 blocks a send or external write that
carries a secret or card number; Guardrail #4 caps sends and deletes per turn.
(Guardrail #2, business isolation, lives in the retrieval path, not here.)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
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
SAFETY = "safety"            # weakens a safety guardrail (set_guardrail ... off)
AUTHORIZE = "authorize"      # grants a scheduled task standing FULL autonomy

# Static tool -> class map. `maton_request`, `set_guardrail` and the two
# scheduled-task authoring tools are argument-dependent and handled separately
# in `classify()`; their entries here are the class of their harmless form.
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

# Which classes this guardrail confirms for. DESTRUCTIVE used to be left out,
# so a `maton_request` DELETE (a Drive file, a calendar event) and
# `delete_scheduled_task` ran with no confirmation at all while the system
# prompt told the model "a safety guardrail enforces this" (B1/B2 in
# docs/architecture/TURN_COST_PLAN.md). INTERNAL stays out: it only writes to
# Hermes' own stores (a generated file, a readonly scheduled task).
GUARDED_CLASSES: frozenset[str] = frozenset({SEND, WRITE, DESTRUCTIVE, SAFETY, AUTHORIZE})

# Classes confirmed even when the confirm-before-write switch is OFF. The switch
# means "don't ask me before sends and writes"; it was never consent to let the
# model switch other guardrails off (B3) or hand a scheduled task standing
# authority to send unattended (B14) without the user saying so.
_ALWAYS_CONFIRM: frozenset[str] = frozenset({SAFETY, AUTHORIZE})

# Tools that run their own explicit prepare -> approve -> send flow (a Gmail
# draft held on a `draft_id`). Gating them again would double-confirm, so the
# generic gate defers to the draft gate in `check()` (B4).
_SELF_GATED: frozenset[str] = frozenset({"email_with_attachment"})

_WRITE_METHODS = {"POST", "PUT", "PATCH"}
_DELETE_METHODS = {"DELETE"}

# Scheduled-task fields that change what a FULL-autonomy task will do the next
# time it fires unattended. Editing any of them on such a task is a new
# standing authorization, not a cosmetic edit.
_AUTHORITY_FIELDS = ("prompt", "allowed_apps", "business")

# Tools that may carry an `approval_token` in their schema — the ones the generic
# gate can hold. Excludes self-gated tools (they get their own draft-token field,
# see DRAFT_TOKEN_TOOLS); includes the argument-dependent tools (guarded only in
# some forms, but the optional field is harmless otherwise). `build_tools` reads
# this.
APPROVAL_TOKEN_TOOLS: frozenset[str] = frozenset(
    ({n for n, c in _TOOL_CLASS.items() if c in GUARDED_CLASSES} - _SELF_GATED)
    | {"maton_request", "set_guardrail", "create_scheduled_task", "update_scheduled_task"}
)

# Self-gated tools whose SEND step must carry the approval_token handed out with
# the prepared draft (B4). `build_tools` gives them a draft-specific field.
DRAFT_TOKEN_TOOLS: frozenset[str] = _SELF_GATED


def _is_full_autonomy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "full"


def _existing_task(task_id: Any) -> dict[str, Any]:
    """The scheduled task being edited, as far as the gate needs it.

    Fail-safe: an unreadable task reads as a PAUSED FULL-autonomy task, so a DB
    hiccup asks for one confirmation too many rather than letting a standing
    send authority be rewritten or resumed unconfirmed. A task that doesn't
    exist reads as empty — the tool itself returns a 404."""
    try:
        from .db import scheduled_tasks as tasks_db

        existing = tasks_db.get_task(str(task_id or "").strip())
    except Exception:  # noqa: BLE001 — never let a lookup weaken the gate
        log.warning("scheduled-task lookup failed; treating as full autonomy", exc_info=True)
        return {"autonomy": "full", "enabled": False}
    return existing if isinstance(existing, dict) else {}


def _resumes(ti: dict[str, Any]) -> bool:
    """Whether an update switches the task ON, read exactly as
    tools._update_scheduled_task parses `enabled`."""
    if "enabled" not in ti:
        return False
    en = ti["enabled"]
    return en if isinstance(en, bool) else str(en).strip().lower() not in ("false", "0", "no", "off", "")


def classify(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """The action class of a tool call, or None if it's read-only/unknown."""
    ti = tool_input or {}
    if tool_name == "maton_request":
        method = str(ti.get("method", "GET")).upper()
        if method in _WRITE_METHODS:
            return WRITE
        if method in _DELETE_METHODS:
            return DESTRUCTIVE
        return None
    if tool_name == "set_guardrail":
        # Same truthiness `tools._set_guardrail` applies, so the gate and the
        # dispatcher always agree on which way the switch is about to go.
        # Turning a guardrail ON only makes Hermes safer and is never held.
        return None if bool(ti.get("enabled")) else SAFETY
    if tool_name == "create_scheduled_task":
        return AUTHORIZE if _is_full_autonomy(ti.get("autonomy")) else INTERNAL
    if tool_name == "update_scheduled_task":
        # Read `autonomy` with the dispatcher's own truthiness
        # (tools._update_scheduled_task: `if tool_input.get("autonomy")`). A
        # present-but-empty autonomy ("", False, 0) is IGNORED by the update, so
        # it must not decide the class either — or {"autonomy": "", "prompt":
        # ...} rewrites a FULL task's instructions unconfirmed.
        autonomy = ti.get("autonomy")
        if autonomy and _is_full_autonomy(autonomy):
            return AUTHORIZE  # raising to full is a grant
        lowering = isinstance(autonomy, str) and autonomy.strip().lower() in ("readonly", "draft")
        if not lowering:
            existing = _existing_task(ti.get("task_id"))
            if _is_full_autonomy(existing.get("autonomy")):
                if any(field in ti for field in _AUTHORITY_FIELDS):
                    return AUTHORIZE  # rewriting what a full task will do
                if _resumes(ti) and existing.get("enabled") is False:
                    return AUTHORIZE  # switching a paused full task back on
        return INTERNAL
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


_SECRET_EGRESS_KEY = "guardrail.secret_egress"
_SEND_LIMIT_KEY = "guardrail.send_limit"


def secret_egress_enabled(business: str | None = None) -> bool:
    """Is the block-secrets-in-outbound-actions guardrail active for this scope?"""
    return _resolve_enabled(_SECRET_EGRESS_KEY, bool(Config.GUARDRAIL_SECRET_EGRESS), business)


def send_limit_enabled(business: str | None = None) -> bool:
    """Is the per-turn cap on sends and deletes active for this scope?"""
    return _resolve_enabled(_SEND_LIMIT_KEY, bool(Config.GUARDRAIL_SEND_LIMIT), business)


# ---- Guardrail #3: secrets never leave in a send or external write ----------
# High-confidence shapes only — the block is a refusal, not a question, so a
# false positive costs the user a Settings trip. No "password: ..." heuristic:
# it fires on ordinary prose ("password: please reset it").
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("an API secret key", re.compile(r"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_\-]{24,}")),
    ("a Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{30,}=*")),
)
# A card number as people write one: 13-19 digits, optionally grouped by single
# spaces or dashes, starting with a real card-network prefix, passing Luhn.
_CARD = re.compile(r"\b(?:3[47]|4|5[1-5]|2[2-7]|6(?:011|5))(?:[ -]?\d){11,17}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _strings(value: Any) -> list[str]:
    """Every string inside a tool input, however deeply nested."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k != "approval_token" for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def find_secret(tool_input: dict[str, Any] | None) -> str | None:
    """What kind of secret this tool input carries ("an AWS access key"), or None."""
    for text in _strings(tool_input or {}):
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                return kind
        for m in _CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                return "a payment card number"
    return None


# ---- Guardrail #4: a ceiling on sends and deletes per turn -------------------
# session_id -> (turn, actions allowed so far in that turn). One entry per
# session, so the map stays as small as the session count.
_sends_lock = threading.Lock()
_sends: dict[str, tuple[int, int]] = {}


def _limited(tool_name: str, ti: dict[str, Any]) -> bool:
    """Whether this call counts toward the per-turn cap: a send or a delete that
    will actually execute (an email_with_attachment PREPARE only saves a draft)."""
    if tool_name in _SELF_GATED and not ti.get("confirm"):
        return False
    return classify(tool_name, ti) in (SEND, DESTRUCTIVE)


def _sends_so_far(session_id: str, turn: int) -> int:
    with _sends_lock:
        t, n = _sends.get(session_id, (turn, 0))
        return n if t == turn else 0


def _count_send(session_id: str, turn: int) -> None:
    with _sends_lock:
        t, n = _sends.get(session_id, (turn, 0))
        _sends[session_id] = (turn, n + 1 if t == turn else 1)


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
            "Pause and ask for your confirmation before Hermes sends, writes or deletes "
            "anything — email, WhatsApp, a Zoom invite, a Jira issue, any data write or "
            "delete in a connected app, or deleting a scheduled task. When off, those "
            "actions run without asking. Turning a guardrail off, or giving a scheduled "
            "task full autonomy, always asks first."
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
    {
        "key": "secret_egress",
        "label": "Block secrets in outbound actions",
        "description": (
            "Refuse any email, message or external write that contains an API key, access token, "
            "private key or payment card number — in chat and in scheduled tasks. Stops a "
            "credential from leaving by mistake or through an instruction hidden in an email."
        ),
    },
    {
        "key": "send_limit",
        "label": "Limit sends and deletes per turn",
        "description": (
            f"Allow at most {Config.GUARDRAIL_MAX_SENDS_PER_TURN} sends or deletes in a single turn, "
            "including scheduled tasks. Stops a runaway loop or an injected instruction from "
            "mass-emailing or mass-deleting. Split bigger batches across turns."
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
    if key == "secret_egress":
        return secret_egress_enabled(None)
    if key == "send_limit":
        return send_limit_enabled(None)
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
    if key == "secret_egress":
        _apply_setting(_SECRET_EGRESS_KEY, enabled, business)
        return
    if key == "send_limit":
        _apply_setting(_SEND_LIMIT_KEY, enabled, business)
        return
    raise KeyError(key)  # registered but no setter wired — a bug, fail loudly


# ---- Approval-token store (per session) ------------------------------------
_secret_cache: bytes | None = None
_pending_lock = threading.Lock()
# Per-session token buckets. Dicts used as insertion-ordered sets, so the soft
# cap below really does evict the OLDEST token — `set.pop()` removed an
# arbitrary one, which could invalidate the approval the user was about to
# give (B11).
_pending: dict[str, dict[str, None]] = {}
#: Approvals already acted on — one approval, one execution.
_spent: dict[str, dict[str, None]] = {}
_MAX_PENDING_PER_SESSION = 64  # soft cap so a long session can't grow unbounded


def _salient_args(tool_input: dict[str, Any]) -> dict[str, Any]:
    """The arguments that define the action, minus the approval field itself, so
    the token binds to *what will happen* and re-sending the same call reproduces
    the same token."""
    return {k: v for k, v in (tool_input or {}).items() if k != "approval_token"}


def _secret() -> bytes:
    """Key for the approval HMAC.

    The token used to be a bare SHA-256 of the arguments, which anyone (the
    model included) could recompute offline. That was survivable only because a
    token also had to be *issued* in-session to be usable. Binding the issuing
    turn into the token removes that crutch — a plain hash would let the model
    mint a token claiming to come from an earlier turn — so it is keyed now.

    Reuses AUTH_SESSION_SECRET / the cron secret if present. With none of them
    set, a per-process random key still secures a single-process deployment; it
    only degrades on multi-process/serverless, where the warning says so.
    """
    global _secret_cache
    if _secret_cache is None:
        configured = (
            os.getenv("HERMES_GUARDRAIL_SECRET", "").strip()
            or os.getenv("AUTH_SESSION_SECRET", "").strip()
            or os.getenv("SCHEDULER_SECRET", "").strip()
            or os.getenv("CRON_SECRET", "").strip()
        )
        if not configured:
            configured = secrets.token_hex(32)
            log.warning(
                "No HERMES_GUARDRAIL_SECRET/AUTH_SESSION_SECRET set — approval "
                "tokens are keyed per-process. On a multi-process or serverless "
                "deployment a confirmation issued by one worker cannot be "
                "approved on another; set a secret to fix that."
            )
        _secret_cache = configured.encode("utf-8")
    return _secret_cache


def token_for(
    tool_name: str,
    tool_input: dict[str, Any],
    business: str | None,
    turn: int = 0,
    session_id: str | None = None,
) -> str:
    """Deterministic token for a specific action IN a specific turn of a
    specific conversation.

    Same tool + business + args + turn + conversation -> same token; any change
    -> a different token. The `turn` prefix is carried in clear so the verifier
    knows which turn to check the signature against; it is covered by the
    signature, so it cannot be edited to claim an earlier turn. The
    conversation is signed in too: a token approved (and spent) in one chat
    used to be replayable in another chat of the same business, where the
    per-session `_spent` record can't see it.
    """
    payload = json.dumps(
        {
            "tool": tool_name,
            "business": _scope_key(business) or "all",
            "args": _salient_args(tool_input),
            "turn": int(turn),
            "session": session_id or "",
        },
        sort_keys=True,
        default=str,
    )
    digest = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{int(turn)}.{digest[:16]}"


def _turn_of(token: str) -> int | None:
    """The turn a token claims to have been issued in, or None if malformed."""
    head, _, _rest = (token or "").partition(".")
    try:
        return int(head)
    except ValueError:
        return None


def _add_bounded(bucket: dict[str, None], token: str) -> None:
    """Add `token`, evicting the OLDEST entry once the bucket is full. This used
    to clear() the whole bucket, which silently invalidated every other approval
    the user had in flight. Caller holds `_pending_lock`."""
    bucket.pop(token, None)  # re-adding refreshes its position
    while len(bucket) >= _MAX_PENDING_PER_SESSION:
        bucket.pop(next(iter(bucket)))
    bucket[token] = None


def _issue(session_id: str, token: str) -> None:
    with _pending_lock:
        _add_bounded(_pending.setdefault(session_id, {}), token)


def _already_spent(session_id: str, token: str) -> bool:
    with _pending_lock:
        return token in _spent.get(session_id, {})


def _mark_spent(session_id: str, token: str) -> None:
    """Remember that this approval has been acted on, so it can't run twice."""
    with _pending_lock:
        _add_bounded(_spent.setdefault(session_id, {}), token)


def _claim(session_id: str, token: str) -> bool:
    """Spend `token` for this session: True the first time, False ever after.

    One locked check-and-set. It used to be a check, then a separate mark —
    two concurrent requests carrying the same approval (a voice relay fires
    several at once) could both pass the check and both run the action."""
    with _pending_lock:
        spent = _spent.setdefault(session_id, {})
        if token in spent:
            return False
        _add_bounded(spent, token)
        bucket = _pending.get(session_id)
        if bucket:
            bucket.pop(token, None)
        return True


def _consume(session_id: str, token: str) -> bool:
    """True if `token` was pending for this session; removes it (single-use)."""
    with _pending_lock:
        bucket = _pending.get(session_id)
        if bucket and token in bucket:
            del bucket[token]
            return True
        return False


def clear_session(session_id: str) -> None:
    """Drop any pending approvals for a session (e.g. on reset)."""
    with _pending_lock:
        _pending.pop(session_id, None)
        _spent.pop(session_id, None)
    with _sends_lock:
        _sends.pop(session_id, None)


# ---- Human-readable action summary -----------------------------------------
# The summary is what the user approves: since B6 it is appended, verbatim, to
# the reply that asks. So it must show every argument that decides WHO gets
# something and WHAT they get — a summary that said "Send an email to the
# client" while the call also cc'd a stranger, or a task approval that showed
# 160 characters of a longer instruction, let the user approve something they
# never saw.
def _clip(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _addresses(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "?"
    return str(value or "?")


def _json_clip(value: Any, limit: int = 300) -> str:
    try:
        return _clip(json.dumps(value, default=str, sort_keys=True), limit)
    except (TypeError, ValueError):
        return _clip(value, limit)


def _schedule_line(ti: dict[str, Any]) -> str:
    parts = [str(ti.get("frequency") or "daily")]
    if ti.get("times"):
        parts.append("at " + ", ".join(str(t) for t in ti["times"]))
    if ti.get("days_of_week"):
        parts.append("on days " + ", ".join(str(d) for d in ti["days_of_week"]))
    return " ".join(parts)


def _summarize(tool_name: str, ti: dict[str, Any]) -> str:
    """One-line description of exactly what the tool call would do, for the user
    to approve."""
    if tool_name == "send_email":
        cc = f", cc {_addresses(ti['cc'])}" if ti.get("cc") else ""
        return (
            f"Send an email to {_addresses(ti.get('to'))}{cc} — subject \"{ti.get('subject', '')}\" "
            f"— \"{_clip(ti.get('body'), 300)}\""
        )
    if tool_name == "zoom_send_invite":
        note = f" with the note \"{_clip(ti.get('note'), 200)}\"" if ti.get("note") else ""
        return f"Email the invite for Zoom meeting {ti.get('meeting_id', '?')} to {_addresses(ti.get('to'))}{note}"
    if tool_name == "wati_send_message":
        return (
            f"Send a WhatsApp message to {ti.get('whatsapp_number', '?')}: "
            f"\"{_clip(ti.get('message_text'), 300)}\""
        )
    if tool_name == "wati_send_file":
        source = ti.get("attachment_id") or ti.get("file_url") or "?"
        caption = f" with the caption \"{_clip(ti.get('caption'), 200)}\"" if ti.get("caption") else ""
        return f"Send the WhatsApp file {source} to {ti.get('whatsapp_number', '?')}{caption}"
    if tool_name == "jira_create_issue":
        who = f", assigned to {ti['assignee_email']}" if ti.get("assignee_email") else ""
        return f"Create a Jira issue in {ti.get('project_key', '?')}: \"{ti.get('summary', '')}\"{who}"
    if tool_name == "jira_assign_issue":
        # The schema field is `assignee_email` (hermes/tools.py); reading
        # `assignee` showed the user "?" as the person being assigned (B12).
        who = ti.get("assignee_email") or ti.get("assignee") or "?"
        return f"Assign Jira issue {ti.get('issue_key', '?')} to {who}"
    if tool_name == "wati_add_contact":
        name = f" ({ti['name']})" if ti.get("name") else ""
        return f"Add a WhatsApp contact: {ti.get('whatsapp_number', '?')}{name}"
    if tool_name == "wati_update_contact_attributes":
        return (
            f"Update WhatsApp contact {ti.get('whatsapp_number', '?')}: "
            f"{_json_clip(ti.get('custom_params'), 200)}"
        )
    if tool_name == "wati_assign_operator":
        return f"Assign operator {ti.get('email', '?')} to {ti.get('whatsapp_number', '?')}"
    if tool_name == "maton_request":
        method = str(ti.get("method", "GET")).upper()
        line = f"{method} {ti.get('app', '?')} {ti.get('path', '')}".strip()
        if ti.get("query"):
            line += f" ?{_json_clip(ti['query'], 150)}"
        if ti.get("body") not in (None, "", {}, []):
            line += f" with {_json_clip(ti['body'], 300)}"
        return f"DELETE (irreversible): {line}" if method in _DELETE_METHODS else line
    if tool_name == "delete_scheduled_task":
        return f"Permanently delete scheduled task {ti.get('task_id', '?')} and its run history"
    if tool_name == "set_guardrail":
        label = next(
            (m["label"] for m in GUARDRAILS_META if m["key"] == ti.get("guardrail")),
            str(ti.get("guardrail") or "?"),
        )
        scope = (str(ti.get("business") or "").strip() or "all businesses")
        return f"Turn OFF the \"{label}\" guardrail for {scope}"
    if tool_name == "create_scheduled_task":
        return (
            f"Create scheduled task \"{ti.get('name', '?')}\" with FULL autonomy — it will send "
            "and modify things on its own, unattended. Business: "
            f"{ti.get('business') or 'the current one'}. Apps: "
            f"{_addresses(ti.get('allowed_apps')) if ti.get('allowed_apps') else 'all connected apps'}. "
            f"Runs {_schedule_line(ti)}. Instruction: \"{_clip(ti.get('prompt'), 1500)}\""
        )
    if tool_name == "update_scheduled_task":
        eff = ti.get("_effective") if isinstance(ti.get("_effective"), dict) else {}
        state = (
            f"Business: {eff.get('business') or 'all'}. Apps: "
            f"{_addresses(eff.get('allowed_apps')) if eff.get('allowed_apps') else 'all connected apps'}. "
            f"Instruction: \"{_clip(eff.get('prompt'), 1500)}\""
            if eff else ""
        )
        changed = ", ".join(k for k in ("autonomy", *_AUTHORITY_FIELDS, "enabled") if k in ti) or "settings"
        return (
            f"Change scheduled task {ti.get('task_id', '?')} ({changed}) — after this it runs "
            f"with FULL autonomy and will send and modify things on its own, unattended. {state}"
        ).strip()
    if tool_name == "email_with_attachment":
        cc = f", cc {_addresses(ti['cc'])}" if ti.get("cc") else ""
        return (
            f"Send the prepared email draft to {_addresses(ti.get('to'))}{cc} — "
            f"subject \"{ti.get('subject', '')}\""
        )
    return f"Run {tool_name} (a write/send action)"


# ---- What a token binds ------------------------------------------------------
def _effective_task(ti: dict[str, Any]) -> dict[str, Any]:
    """What a scheduled task will be AFTER this update: the stored row's
    authority fields with the call's own changes applied.

    Signed into an AUTHORIZE token for update_scheduled_task. Without it, a
    grant of FULL autonomy bound only {task_id, autonomy}: the task's prompt
    could be rewritten (an unconfirmed INTERNAL edit while it was still
    readonly) between the user approving "raise t-1 to full" and the token
    being used, and the approval then applied to an instruction the user never
    saw. Now any change to the row changes the signature, and the old token
    stops working."""
    existing = _existing_task(ti.get("task_id"))
    eff = {
        "prompt": existing.get("prompt"),
        "allowed_apps": existing.get("allowed_apps") or [],
        "business": existing.get("business") or existing.get("entity_slug"),
        "autonomy": existing.get("autonomy"),
    }
    if ti.get("prompt"):
        eff["prompt"] = ti["prompt"]
    if "allowed_apps" in ti:
        eff["allowed_apps"] = ti.get("allowed_apps") or []
    if "business" in ti:
        eff["business"] = ti.get("business")
    if ti.get("autonomy"):
        eff["autonomy"] = ti["autonomy"]
    return eff


def _bound_args(tool_name: str, ti: dict[str, Any], action_class: str | None) -> dict[str, Any]:
    """The arguments a token for this call is signed over (and summarised from)."""
    args = _salient_args(ti)
    if tool_name == "update_scheduled_task" and action_class == AUTHORIZE:
        args["_effective"] = _effective_task(ti)
    return args


# ---- The gate --------------------------------------------------------------
class Decision(NamedTuple):
    allow: bool
    summary: str
    approval_token: str | None
    #: "confirm"     — show the user this summary and ask; re-call on a later turn.
    #: "awaiting"    — already asked this turn; the model must STOP and wait for
    #:                 the user rather than burning tool iterations retrying.
    #: "spent"       — the approval was already used once; held again, fresh.
    #: "needs_draft" — an email send with no prepared, approved draft behind it.
    reason: str = "confirm"


_ALLOWED = Decision(True, "", None, "allowed")


def _confirm_scope_enabled(active_business: str | None, ti: dict[str, Any]) -> bool:
    """Whether confirm-before-write applies to this call.

    A call can name the business whose account it uses (`business`, the "All
    Businesses" argument). The gate used to consult only the conversation's
    scope, so with confirmations off globally but ON for one business, a call
    from an "All Businesses" chat that targeted that business skipped the
    business's own setting. Both scopes are consulted now, strictest wins."""
    target = ti.get("business")
    if isinstance(target, str) and target.strip() and _scope_key(target) != _scope_key(active_business):
        return confirm_writes_enabled(active_business) or confirm_writes_enabled(target)
    return confirm_writes_enabled(active_business)


def check(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    active_business: str | None = None,
    interactive: bool = True,
    turn: int | None = None,
    approved_tokens: frozenset[str] | set[str] | None = None,
) -> Decision:
    """Decide whether a tool call may execute now.

    Returns ALLOW, or REQUIRE_CONFIRMATION carrying a summary and an
    approval_token the model must echo back (with unchanged args) to proceed.

    `turn` is a monotonically increasing counter for the conversation (the agent
    passes the number of user messages so far). An approval issued during turn N
    is only honoured from turn N+1 — i.e. after the user has actually spoken
    again. Pass None only from callers with no conversational turn (they keep
    the legacy same-turn behaviour).

    `approved_tokens` is the set of tokens THIS turn's user message approves:
    the ones the previous reply held and showed the user, when the message is
    an explicit approval of them (hermes.agent; `explicit_approval`), else
    empty. "The user has spoken since" is not "the user agreed to THIS": a
    token that survives in history (the in-memory store replays tool results;
    a model can copy a token into its reply text) used to run on any later
    message — after a "no", or on a "yes" to something else entirely. Any other
    token, however validly signed, is not honoured; the action is held again
    with a fresh one. None (callers that can't tell) keeps the turn-only rule.

    `interactive=False` (unattended/scheduled runs) defers to the scheduler's
    autonomy tiers — the gate no-ops so a run with no human never deadlocks.
    Unattended runs are never offered the SAFETY/AUTHORIZE tools at all
    (hermes.tools.build_tools / write_blocked_reason).

    Two refusals ("blocked", no token — nothing to approve) come first and hold
    for unattended runs too: a send or external write carrying a secret
    (Guardrail #3), and a send/delete past the per-turn cap (Guardrail #4).
    """
    ti = tool_input or {}
    blocked = _refusal(session_id, tool_name, ti, active_business, turn)
    if blocked:
        return blocked
    decision = _gate(session_id, tool_name, ti, active_business, interactive, turn, approved_tokens)
    if decision.allow and turn is not None and _limited(tool_name, ti):
        _count_send(session_id, int(turn))
    return decision


def _refusal(
    session_id: str,
    tool_name: str,
    ti: dict[str, Any],
    active_business: str | None,
    turn: int | None,
) -> Decision | None:
    """Guardrails #3 and #4: actions refused outright, before any confirmation."""
    try:
        action_class = SEND if tool_name in _SELF_GATED else classify(tool_name, ti)
        target = ti.get("business") if isinstance(ti.get("business"), str) else None
        if action_class in (SEND, WRITE) and (
            secret_egress_enabled(active_business) or (target and secret_egress_enabled(target))
        ):
            kind = find_secret(ti)
            if kind:
                return Decision(False, (
                    f"Blocked: this {tool_name} call contains what looks like {kind}. Secrets and "
                    "card numbers are never sent or written to outside apps. Remove it and try "
                    "again, or the user can switch off 'Block secrets in outbound actions' in Settings."
                ), None, "blocked")
        if turn is not None and _limited(tool_name, ti) and send_limit_enabled(active_business):
            cap = max(0, int(Config.GUARDRAIL_MAX_SENDS_PER_TURN))
            if _sends_so_far(session_id, int(turn)) >= cap:
                return Decision(False, (
                    f"Blocked: {cap} sends or deletes have already run in this turn, the most "
                    "allowed. Stop here, tell the user what was done, and continue the rest on "
                    "their next message — or they can raise the limit or switch off 'Limit sends "
                    "and deletes per turn' in Settings."
                ), None, "blocked")
    except Exception:  # noqa: BLE001 — fail-safe: a broken check refuses, never allows
        log.warning("guardrail refusal check failed for %s; blocking", tool_name, exc_info=True)
        return Decision(False, "Blocked: the safety check for this action failed. Try again.", None, "blocked")
    return None


def _gate(
    session_id: str,
    tool_name: str,
    ti: dict[str, Any],
    active_business: str | None,
    interactive: bool,
    turn: int | None,
    approved_tokens: frozenset[str] | set[str] | None,
) -> Decision:
    """The confirmation half of `check()` (Guardrail #1)."""
    if tool_name in _SELF_GATED:
        return _check_draft_send(
            session_id, tool_name, ti, active_business, interactive, turn, approved_tokens,
        )
    action_class = classify(tool_name, ti)
    if action_class not in GUARDED_CLASSES:
        return _ALLOWED
    if not interactive:
        return _ALLOWED
    if action_class not in _ALWAYS_CONFIRM and not _confirm_scope_enabled(active_business, ti):
        return _ALLOWED

    return _check_token(
        session_id, tool_name, ti, active_business, turn, approved_tokens,
        _bound_args(tool_name, ti, action_class),
    )


def _honour(
    session_id: str,
    supplied: str,
    issued_turn: int,
    turn: int | None,
    approved_tokens: frozenset[str] | set[str] | None,
) -> str:
    """What to do with a correctly signed token: "allow", "awaiting" (minted
    this turn), "unapproved" (not what this message approves) or "spent"."""
    if turn is not None and issued_turn >= int(turn):
        return "awaiting"
    if approved_tokens is not None and supplied not in approved_tokens:
        return "unapproved"
    return "allow" if _claim(session_id, supplied) else "spent"


def _check_token(
    session_id: str,
    tool_name: str,
    ti: dict[str, Any],
    active_business: str | None,
    turn: int | None,
    approved_tokens: frozenset[str] | set[str] | None = None,
    bound: dict[str, Any] | None = None,
) -> Decision:
    """The approval-token half of `check()`: honour a valid token minted on an
    earlier turn and approved by this turn's message, otherwise issue one bound
    to these exact arguments."""
    current = 0 if turn is None else int(turn)
    supplied = str(ti.get("approval_token") or "").strip()
    bound = bound if bound is not None else _salient_args(ti)
    summary = _summarize(tool_name, bound)

    def _fresh(reason: str = "confirm") -> Decision:
        expected = token_for(tool_name, bound, active_business, current, session_id)
        _issue(session_id, expected)
        return Decision(False, summary, expected, reason)

    if supplied:
        issued_turn = _turn_of(supplied)
        # Recompute the signature for the turn the token CLAIMS. A forged or
        # edited turn fails here, because the turn is inside the signed payload.
        if issued_turn is not None and hmac.compare_digest(
            supplied, token_for(tool_name, bound, active_business, issued_turn, session_id)
        ):
            verdict = _honour(session_id, supplied, issued_turn, turn, approved_tokens)
            if verdict == "allow":
                return _ALLOWED
            if verdict == "awaiting":
                # Correct token, but minted this very turn — the user has not
                # spoken since. Tell the model to stop, not to retry.
                return Decision(False, summary, supplied, "awaiting")
            # "unapproved": issued earlier, but not what this message approves.
            # "spent": already used for one attempt, whose result the model has
            # seen — it may have FAILED (a Gmail 429, a network error). Never
            # let the reply claim success from it; ask the user again instead.
            # Single use per process: `_spent` is what stops a replay. A
            # cross-process replay is closed by `approved_tokens` (a spent
            # token is no longer held) and by the session in the signature.
            return _fresh("spent" if verdict == "spent" else "confirm")

    return _fresh()


# ---- The draft gate (email_with_attachment) ----------------------------------
# email_with_attachment confirms through its own preview: the PREPARE call saves
# a Gmail draft and returns a preview the model relays; the SEND call
# (confirm=true) sends that exact draft. Nothing used to tie the send to a human
# having seen the preview — the model could prepare and send in one turn, or
# send with confirm=true and no draft at all, which rebuilt the message and sent
# it unreviewed (B4). The draft token closes both: prepare hands out a token
# bound to the draft_id and to the turn it was prepared in, and send is only
# allowed with that token, on a later turn, on the user's explicit approval.


def _draft_args(draft_id: str) -> dict[str, Any]:
    # The send step sends the stored draft by id and nothing else, so the draft
    # id is the whole of "what will happen".
    return {"draft_id": draft_id}


def draft_token(draft_id: str, business: str | None, turn: int, session_id: str | None = None) -> str:
    """The approval token handed out with a prepared draft (see above)."""
    return token_for("email_with_attachment", _draft_args(draft_id), business, turn, session_id)


def issue_draft_token(session_id: str, draft_id: str, business: str | None, turn: int) -> str:
    """Mint and register the draft token for a draft prepared during `turn`."""
    token = draft_token(draft_id, business, turn, session_id)
    _issue(session_id, token)
    return token


def draft_send_needs_approval(business: str | None, tool_input: dict[str, Any] | None = None) -> bool:
    """Whether sending a prepared draft must wait for the user's approval —
    i.e. confirm-before-write applies to this live chat's scope."""
    return _confirm_scope_enabled(business, tool_input or {})


def _check_draft_send(
    session_id: str,
    tool_name: str,
    ti: dict[str, Any],
    active_business: str | None,
    interactive: bool,
    turn: int | None,
    approved_tokens: frozenset[str] | set[str] | None = None,
) -> Decision:
    if not ti.get("confirm"):
        return _ALLOWED  # PREPARE: saves a private Gmail draft, sends and shares nothing
    if not interactive or not _confirm_scope_enabled(active_business, ti):
        return _ALLOWED
    summary = _summarize(tool_name, ti)
    draft_id = str(ti.get("draft_id") or "").strip()
    supplied = str(ti.get("approval_token") or "").strip()
    issued_turn = _turn_of(supplied) if supplied else None
    valid = bool(draft_id) and issued_turn is not None and hmac.compare_digest(
        supplied, draft_token(draft_id, active_business, issued_turn, session_id)
    )
    if not valid:
        # No prepared draft, or a token that doesn't belong to this draft:
        # nothing the user has reviewed. Prepare (again) instead.
        return Decision(False, summary, None, "needs_draft")
    verdict = _honour(session_id, supplied, issued_turn, turn, approved_tokens)
    if verdict == "allow":
        return _ALLOWED
    if verdict == "awaiting":
        return Decision(False, summary, supplied, "awaiting")
    current = 0 if turn is None else int(turn)
    fresh = issue_draft_token(session_id, draft_id, active_business, current)
    return Decision(False, summary, fresh, "spent" if verdict == "spent" else "confirm")


# ---- Carrying an approval across turns (B6) --------------------------------
# A Postgres-backed conversation replays only the reply TEXT of earlier turns
# (hermes/db/conversations.py), so the approval_token / draft token the model
# was handed in a tool_result is gone by the time the user says "yes". The
# agent persists what was held ("pending" in the message metadata), appends the
# gate's own summary of it to the reply (so the user always sees exactly what a
# "yes" approves — not whatever question the model chose to ask), and on the
# user's next message hands it back to the model — and to the gate, as the
# only tokens it will honour — but only when that message is an unambiguous
# approval of it.
#
# What counts as an approval (`explicit_approval`): the WHOLE message, after
# lowercasing and dropping punctuation, must
#   * open with an affirmation ("yes", "sure", "go ahead", "looks good",
#     "confirmed", "send it", ...; a bare "ok"/"okay" only with a real go-ahead
#     after it),
#   * contain no negation, hedge or change ("no", "not", "don't", "but",
#     "instead", "wait", "cancel", "change", "also", "cc", a question word, ...)
#     outside words quoted from the held summary itself,
#   * and say nothing else except filler, verbs that fit the HELD action ("send"
#     for a send, "delete" for a delete — so "ok, delete the draft" is not an
#     approval to SEND it), and words — numbers included — from the held
#     action's own summary.
# A missed approval only costs one more confirmation; a false one could send
# something the user didn't agree to.

_AFFIRM_LEADS: tuple[tuple[str, ...], ...] = tuple(
    tuple(lead.split()) for lead in (
        "yes please", "go for it", "go ahead", "please do", "please proceed", "do it",
        "send it", "delete it", "looks good", "sounds good", "looks fine", "that s right",
        "that is right", "all good", "lgtm", "yes", "yeah", "yep", "yup", "ya", "sure",
        "confirmed", "confirm", "approved", "approve", "i approve", "i confirm",
        "proceed", "absolutely", "definitely", "affirmative", "alright", "all right",
        "ok", "okay",
    )
)
# Leads too weak to stand alone: "ok" alone is as often "noted" as "do it".
_WEAK_LEADS = frozenset({("ok",), ("okay",), ("alright",), ("all", "right")})
# Leads that name a verb: they only approve an action of the matching kind.
_LEAD_VERB = {("send", "it"): "send", ("delete", "it"): "delete"}
_NEGATING = frozenset({
    "no", "not", "nope", "nah", "never", "don", "dont", "doesn", "didn", "isn", "won",
    "wont", "shouldn", "can", "cannot", "cant", "wait", "hold", "cancel", "stop",
    "instead", "but", "however", "except", "unless", "change", "changed", "edit",
    "modify", "only", "without", "later", "tomorrow", "actually", "rather",
    "different", "another", "other", "else", "also", "plus", "too", "cc", "bcc",
    "add", "first", "before", "after", "maybe", "perhaps", "if", "why", "what", "how",
    "which", "who", "when", "where", "hmm", "hm", "undo", "revert", "correct", "fix",
})
_FILLER = frozenset({
    "please", "pls", "plz", "now", "thanks", "thank", "you", "ty", "thx", "it", "that",
    "this", "them", "those", "these", "the", "a", "an", "and", "to", "go", "ahead",
    "do", "just", "right", "away", "so", "yes", "yeah", "sure", "ok", "okay", "all",
    "good", "great", "perfect", "fine", "looks", "sounds", "is", "s", "for", "me",
    "my", "of", "on", "with", "as", "planned", "described", "shown", "above", "same",
    "exactly", "one", "both", "everything", "confirmed", "approved", "proceed",
    "proceeding", "confirm", "approve", "carry", "out", "i", "we", "be",
})
# Verbs (and their objects) that fit each kind of held action.
_KIND_WORDS: dict[str, frozenset[str]] = {
    "send": frozenset({
        "send", "sending", "sent", "email", "emails", "mail", "message", "messages",
        "reply", "invite", "whatsapp", "draft", "attachment", "attachments", "file",
    }),
    "delete": frozenset({
        "delete", "deleting", "remove", "removing", "permanently", "file", "files",
        "task", "event", "it",
    }),
    "safety": frozenset({
        "turn", "turning", "off", "disable", "disabling", "asking", "confirmation",
        "confirmations", "guardrail", "isolation",
    }),
    "authorize": frozenset({
        "create", "creating", "schedule", "scheduling", "set", "up", "update", "updating",
        "enable", "resume", "run", "full", "autonomy", "task",
    }),
    "write": frozenset({
        "create", "creating", "add", "adding", "update", "updating", "assign",
        "assigning", "post", "save", "issue", "contact", "it",
    }),
}
# Phrases that contain a negating word but ARE the approved action — "yes, stop
# asking me" approves turning confirmations off. Removed before the check.
_ACTION_PHRASES: tuple[tuple[str, ...], ...] = (("stop", "asking"),)
_KIND_PHRASES = {("stop", "asking"): "safety"}


def _kind(record: dict[str, Any]) -> str:
    """The kind of a held action, for matching an approval's verbs."""
    tool = record.get("tool")
    args = record.get("input") if isinstance(record.get("input"), dict) else {}
    cls = classify(str(tool), args) if tool != "email_with_attachment" else SEND
    return {
        SEND: "send", DESTRUCTIVE: "delete", SAFETY: "safety", AUTHORIZE: "authorize",
    }.get(cls, "write")


def _words(text: str | None) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def _strip_runs(words: list[str], runs: list[list[str]], min_len: int = 3) -> list[str]:
    """Remove every stretch of `words` that repeats >= min_len consecutive words
    of one of `runs` (the held summaries): quoting the guardrail's own label
    "confirm before write" must not trip the negation check on "before"."""
    out: list[str] = []
    i = 0
    while i < len(words):
        best = 0
        for run in runs:
            for j in range(len(run)):
                n = 0
                while i + n < len(words) and j + n < len(run) and words[i + n] == run[j + n]:
                    n += 1
                best = max(best, n)
        if best >= min_len:
            i += best
            continue
        out.append(words[i])
        i += 1
    return out


def _drop_phrases(words: list[str]) -> tuple[list[str], set[str]]:
    """Remove the action phrases; report which kinds they named."""
    out: list[str] = []
    kinds: set[str] = set()
    i = 0
    while i < len(words):
        phrase = next((ph for ph in _ACTION_PHRASES if tuple(words[i:i + len(ph)]) == ph), None)
        if phrase:
            kinds.add(_KIND_PHRASES.get(phrase, ""))
            i += len(phrase)
            continue
        out.append(words[i])
        i += 1
    return out, kinds


def explicit_approval(text: str | None, held: list[dict[str, Any]] | tuple | None = None) -> bool:
    """True when the user's whole message is an unambiguous go-ahead for the
    `held` actions (the previous reply's pending records). See the rules above.

    With no `held`, any kind of action verb is accepted — only for callers that
    have nothing specific to match against."""
    held = [h for h in (held or []) if isinstance(h, dict)]
    words = _words(text)
    if not words or len(words) > 24:
        return False
    summaries = [_words(h.get("summary")) for h in held]
    words = _strip_runs(words, [s for s in summaries if s])
    words, phrase_kinds = _drop_phrases(words)
    kinds = {_kind(h) for h in held} if held else set(_KIND_WORDS)
    if phrase_kinds - {""} - kinds:
        return False  # "stop asking" approves turning a guardrail off, nothing else
    if any(w in _NEGATING for w in words):
        return False
    if not words:
        return bool(phrase_kinds)
    lead = next((ld for ld in _AFFIRM_LEADS if tuple(words[:len(ld)]) == ld), None)
    if lead is None:
        return False
    if lead in _LEAD_VERB and _LEAD_VERB[lead] not in kinds:
        return False
    rest = words[len(lead):]
    allowed = set(_FILLER)
    for kind in kinds:
        allowed |= _KIND_WORDS[kind]
    for s in summaries:
        allowed |= set(s)
    if lead in _WEAK_LEADS:
        # "ok" must be followed by a real go-ahead: "ok send it", "ok go ahead".
        nested = next(
            (ld for ld in _AFFIRM_LEADS if ld not in _WEAK_LEADS and tuple(rest[:len(ld)]) == ld),
            None,
        )
        verb_words = set().union(*(_KIND_WORDS[k] for k in kinds)) if kinds else set()
        if nested is None and not (rest and rest[0] in verb_words) and not phrase_kinds:
            return False
    return all(w in allowed for w in rest)


def declines(text: str | None) -> bool:
    """True when the message pushes back on, questions or changes what was
    held (any negating word). Used to decide whether a held action survives a
    reply that isn't an approval."""
    words, _ = _drop_phrases(_words(text))
    return any(w in _NEGATING for w in words)


def pending_record(
    tool_name: str,
    tool_input: dict[str, Any],
    approval_token: str,
    summary: str | None = None,
    carried: int = 0,
) -> dict[str, Any]:
    """The persisted form of one held action: the exact arguments the token is
    bound to (minus the token itself), the token, the gate's one-line summary —
    the text shown to the user, and the words an approval may restate — and how
    many replies it has already survived without an answer."""
    return {
        "tool": tool_name,
        "input": _salient_args(tool_input),
        "approval_token": approval_token,
        "summary": summary if summary is not None else _summarize(tool_name, tool_input),
        "carried": int(carried),
    }


def strip_token(tool_input: dict[str, Any] | None) -> dict[str, Any]:
    """Arguments safe to show or persist: everything but a live approval_token."""
    return _salient_args(tool_input or {})
