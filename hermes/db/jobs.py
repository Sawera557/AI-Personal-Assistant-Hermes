"""Postgres-backed store for Jobs — mirrors hermes/db/scheduled_tasks.py.

jobs      — one row per code-driven cron job (a job_type mapping to a Python
            function in hermes/job_runner.py), with its config, structured
            schedule, resolved next_run_at, and a cancel_requested flag.
job_runs  — one row per execution (manual or scheduled), with the job's result
            summary or error.

Scheduling is pull-based (see hermes/job_runner.py): `due()` returns jobs whose
next_run_at has passed, the caller runs each and records the outcome, and
`finish_run` advances next_run_at. Nothing here runs the job function — that
stays in the runner layer, so this module is pure storage.
"""
from __future__ import annotations

import json
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.jobs")

_JOB_COLS = (
    "job_id, job_type, name, config, schedule, timezone, enabled, "
    "next_run_at, last_run_at, last_status, cancel_requested, "
    "allow_multiple_runs, created_at, updated_at"
)


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _job_row_to_dict(row: tuple) -> dict[str, Any]:
    (job_id, job_type, name, config, schedule, tz, enabled,
     next_run_at, last_run_at, last_status, cancel_requested,
     allow_multiple_runs, created, updated) = row
    return {
        "job_id": str(job_id),
        "job_type": job_type,
        "name": name,
        "config": config or {},
        "schedule": schedule or {},
        "timezone": tz,
        "enabled": enabled,
        "next_run_at": _iso(next_run_at),
        "last_run_at": _iso(last_run_at),
        "last_status": last_status,
        "cancel_requested": bool(cancel_requested),
        "allow_multiple_runs": bool(allow_multiple_runs),
        "created_at": _iso(created),
        "updated_at": _iso(updated),
    }


