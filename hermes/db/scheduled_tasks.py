"""Postgres-backed store for Scheduled Tasks — mirrors hermes/db/meetings.py.

scheduled_tasks      — one row per task, owned by the single owner user, scoped
                       to one business (entity_id) or NULL for "All Businesses".
                       Carries the prompt, the allowed apps, the autonomy level,
                       the structured schedule, and the resolved next_run_at that
                       the due-scan filters on.
scheduled_task_runs  — one row per execution (manual or scheduled), with the
                       agent's reply + tool trace, mirroring how chat stores a turn.

Scheduling is pull-based (see hermes/scheduler.py): `due()` returns tasks whose
next_run_at has passed, the caller runs each and records the outcome, and
`finish_run` advances next_run_at. Nothing here runs the agent — that stays in
the scheduler/agent layer, so this module is pure storage.
"""
from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.scheduled_tasks")

_TASK_COLS = (
    "t.task_id, t.name, t.prompt, t.allowed_apps, t.autonomy, t.schedule, "
    "t.timezone, t.enabled, e.slug, "
    "t.next_run_at, t.last_run_at, t.last_status, t.created_at, t.updated_at"
)


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _owner(cur) -> str:
    """Same owner-first resolution as conversations.py / meetings.py."""
    email = (os.getenv("OWNER_EMAIL") or "owner@hermes.local").strip().lower()
    name = (os.getenv("OWNER_NAME") or "Owner").strip()
    cur.execute("SELECT user_id FROM users WHERE lower(email) = %s;", (email,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING user_id;",
            (name, email),
        )
        row = cur.fetchone()
    return str(row[0])


def _resolve_entity_id(cur, entity_slug: str | None) -> str | None:
    """entities.slug (or a uuid) -> entities.entity_id, or None for unscoped."""
    eid = str(entity_slug or "").strip()
    if not eid:
        return None
    col = "entity_id" if _is_uuid(eid) else "slug"
    cur.execute(f"SELECT entity_id FROM entities WHERE {col} = %s;", (eid,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _task_row_to_dict(row: tuple) -> dict[str, Any]:
    from ..businesses import to_business_slug  # local import: avoid a cycle

    (task_id, name, prompt, allowed_apps, autonomy, schedule, tz, enabled,
     entity_slug, next_run_at, last_run_at, last_status, created, updated) = row
    return {
        "task_id": str(task_id),
        "name": name,
        "prompt": prompt,
        "allowed_apps": allowed_apps or [],
        "autonomy": autonomy,
        "schedule": schedule or {},
        "timezone": tz,
        "enabled": enabled,
        "business": to_business_slug(entity_slug),   # None == All Businesses
        "next_run_at": _iso(next_run_at),
        "last_run_at": _iso(last_run_at),
        "last_status": last_status,
        "created_at": _iso(created),
        "updated_at": _iso(updated),
    }


def create_task(
    *,
    entity_slug: str | None,
    name: str,
    prompt: str,
    schedule: dict[str, Any],
    timezone_name: str,
    allowed_apps: list[str] | None = None,
    autonomy: str = "readonly",
    enabled: bool = True,
    next_run_at: datetime | None = None,
) -> str | None:
    """Insert a task and return its task_id (or None on failure).

    `entity_slug` None == "All Businesses" scope. `next_run_at` is the resolved
    UTC instant computed by the scheduler; NULL when disabled."""
    try:
        with cursor() as cur:
            owner = _owner(cur)
            ent = _resolve_entity_id(cur, entity_slug)  # None is valid (unscoped)
            cur.execute(
                """INSERT INTO scheduled_tasks
                       (user_id, entity_id, name, prompt, allowed_apps, autonomy,
                        schedule, timezone, enabled, next_run_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING task_id;""",
                (
                    owner, ent, name, prompt,
                    json.dumps(allowed_apps or []), autonomy,
                    json.dumps(schedule or {}), timezone_name, enabled,
                    next_run_at if enabled else None,
                ),
            )
            return str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("create_task failed: %s", exc)
        return None


def get_task(task_id: str) -> dict[str, Any] | None:
    if not _is_uuid(task_id):
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_TASK_COLS} FROM scheduled_tasks t
                    LEFT JOIN entities e ON e.entity_id = t.entity_id
                    WHERE t.task_id = %s;""",
                (task_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_task(%s) failed: %s", task_id, exc)
        return None
    return _task_row_to_dict(row) if row else None


def list_tasks(entity_slug: str | None = None) -> list[dict[str, Any]]:
    """All of the owner's tasks (newest-updated first). Pass a business slug to
    scope to one business; omit (or "all") for every task."""
    try:
        with cursor(commit=False) as cur:
            owner = _owner(cur)
            where = ["t.user_id = %s"]
            params: list[Any] = [owner]
            if entity_slug and entity_slug != "all":
                ent = _resolve_entity_id(cur, entity_slug)
                where.append("t.entity_id = %s")
                params.append(ent)
            cur.execute(
                f"""SELECT {_TASK_COLS} FROM scheduled_tasks t
                    LEFT JOIN entities e ON e.entity_id = t.entity_id
                    WHERE {' AND '.join(where)}
                    ORDER BY t.updated_at DESC
                    LIMIT 200;""",
                params,
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_tasks failed: %s", exc)
        return []
    return [_task_row_to_dict(r) for r in rows]


def update_task(
    task_id: str,
    *,
    name: str | None = None,
    prompt: str | None = None,
    schedule: dict[str, Any] | None = None,
    timezone_name: str | None = None,
    allowed_apps: list[str] | None = None,
    autonomy: str | None = None,
    enabled: bool | None = None,
    entity_slug: str | None = None,
    entity_slug_set: bool = False,
    next_run_at: datetime | None = None,
    next_run_set: bool = False,
) -> bool:
    """Patch a task. Only the fields passed (non-None) are written. `entity_slug`
    and `next_run_at` are nullable columns, so they use explicit *_set flags to
    tell "clear it to NULL" apart from "don't touch it"."""
    if not _is_uuid(task_id):
        return False
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s"); params.append(name)
    if prompt is not None:
        sets.append("prompt = %s"); params.append(prompt)
    if schedule is not None:
        sets.append("schedule = %s"); params.append(json.dumps(schedule))
    if timezone_name is not None:
        sets.append("timezone = %s"); params.append(timezone_name)
    if allowed_apps is not None:
        sets.append("allowed_apps = %s"); params.append(json.dumps(allowed_apps))
    if autonomy is not None:
        sets.append("autonomy = %s"); params.append(autonomy)
    if enabled is not None:
        sets.append("enabled = %s"); params.append(enabled)
    if next_run_set:
        sets.append("next_run_at = %s"); params.append(next_run_at)
    if not sets and not entity_slug_set:
        return False
    try:
        with cursor() as cur:
            if entity_slug_set:
                ent = _resolve_entity_id(cur, entity_slug)  # None clears scope
                sets.append("entity_id = %s"); params.append(ent)
            sets.append("updated_at = now()")
            params.append(task_id)
            cur.execute(
                f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE task_id = %s;",
                params,
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("update_task(%s) failed: %s", task_id, exc)
        return False


def delete_task(task_id: str) -> bool:
    """Hard-delete a task and its runs (ON DELETE CASCADE)."""
    if not _is_uuid(task_id):
        return False
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM scheduled_tasks WHERE task_id = %s;", (task_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_task(%s) failed: %s", task_id, exc)
        return False


def due(now: datetime | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Enabled tasks whose next_run_at has passed — the cron's work list.

    Ordered oldest-due first so a backlog drains fairly. `limit` caps how many
    a single trigger will attempt, so one crowded minute can't run unbounded."""
    now = now or datetime.now(timezone.utc)
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_TASK_COLS} FROM scheduled_tasks t
                    LEFT JOIN entities e ON e.entity_id = t.entity_id
                    WHERE t.enabled AND t.next_run_at IS NOT NULL AND t.next_run_at <= %s
                    ORDER BY t.next_run_at ASC
                    LIMIT %s;""",
                (now, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("due() failed: %s", exc)
        return []
    return [_task_row_to_dict(r) for r in rows]


def start_run(task_id: str, trigger: str = "schedule") -> str | None:
    """Open a run row (status='running') and mark the task running. Returns the
    run_id, or None on failure."""
    if not _is_uuid(task_id):
        return None
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO scheduled_task_runs (task_id, trigger, status)
                   VALUES (%s, %s, 'running') RETURNING run_id;""",
                (task_id, trigger),
            )
            run_id = str(cur.fetchone()[0])
            cur.execute(
                "UPDATE scheduled_tasks SET last_status = 'running', updated_at = now() "
                "WHERE task_id = %s;",
                (task_id,),
            )
            return run_id
    except Exception as exc:  # noqa: BLE001
        log.warning("start_run(%s) failed: %s", task_id, exc)
        return None


