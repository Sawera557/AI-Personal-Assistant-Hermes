"""Signal health: which parts of a turn actually worked.

Every retrieval and memory path in Hermes is deliberately fail-open — a RAG
outage or a dead memory extractor must never break a chat turn. That is the
right call for uptime and the wrong call on its own, because a degraded answer
and a good answer look identical from outside. This module is the missing half:
fail open AND count it.

It exists because of a real, month-long outage. `memory_extract` and
`conversation_write` both call Config.GUIDANCE_MODEL, which is a Claude model;
when that account ran out of credit both silently stopped writing. Chat kept
working, `/api/health` kept saying "ok", and `conversation_memory` never
received a single row. Nothing in the system was capable of noticing.

So: every fail-open step reports ok / empty / failed here, and /api/health
surfaces both the counters and the last time the memory tables were actually
written. "Empty" is tracked separately from "failed" on purpose — a retrieval
that returns nothing on small talk is healthy, but one that returns nothing on
EVERY turn is a broken index, and only the ratio tells them apart.

In-process and non-persistent by design, like hermes.credits: this answers "is
it working right now", and a restart legitimately resets that question. The
durable half — when memory was last written — is read from the database.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("hermes.health")

#: Signals where returning nothing is a perfectly normal outcome, not a symptom:
#: most chats have no attachments, and recall/RAG correctly stay quiet on small
#: talk. These are never reported as "always-empty" — a health page that cries
#: wolf is one people stop reading, which is how the original outage survived.
EMPTY_IS_NORMAL = frozenset({"attachments", "conversation_recall", "rag", "memory_block"})

#: Runs required before "never returned anything" means anything at all. One
#: quiet turn is not evidence of a broken index.
EMPTY_MIN_RUNS = 20

#: The fail-open steps worth tracking, with a human label for /api/health.
SIGNALS: dict[str, str] = {
    "memory_block": "Long-term memory recall (per-turn)",
    "context_pack": "Per-business context pack",
    "rag": "Knowledge-base retrieval (pgvector + Pinecone)",
    "conversation_recall": "Past-conversation recall",
    "attachments": "Files shared earlier in the chat",
    "memory_extract": "Memory extraction (writes `memories`)",
    "conversation_write": "Turn recording (writes `conversation_memory`)",
    "pinecone": "Pinecone vector search",
}

_OK, _EMPTY, _FAILED = "ok", "empty", "failed"

_lock = threading.Lock()
_counts: dict[str, dict[str, int]] = {}
_last: dict[str, dict[str, Any]] = {}


def record(signal: str, status: str, detail: Any = "") -> None:
    """Note one outcome for `signal`. Never raises — health tracking must not
    become a new way for a turn to fail."""
    try:
        with _lock:
            bucket = _counts.setdefault(signal, {_OK: 0, _EMPTY: 0, _FAILED: 0})
            bucket[status] = bucket.get(status, 0) + 1
            entry = {"status": status, "at": time.time()}
            if detail:
                entry["detail"] = f"{type(detail).__name__}: {detail}"[:300] \
                    if isinstance(detail, BaseException) else str(detail)[:300]
            _last[signal] = entry
    except Exception:  # noqa: BLE001
        pass


def guarded(signal: str, fn: Callable[[], Any], default: Any = "") -> Any:
    """Run one fail-open step, returning `default` if it raises — and recording
    which of those two actually happened.

    Replaces the bare `try: ... except: default` blocks this codebase used
    everywhere. Behaviour is identical; the difference is that afterwards
    somebody can tell.
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 — fail open, exactly as before
        record(signal, _FAILED, exc)
        log.warning("%s failed this turn: %s", signal, exc, exc_info=True)
        return default
    record(signal, _OK if value else _EMPTY)
    return value


#: Signals that exist only to write to Postgres. When the database is
#: deliberately switched off they cannot succeed, and saying so on the health
#: page is crying wolf about a choice the operator made — the same reasoning
#: `_memory_write_times` already applies by returning {} in that mode.
DB_WRITE_SIGNALS = frozenset({"conversation_write", "memory_extract"})


def _db_deliberately_off() -> bool:
    import os

    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return not os.getenv("DBRDS_HOST", "").strip()


def _memory_write_times() -> dict[str, Any]:
    """When the memory tables were last actually written. This is the number
    that would have exposed the outage: chat active daily, memories a month
    stale."""
    if _db_deliberately_off():
        return {}
    out: dict[str, Any] = {}
    try:
        from .db.connection import cursor

        with cursor(commit=False) as cur:
            for table in ("memories", "conversation_memory"):
                try:
                    cur.execute(f"SELECT count(*), max(created_at) FROM {table};")  # noqa: S608
                    rows, last = cur.fetchone()
                    out[table] = {
                        "rows": rows,
                        "last_write": last.isoformat() if last else None,
                        "stale_days": (
                            round((time.time() - last.timestamp()) / 86400, 1) if last else None
                        ),
                    }
                except Exception as exc:  # noqa: BLE001
                    out[table] = {"error": str(exc)[:120]}
    except Exception as exc:  # noqa: BLE001 — health must never 500
        return {"error": str(exc)[:120]}
    return out


def snapshot(include_db: bool = True) -> dict[str, Any]:
    """Everything /api/health needs to show whether Hermes is quietly degraded."""
    with _lock:
        counts = {k: dict(v) for k, v in _counts.items()}
        last = {k: dict(v) for k, v in _last.items()}

    db_off = _db_deliberately_off()

    signals: dict[str, Any] = {}
    for key, label in SIGNALS.items():
        c = counts.get(key) or {_OK: 0, _EMPTY: 0, _FAILED: 0}
        total = sum(c.values())
        entry: dict[str, Any] = {"label": label, **c, "total": total}
        if key in last:
            entry["last"] = last[key]
        # Only a step that has actually run can be called degraded; one that
        # has never run this process is "unknown", not "healthy".
        if total == 0:
            entry["state"] = "unused"
        elif db_off and key in DB_WRITE_SIGNALS and c.get(_FAILED):
            # FAILED writes to a database that was switched off on purpose.
            # Reporting these as degradation made /api/health answer "degraded"
            # for every run of the offline test suite — exactly the cry-wolf
            # this module sets out to avoid. Scoped to failures only: a write
            # signal that merely returns nothing is a different question, and
            # still reaches the always-empty check below.
            entry["state"] = "db-disabled"
        elif c.get(_FAILED):
            entry["state"] = "degraded"
        elif (
            c.get(_OK) == 0
            and key not in EMPTY_IS_NORMAL
            and total >= EMPTY_MIN_RUNS
        ):
            entry["state"] = "always-empty"
        else:
            entry["state"] = "ok"
        signals[key] = entry

    out: dict[str, Any] = {"signals": signals}
    if include_db:
        out["memory_writes"] = _memory_write_times()

    problems = [
        f"{v['label']}: {v[_FAILED]} failure(s) this process"
        for v in signals.values() if v.get("state") == "degraded"
    ]
    problems += [
        f"{v['label']}: ran {v['total']}x and never returned anything "
        "(index may be empty or mis-scoped)"
        for v in signals.values() if v.get("state") == "always-empty"
    ]
    for table, info in (out.get("memory_writes") or {}).items():
        if isinstance(info, dict) and info.get("rows") == 0:
            problems.append(f"{table} has never been written")
        elif isinstance(info, dict) and (info.get("stale_days") or 0) > 7:
            problems.append(
                f"{table} has not been written for {info['stale_days']} days"
            )
    out["degraded"] = problems
    return out


def reset() -> None:
    """Clear counters — for tests."""
    with _lock:
        _counts.clear()
        _last.clear()
