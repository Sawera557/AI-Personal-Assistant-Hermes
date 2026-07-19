"""HTTP routes for Jobs (the Jobs tab) — a self-contained FastAPI APIRouter.

Split out of app.py for readability: everything about the /api/jobs surface lives
here. app.py just does `from hermes.jobs_api import router` +
`app.include_router(router)`. Mirrors the scheduled-tasks API but fires
registered Python functions (hermes/job_runner.py) instead of agent prompts:
Start = run now, Stop = flag cancel, plus a shared /api/jobs/run-due cron trigger.

Kept independent of app.py (no import back into it) to avoid a circular import —
so the small schedule model + validator are defined locally rather than shared.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import job_runner as hermes_jobs
from . import scheduler as hermes_scheduler
from .config import Config
from .db import jobs as jobs_db

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ── Request models + helpers ─────────────────────────────────────────────────
class ScheduleBody(BaseModel):
    frequency: str = "daily"           # hourly | daily | weekdays | weekly
    times: list[str] = []              # ["07:00", "13:00", ...] wall-clock
    days_of_week: list[int] = []       # 0=Mon … 6=Sun, for weekly


class JobCreateBody(BaseModel):
    job_type: str
    name: str
    config: dict = {}
    schedule: ScheduleBody
    timezone: str | None = None
    enabled: bool = True
    allow_multiple_runs: bool = False


class JobPatchBody(BaseModel):
    name: str | None = None
    config: dict | None = None
    schedule: ScheduleBody | None = None
    timezone: str | None = None
    enabled: bool | None = None
    allow_multiple_runs: bool | None = None


def _validate_schedule(schedule: dict) -> str | None:
    """Return an error string if the schedule is unusable, else None."""
    freq = (schedule or {}).get("frequency")
    if freq not in hermes_scheduler.VALID_FREQUENCIES:
        return f"frequency must be one of {sorted(hermes_scheduler.VALID_FREQUENCIES)}."
    if freq == "weekly" and not (schedule.get("days_of_week") or []):
        return "A weekly schedule needs at least one day in days_of_week (0=Mon … 6=Sun)."
    if freq != "hourly" and not (schedule.get("times") or []):
        return "Provide at least one time (HH:MM) for this schedule."
    return None


def _enrich_job(job: dict) -> dict:
    """Add the human schedule summary the UI shows on each card."""
    if job:
        job["schedule_summary"] = hermes_scheduler.describe_schedule(
            job.get("schedule") or {}, job.get("timezone")
        )
    return job


# ── Routes ───────────────────────────────────────────────────────────────────
@router.get("")
async def list_jobs():
    """List all jobs (newest-updated first). Jobs are system-wide crons."""
    jobs = await run_in_threadpool(jobs_db.list_jobs)
    return {"jobs": [_enrich_job(j) for j in jobs], "job_types": hermes_jobs.job_types()}


@router.post("")
async def create_job(body: JobCreateBody):
    """Create a job. Validates the job_type against the registry + the schedule,
    computes the first next_run_at, and stores it."""
    name = (body.name or "").strip()
    if not name:
        return JSONResponse({"error": "A job name is required."}, status_code=400)
    if body.job_type not in hermes_jobs.REGISTRY:
        return JSONResponse(
            {"error": f"Unknown job_type. Known: {hermes_jobs.job_types()}."}, status_code=400
        )
    schedule = body.schedule.model_dump()
    if err := _validate_schedule(schedule):
        return JSONResponse({"error": err}, status_code=400)

    tz = (body.timezone or Config.SCHEDULER_DEFAULT_TZ).strip() or Config.SCHEDULER_DEFAULT_TZ
    next_run = hermes_scheduler.compute_next_run(schedule, tz) if body.enabled else None

    job_id = await run_in_threadpool(
        jobs_db.create_job,
        job_type=body.job_type,
        name=name,
        schedule=schedule,
        timezone_name=tz,
        config=body.config or {},
        enabled=body.enabled,
        allow_multiple_runs=body.allow_multiple_runs,
        next_run_at=next_run,
    )
    if not job_id:
        return JSONResponse({"error": "Could not create the job."}, status_code=500)
    job = await run_in_threadpool(jobs_db.get_job, job_id)
    return {"job": _enrich_job(job)}


# NOTE: this literal route MUST be declared before "/{job_id}" — FastAPI matches
# in definition order and would otherwise read "run-due" as an id.
@router.api_route("/run-due", methods=["GET", "POST"])
async def run_due_jobs(request: Request, limit: int = 25):
    """Fire every job whose next_run_at has passed. The endpoint a cron hits.
    Auth is identical to /api/scheduled-tasks/run-due (SCHEDULER_SECRET /
    CRON_SECRET as a Bearer token or ?token=)."""
    secrets = {s for s in (Config.SCHEDULER_SECRET, os.getenv("CRON_SECRET", "").strip()) if s}
    if secrets:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = request.query_params.get("token") or bearer
        if token not in secrets:
            return JSONResponse({"error": "Unauthorized."}, status_code=401)
    result = await run_in_threadpool(hermes_jobs.run_due, max(1, min(limit, 100)))
    return result


@router.get("/{job_id}")
async def get_job_detail(job_id: str):
    """One job plus its recent run history (for the detail panel)."""
    job = await run_in_threadpool(jobs_db.get_job, job_id)
    if not job:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    runs = await run_in_threadpool(jobs_db.list_runs, job_id, 20)
    return {"job": _enrich_job(job), "runs": runs}


@router.patch("/{job_id}")
async def update_job_detail(job_id: str, body: JobPatchBody):
    """Edit a job. Only the fields sent are changed. Any change to the schedule,
    timezone, or enabled flag recomputes next_run_at."""
    existing = await run_in_threadpool(jobs_db.get_job, job_id)
    if not existing:
        return JSONResponse({"error": "Job not found."}, status_code=404)

    sent = body.model_dump(exclude_unset=True)
    kwargs: dict = {}
    if "name" in sent:
        if not (body.name or "").strip():
            return JSONResponse({"error": "A job name is required."}, status_code=400)
        kwargs["name"] = body.name.strip()
    if "config" in sent and body.config is not None:
        kwargs["config"] = body.config
    if "schedule" in sent and body.schedule is not None:
        sched = body.schedule.model_dump()
        if err := _validate_schedule(sched):
            return JSONResponse({"error": err}, status_code=400)
        kwargs["schedule"] = sched
    if "timezone" in sent and body.timezone:
        kwargs["timezone_name"] = body.timezone.strip()
    if "enabled" in sent:
        kwargs["enabled"] = body.enabled
    if "allow_multiple_runs" in sent:
        kwargs["allow_multiple_runs"] = body.allow_multiple_runs

    schedule = kwargs.get("schedule", existing.get("schedule") or {})
    tz = kwargs.get("timezone_name", existing.get("timezone"))
    enabled = kwargs.get("enabled", existing.get("enabled"))
    if {"schedule", "timezone_name", "enabled"} & kwargs.keys():
        kwargs["next_run_at"] = hermes_scheduler.compute_next_run(schedule, tz) if enabled else None
        kwargs["next_run_set"] = True

    ok = await run_in_threadpool(lambda: jobs_db.update_job(job_id, **kwargs))
    if not ok:
        return JSONResponse({"error": "Nothing to update or job not found."}, status_code=400)
    job = await run_in_threadpool(jobs_db.get_job, job_id)
    return {"job": _enrich_job(job)}


@router.delete("/{job_id}")
async def delete_job_detail(job_id: str):
    ok = await run_in_threadpool(jobs_db.delete_job, job_id)
    if not ok:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return {"deleted": job_id}


@router.get("/{job_id}/runs")
async def list_job_runs(job_id: str, limit: int = 20):
    runs = await run_in_threadpool(jobs_db.list_runs, job_id, max(1, min(limit, 100)))
    return {"job_id": job_id, "runs": runs}


@router.post("/{job_id}/run")
async def run_job_now(job_id: str):
    """Start a job immediately (manual trigger), without disturbing its next
    scheduled fire. Blocks until the run completes, then returns the run summary."""
    job = await run_in_threadpool(jobs_db.get_job, job_id)
    if not job:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    try:
        summary = await run_in_threadpool(hermes_jobs.execute_and_record, job, "manual")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Run failed: {exc}"}, status_code=500)
    return summary


@router.post("/{job_id}/stop")
async def stop_job(job_id: str):
    """Request a running job to stop. Sets cancel_requested; the job function
    polls it and aborts cleanly (recorded as a 'cancelled' run)."""
    ok = await run_in_threadpool(jobs_db.request_cancel, job_id)
    if not ok:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return {"stopping": job_id}