def create_job(
    *,
    job_type: str,
    name: str,
    schedule: dict[str, Any],
    timezone_name: str,
    config: dict[str, Any] | None = None,
    enabled: bool = True,
    allow_multiple_runs: bool = False,
    next_run_at: datetime | None = None,
) -> str | None:
    """Insert a job and return its job_id (or None on failure)."""
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO jobs
                       (job_type, name, config, schedule, timezone, enabled,
                        allow_multiple_runs, next_run_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING job_id;""",
                (
                    job_type, name, json.dumps(config or {}),
                    json.dumps(schedule or {}), timezone_name, enabled,
                    allow_multiple_runs, next_run_at if enabled else None,
                ),
            )
            return str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("create_job failed: %s", exc)
        return None


def get_job(job_id: str) -> dict[str, Any] | None:
    if not _is_uuid(job_id):
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE job_id = %s;", (job_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_job(%s) failed: %s", job_id, exc)
        return None
    return _job_row_to_dict(row) if row else None


def list_jobs() -> list[dict[str, Any]]:
    """All jobs, newest-updated first (jobs are system-wide, not business-scoped)."""
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"SELECT {_JOB_COLS} FROM jobs ORDER BY created_at ASC LIMIT 200;"
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_jobs failed: %s", exc)
        return []
    return [_job_row_to_dict(r) for r in rows]


def update_job(
    job_id: str,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    timezone_name: str | None = None,
    enabled: bool | None = None,
    allow_multiple_runs: bool | None = None,
    next_run_at: datetime | None = None,
    next_run_set: bool = False,
) -> bool:
    """Patch a job. Only the fields passed (non-None) are written. `next_run_at`
    is nullable, so it uses next_run_set to tell "clear to NULL" from "leave it"."""
    if not _is_uuid(job_id):
        return False
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s"); params.append(name)
    if config is not None:
        sets.append("config = %s"); params.append(json.dumps(config))
    if schedule is not None:
        sets.append("schedule = %s"); params.append(json.dumps(schedule))
    if timezone_name is not None:
        sets.append("timezone = %s"); params.append(timezone_name)
    if enabled is not None:
        sets.append("enabled = %s"); params.append(enabled)
    if allow_multiple_runs is not None:
        sets.append("allow_multiple_runs = %s"); params.append(allow_multiple_runs)
    if next_run_set:
        sets.append("next_run_at = %s"); params.append(next_run_at)
    if not sets:
        return False
    try:
        with cursor() as cur:
            sets.append("updated_at = now()")
            params.append(job_id)
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = %s;", params)
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("update_job(%s) failed: %s", job_id, exc)
        return False


def delete_job(job_id: str) -> bool:
    """Hard-delete a job and its runs (ON DELETE CASCADE)."""
    if not _is_uuid(job_id):
        return False
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = %s;", (job_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_job(%s) failed: %s", job_id, exc)
        return False


# ── Stop / cancel ────────────────────────────────────────────────────────────
def request_cancel(job_id: str) -> bool:
    """Flag a running job to stop. The job function polls is_cancel_requested()
    between steps and aborts cleanly. Returns False if the job isn't found."""
    if not _is_uuid(job_id):
        return False
    try:
        with cursor() as cur:
            cur.execute(
                "UPDATE jobs SET cancel_requested = true, updated_at = now() "
                "WHERE job_id = %s;",
                (job_id,),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("request_cancel(%s) failed: %s", job_id, exc)
        return False


def is_cancel_requested(job_id: str) -> bool:
    """Whether a Stop has been requested for this job (polled mid-run). Opens its
    own short connection so a long-running job sees a Stop set by another request."""
    if not _is_uuid(job_id):
        return False
    try:
        with cursor(commit=False) as cur:
            cur.execute("SELECT cancel_requested FROM jobs WHERE job_id = %s;", (job_id,))
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("is_cancel_requested(%s) failed: %s", job_id, exc)
        return False


# ── Firing / run lifecycle ───────────────────────────────────────────────────
def has_run_today(job_id: str, tz_name: str | None) -> bool:
    """Whether the job already has a run today (in its own timezone) that is
    successful or still in progress — the once-per-day guard. A prior 'error' or
    'cancelled' run does NOT count, so a failed attempt can be retried same-day."""
    if not _is_uuid(job_id):
        return False
    tz = tz_name or "America/Chicago"
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT 1 FROM job_runs
                   WHERE job_id = %s
                     AND status IN ('success', 'running')
                     AND (started_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date
                   LIMIT 1;""",
                (job_id, tz, tz),
            )
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("has_run_today(%s) failed: %s", job_id, exc)
        return False


def due(now: datetime | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Enabled jobs whose next_run_at has passed — the cron's work list."""
    now = now or datetime.now(timezone.utc)
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_JOB_COLS} FROM jobs
                    WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= %s
                    ORDER BY next_run_at ASC
                    LIMIT %s;""",
                (now, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("due() failed: %s", exc)
        return []
    return [_job_row_to_dict(r) for r in rows]


def claim_due(job_id: str, next_run_at: datetime | None, now: datetime | None = None) -> bool:
    """Atomically claim a due job for a scheduled fire and advance next_run_at.

    Same re-entrancy guard as scheduled_tasks.claim_due: a single UPDATE re-checks
    the job is still enabled AND due and moves next_run_at forward, so concurrent
    pollers can't both fire it and a slow run isn't re-selected every tick.
    Returns True only if THIS caller won the claim."""
    if not _is_uuid(job_id):
        return False
    now = now or datetime.now(timezone.utc)
    try:
        with cursor() as cur:
            cur.execute(
                """UPDATE jobs
                       SET next_run_at = %s, last_status = 'running', updated_at = now()
                   WHERE job_id = %s AND enabled
                         AND next_run_at IS NOT NULL AND next_run_at <= %s
                   RETURNING job_id;""",
                (next_run_at, job_id, now),
            )
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("claim_due(%s) failed: %s", job_id, exc)
        return False


def reap_stale_runs(older_than_minutes: int = 30) -> int:
    """Close runs stuck in 'running' whose process died mid-run (no finish_run).

    Marks any run still 'running' past the cutoff as 'error' and clears the parent
    job's last_status if it was left 'running'. Called at the top of run_due so it
    self-heals on the next tick — important on serverless, which can kill a run."""
    try:
        with cursor() as cur:
            cur.execute(
                """UPDATE job_runs
                       SET status = 'error',
                           error = 'run did not finish (process ended before completion)',
                           finished_at = now()
                   WHERE status = 'running'
                         AND started_at < now() - make_interval(mins => %s)
                   RETURNING job_id;""",
                (older_than_minutes,),
            )
            reaped = cur.fetchall()
            for (job_id,) in reaped:
                cur.execute(
                    "UPDATE jobs SET last_status = 'error' "
                    "WHERE job_id = %s AND last_status = 'running';",
                    (job_id,),
                )
            return len(reaped)
    except Exception as exc:  # noqa: BLE001
        log.warning("reap_stale_runs failed: %s", exc)
        return 0


def start_run(job_id: str, trigger: str = "schedule") -> str | None:
    """Open a run row (status='running'), mark the job running, and clear any
    pending Stop from a previous run. Returns the run_id, or None on failure."""
    if not _is_uuid(job_id):
        return None
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO job_runs (job_id, trigger, status)
                   VALUES (%s, %s, 'running') RETURNING run_id;""",
                (job_id, trigger),
            )
            run_id = str(cur.fetchone()[0])
            cur.execute(
                "UPDATE jobs SET last_status = 'running', cancel_requested = false, "
                "updated_at = now() WHERE job_id = %s;",
                (job_id,),
            )
            return run_id
    except Exception as exc:  # noqa: BLE001
        log.warning("start_run(%s) failed: %s", job_id, exc)
        return None


def finish_run(
    run_id: str,
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    next_run_at: datetime | None = None,
    set_next: bool = True,
) -> None:
    """Close a run with its outcome and stamp the job's last_run_at/last_status.

    When set_next is True (a scheduled fire) also roll next_run_at forward. A
    manual Start passes set_next=False so it records the run without disturbing
    the next scheduled fire. Always clears cancel_requested."""
    if not _is_uuid(run_id) or not _is_uuid(job_id):
        return
    try:
        with cursor() as cur:
            cur.execute(
                """UPDATE job_runs SET
                       status = %s, result = %s, error = %s, finished_at = now()
                   WHERE run_id = %s;""",
                (status, json.dumps(result or {}), error, run_id),
            )
            if set_next:
                cur.execute(
                    """UPDATE jobs SET
                           last_run_at = now(), last_status = %s, next_run_at = %s,
                           cancel_requested = false, updated_at = now()
                       WHERE job_id = %s;""",
                    (status, next_run_at, job_id),
                )
            else:
                cur.execute(
                    """UPDATE jobs SET
                           last_run_at = now(), last_status = %s,
                           cancel_requested = false, updated_at = now()
                       WHERE job_id = %s;""",
                    (status, job_id),
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("finish_run(%s) failed: %s", run_id, exc)


def list_runs(job_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """A job's recent runs (newest first), for the run-history panel."""
    if not _is_uuid(job_id):
        return []
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT run_id, trigger, status, result, error, started_at, finished_at
                   FROM job_runs
                   WHERE job_id = %s ORDER BY started_at DESC LIMIT %s;""",
                (job_id, limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_runs(%s) failed: %s", job_id, exc)
        return []
    out = []
    for run_id, trigger, status, result, error, started, finished in rows:
        out.append({
            "run_id": str(run_id),
            "trigger": trigger,
            "status": status,
            "result": result or {},
            "error": error,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
        })
    return out