def finish_run(
    run_id: str,
    task_id: str,
    *,
    status: str,
    reply: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    error: str | None = None,
    next_run_at: datetime | None = None,
    set_next: bool = True,
) -> None:
    """Close a run with its outcome, and stamp the task's last_run_at/last_status.

    When `set_next` is True (a scheduled fire), also roll next_run_at forward to
    the freshly-computed instant. A manual "Run now" passes set_next=False so it
    records the run without disturbing the next scheduled fire."""
    if not _is_uuid(run_id) or not _is_uuid(task_id):
        return
    try:
        with cursor() as cur:
            cur.execute(
                """UPDATE scheduled_task_runs SET
                       status = %s, reply = %s, tool_calls = %s, error = %s,
                       finished_at = now()
                   WHERE run_id = %s;""",
                (status, reply, json.dumps(tool_calls or []), error, run_id),
            )
            if set_next:
                cur.execute(
                    """UPDATE scheduled_tasks SET
                           last_run_at = now(), last_status = %s,
                           next_run_at = %s, updated_at = now()
                       WHERE task_id = %s;""",
                    (status, next_run_at, task_id),
                )
            else:
                cur.execute(
                    """UPDATE scheduled_tasks SET
                           last_run_at = now(), last_status = %s, updated_at = now()
                       WHERE task_id = %s;""",
                    (status, task_id),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("finish_run(%s) failed: %s", run_id, exc)


def list_runs(task_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """A task's recent runs (newest first), for the run-history panel."""
    if not _is_uuid(task_id):
        return []
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT run_id, trigger, status, reply, tool_calls, error,
                          started_at, finished_at
                   FROM scheduled_task_runs
                   WHERE task_id = %s ORDER BY started_at DESC LIMIT %s;""",
                (task_id, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_runs(%s) failed: %s", task_id, exc)
        return []
    out = []
    for run_id, trigger, status, reply, tool_calls, error, started, finished in rows:
        out.append({
            "run_id": str(run_id),
            "trigger": trigger,
            "status": status,
            "reply": reply,
            "tool_calls": tool_calls or [],
            "error": error,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
        })
    return out
