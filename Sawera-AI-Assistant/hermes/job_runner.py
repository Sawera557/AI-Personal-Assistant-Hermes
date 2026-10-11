"""Jobs — the code-driven cron runner (the Jobs tab's execution layer).

The code-driven analog of hermes/scheduler.py. Where the scheduler runs an agent
PROMPT, this runs a registered Python FUNCTION selected by `job_type`:

    REGISTRY = {"google_reviews_scraper": reviews_scraper.run, ...}

Each function has the signature `run(config: dict, cancel_check: Callable) -> dict`
and returns a JSON-able summary that gets stored on the run row. Add a future
cron by writing a function and registering it here — the DB layer, API, schedule
math, and UI are all generic.

Firing is pull-based and reuses the scheduler's schedule math (compute_next_run):
something (the /api/jobs/run-due cron, or a manual Start) asks the DB layer for
due jobs and calls `execute_and_record` on each. Nothing here runs a background
thread — that wouldn't survive on serverless.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import drive_sync
from . import reviews_scraper
from . import scheduler as _scheduler  # reuse compute_next_run / describe_schedule
from .db import jobs as repo

log = logging.getLogger("hermes.job_runner")

# job_type → the callable that runs it. The single source of truth for what jobs
# exist; the seed migration inserts a row whose job_type is a key here.
REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "google_reviews_scraper": reviews_scraper.run,
    "drive_s3_sync": drive_sync.run,
}


def job_types() -> list[str]:
    return sorted(REGISTRY.keys())


def run_job(job: dict[str, Any], cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Dispatch one job to its registered function and return its summary dict."""
    job_type = job.get("job_type")
    fn = REGISTRY.get(job_type)
    if fn is None:
        raise ValueError(f"Unknown job_type {job_type!r} (not in REGISTRY).")
    config = dict(job.get("config") or {})
    # Let the scraper resolve its rotation/date in the job's own timezone.
    config.setdefault("timezone", job.get("timezone"))
    return fn(config, cancel_check=cancel_check)


def execute_and_record(job: dict[str, Any], trigger: str = "schedule") -> dict[str, Any]:
    """Run one job end-to-end and persist the outcome: open a run row, run the
    function, close the run, and (for a scheduled fire) roll next_run_at forward.

    Mirrors scheduler.execute_and_record. For a scheduled fire we ATOMICALLY CLAIM
    the job first (advancing next_run_at up front) so the same still-due job can't
    be fired twice in a poll window and concurrent pollers can't both grab it. A
    manual Start never claims and leaves the next scheduled fire untouched. Never
    raises — a failure is recorded as an 'error' run so the schedule keeps moving.

    The run function polls a cancel_check (backed by jobs.is_cancel_requested) so
    a Stop set from another request interrupts it; a cleanly-interrupted run is
    recorded as 'cancelled'.

    Once-per-day guard: unless the job has allow_multiple_runs, a fire (manual OR
    scheduled) is SKIPPED when the job already ran today (in its timezone) — so a
    double-click or an overlapping schedule can't run it twice a day."""
    job_id = job.get("job_id")
    tz = job.get("timezone")

    if not job.get("allow_multiple_runs") and repo.has_run_today(job_id, tz):
        # Already ran today. For a scheduled fire, roll next_run_at forward so the
        # poller doesn't re-select it every tick for the rest of the day.
        if trigger == "schedule":
            nxt = _scheduler.compute_next_run(job.get("schedule") or {}, tz) if job.get("enabled") else None
            repo.update_job(job_id, next_run_at=nxt, next_run_set=True)
        return {"job_id": job_id, "status": "skipped", "reason": "already ran today"}

    if trigger == "schedule":
        next_run = _scheduler.compute_next_run(job.get("schedule") or {}, job.get("timezone")) \
            if job.get("enabled") else None
        if not repo.claim_due(job_id, next_run):
            return {"job_id": job_id, "status": "skipped", "reason": "already claimed or not due"}

    run_id = repo.start_run(job_id, trigger=trigger)
    if not run_id:
        return {"job_id": job_id, "status": "error", "error": "could not open a run row"}

    cancel_check = (lambda: repo.is_cancel_requested(job_id)) if job_id else None
    try:
        result = run_job(job, cancel_check=cancel_check)
        status = "cancelled" if result.get("cancelled") else "success"
        repo.finish_run(run_id, job_id, status=status, result=result, set_next=False)
        return {"job_id": job_id, "run_id": run_id, "status": status, "result": result}
    except Exception as exc:  # noqa: BLE001 — record, don't crash the batch
        log.warning("execute_and_record(%s) failed: %s", job_id, exc)
        repo.finish_run(run_id, job_id, status="error", error=str(exc), set_next=False)
        return {"job_id": job_id, "run_id": run_id, "status": "error", "error": str(exc)}


def run_due(limit: int = 25) -> dict[str, Any]:
    """Find every due job and run it. This is what the cron trigger calls.

    Each job is isolated — one failing job is recorded and the rest still run."""
    reaped = repo.reap_stale_runs()  # self-heal runs whose process died mid-run
    jobs = repo.due(limit=limit)
    results = [execute_and_record(j, trigger="schedule") for j in jobs]
    ran = [r for r in results if r.get("status") != "skipped"]
    return {"ran": len(ran), "skipped": len(results) - len(ran), "reaped": reaped, "results": results}


# ── In-process poller ───────────────────────────────────────────────────────
# Mirrors scheduler.start_background_poller: on an always-on process (local dev,
# a VM, Railway/Render) a daemon thread calls run_due() on an interval so jobs
# "just fire" without an external cron. OFF on Vercel serverless (see
# Config.scheduler_inprocess), where the vercel.json cron hits /api/jobs/run-due.
_poller_started = threading.Event()


def start_background_poller(interval_seconds: int = 60) -> None:
    """Start the in-process due-job poller once (idempotent)."""
    if _poller_started.is_set():
        return
    _poller_started.set()

    def _loop() -> None:
        time.sleep(min(interval_seconds, 20))  # let the app finish booting first
        while True:
            try:
                result = run_due()
                if result.get("ran"):
                    log.info("jobs: fired %d due job(s)", result["ran"])
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                log.warning("jobs poll failed: %s", exc)
            time.sleep(max(15, interval_seconds))

    threading.Thread(target=_loop, name="hermes-jobs", daemon=True).start()
    log.info("In-process jobs poller started (every %ds).", interval_seconds)
