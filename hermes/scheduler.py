"""Scheduled Tasks — the schedule math and the unattended agent run.

Two responsibilities, deliberately separate from storage (hermes/db/scheduled_tasks.py):

* **When** — `compute_next_run` turns a task's structured schedule
  ({frequency, times, days_of_week} + a timezone) into the next concrete UTC
  instant it should fire, and `describe_schedule` turns the same into the
  human line the UI shows ("Every day at 7:00 AM").
* **What** — `run_task` executes one run through the SAME Hermes agent the chat
  uses (`run_turn`), with the task's business locked, its allowed apps as the
  only tools, and an autonomy guardrail prepended so an unattended run can't
  send/modify anything the user didn't explicitly authorize.

Firing is pull-based: something (a cron hitting /api/scheduled-tasks/run-due, or
a manual "Run now") asks the DB layer for due tasks and calls `run_task` on each.
Nothing here runs a background thread — that wouldn't survive on serverless.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("hermes.scheduler")

# days_of_week convention: 0 = Monday … 6 = Sunday (Python's datetime.weekday()).
# The frontend builds its Mon–Sun chips against the same numbering.
_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon–Fri, for the "weekdays" frequency
_ALL_DAYS = {0, 1, 2, 3, 4, 5, 6}

DEFAULT_TZ = "America/Chicago"
VALID_FREQUENCIES = {"hourly", "daily", "weekdays", "weekly"}
VALID_AUTONOMY = {"readonly", "draft", "full"}
# A task may carry an optional DATE WINDOW so it processes a bounded range one day
# per run (see hermes/db/migrations/0013). The pace controls how fast it advances:
#   catchup   — re-arm next_run_at to now after each run, so a backfill of a past
#               range drains as fast as the poller ticks (minutes, not weeks).
#   scheduled — advance one day per normal scheduled fire.
VALID_WINDOW_PACES = {"catchup", "scheduled"}

# How far ahead compute_next_run is willing to search for a matching day before
# giving up (guards against an empty days_of_week producing an infinite loop).
_MAX_LOOKAHEAD_DAYS = 14


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _parse_times(schedule: dict[str, Any]) -> list[tuple[int, int]]:
    """Parse the schedule's ["HH:MM", ...] into sorted (hour, minute) tuples.

    Falls back to a single 09:00 run if none are valid, so a malformed schedule
    still fires once a day rather than never."""
    out: list[tuple[int, int]] = []
    for raw in schedule.get("times") or []:
        try:
            h_str, m_str = str(raw).split(":", 1)
            h, m = int(h_str), int(m_str)
        except (ValueError, AttributeError):
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append((h, m))
    if not out:
        out = [(9, 0)]
    return sorted(set(out))


def _allowed_weekdays(schedule: dict[str, Any]) -> set[int]:
    freq = schedule.get("frequency")
    if freq == "weekdays":
        return set(_WEEKDAYS)
    if freq == "weekly":
        days = {int(d) for d in (schedule.get("days_of_week") or []) if 0 <= int(d) <= 6}
        return days or set(_ALL_DAYS)
    return set(_ALL_DAYS)  # daily (hourly is handled separately)


def compute_next_run(
    schedule: dict[str, Any],
    tz_name: str | None = DEFAULT_TZ,
    after: datetime | None = None,
) -> datetime | None:
    """The next UTC instant this schedule should fire, strictly after `after`.

    `after` defaults to now (UTC). Returns None only if the schedule is
    structurally incapable of ever firing (shouldn't happen — `_parse_times`
    and `_allowed_weekdays` both fall back to non-empty defaults)."""
    tz = _tz(tz_name)
    after_utc = after or datetime.now(timezone.utc)
    if after_utc.tzinfo is None:
        after_utc = after_utc.replace(tzinfo=timezone.utc)
    now_local = after_utc.astimezone(tz)

    freq = schedule.get("frequency", "daily")

    if freq == "hourly":
        # Fire every hour at a fixed minute (taken from the first time, else :00).
        times = _parse_times(schedule)
        minute = times[0][1]
        candidate = now_local.replace(minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(hours=1)
        return candidate.astimezone(timezone.utc)

    times = _parse_times(schedule)
    allowed = _allowed_weekdays(schedule)

    for offset in range(_MAX_LOOKAHEAD_DAYS + 1):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in allowed:
            continue
        for h, m in times:
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            if candidate > now_local:
                return candidate.astimezone(timezone.utc)
    return None


def _fmt_time(h: int, m: int) -> str:
    suffix = "AM" if h < 12 else "PM"
    hour12 = h % 12 or 12
    return f"{hour12}:{m:02d} {suffix}"


def describe_schedule(schedule: dict[str, Any], tz_name: str | None = None) -> str:
    """A short human line for the task card, e.g. 'Every day at 7:00 AM' or
    '3 times a day · 7:00 AM, 1:00 PM, 7:00 PM'."""
    freq = schedule.get("frequency", "daily")
    if freq == "hourly":
        times = _parse_times(schedule)
        return f"Every hour (at :{times[0][1]:02d})"

    times = _parse_times(schedule)
    time_str = ", ".join(_fmt_time(h, m) for h, m in times)

    if freq == "weekdays":
        when = "Weekdays"
    elif freq == "weekly":
        days = sorted({int(d) for d in (schedule.get("days_of_week") or []) if 0 <= int(d) <= 6})
        when = ", ".join(_WEEKDAY_LABELS[d] for d in days) if days else "Weekly"
    else:
        when = "Every day"

    if len(times) > 1 and freq == "daily":
        return f"{len(times)} times a day · {time_str}"
    return f"{when} at {time_str}"


# ── Date-window (backfill) math ───────────────────────────────────────────────
# A windowed task walks [window_start … window_end] one day per run. All the
# per-run bookkeeping is a pure function so the storage layer (claim_due_window)
# stays mechanical and this can be unit-tested without a database.

def _as_date(value: Any) -> date | None:
    """Coerce a date / ISO 'YYYY-MM-DD' string / None into a date (or None)."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value if isinstance(value, date) else None
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def advance_window(
    cursor_day: Any,
    end_day: Any,
    step_days: int,
    pace: str,
    now_utc: datetime,
    scheduled_next: datetime | None,
) -> tuple[date | None, date | None, datetime | None]:
    """Decide what a windowed task does on one fire.

    Given the current cursor (the day this run will process), returns
    ``(claimed_day, new_cursor, new_next_run_at)``:

    * ``claimed_day`` — the single day this run processes (== the cursor). None if
      there is no valid cursor (nothing to do).
    * ``new_cursor`` — the cursor advanced by ``step_days``.
    * ``new_next_run_at`` — when to fire next:
        - the range is exhausted (``new_cursor > end_day``) → ``None`` (stop);
        - pace ``catchup`` → ``now_utc`` (fire again on the next poll tick);
        - pace ``scheduled`` → ``scheduled_next`` (one day per normal fire).

    Keeping this pure means the DB layer only reads/writes columns and the
    "what happens next" decision lives here with the rest of the schedule math.
    """
    cursor = _as_date(cursor_day)
    if cursor is None:
        return None, None, None
    end = _as_date(end_day)
    step = max(1, int(step_days or 1))

    new_cursor = cursor + timedelta(days=step)
    if end is not None and new_cursor > end:
        new_next = None                      # backfill complete — stop firing
    elif pace == "catchup":
        new_next = now_utc                   # drain as fast as the poller ticks
    else:
        new_next = scheduled_next            # one day per scheduled fire
    return cursor, new_cursor, new_next


def window_batch_position(task: dict[str, Any], day: date | None) -> tuple[int, int]:
    """(batch_number, total_batches) for the given day within the task's window,
    for a human "batch N of M" line. Returns (0, 0) when it can't be computed."""
    start = _as_date(task.get("window_start"))
    end = _as_date(task.get("window_end"))
    step = max(1, int(task.get("window_step") or 1))
    if not start or not day:
        return 0, 0
    n = (day - start).days // step + 1
    total = ((end - start).days // step + 1) if end else 0
    return max(1, n), max(0, total)


def describe_window(task: dict[str, Any]) -> str | None:
    """A short human line for a windowed task's card, e.g.
    'Backfill Jun 17 → Jul 17 · 1 day/run · next: Jun 20 (12 left)'. None if the
    task has no window."""
    pace = task.get("window_pace")
    if not pace:
        return None
    start = _as_date(task.get("window_start"))
    end = _as_date(task.get("window_end"))
    cursor = _as_date(task.get("window_cursor"))
    step = max(1, int(task.get("window_step") or 1))

    def _lbl(d: date | None) -> str:  # portable "Jun 17" (no %-d — Windows-safe)
        return f"{d.strftime('%b')} {d.day}" if d else "?"

    span = _lbl(start) + (f" → {_lbl(end)}" if end else " → open-ended")
    per = "1 day/run" if step == 1 else f"{step} days/run"
    label = "Backfill" if pace == "catchup" else "Windowed"
    if end and cursor and cursor > end:
        return f"{label} {span} · {per} · complete"
    if cursor and end:
        left = max(0, (end - cursor).days // step + 1)
        return f"{label} {span} · {per} · next {_lbl(cursor)} ({left} left)"
    if cursor:
        return f"{label} {span} · {per} · next {_lbl(cursor)}"
    return f"{label} {span} · {per}"


def initial_next_run(
    schedule: dict[str, Any],
    tz_name: str | None,
    enabled: bool,
    window_pace: str | None = None,
) -> datetime | None:
    """The next_run_at to store when a task is created or its timing is edited.

    Disabled → None. A 'catchup' backfill fires immediately (now), so it starts
    draining the range on the very next poll instead of waiting for a scheduled
    slot. Everything else uses the normal schedule."""
    if not enabled:
        return None
    if window_pace == "catchup":
        return datetime.now(timezone.utc)
    return compute_next_run(schedule, tz_name)


# ── Autonomy guardrails prepended to the task prompt for an unattended run ──
# There is no human watching to approve a write, so the default is read-only.
# 'full' is an explicit, per-task standing authorization the user set when they
# created the task — the only case where the agent may send/modify unattended.
_AUTONOMY_GUARDRAIL = {
    "readonly": (
        "AUTONOMY: READ-ONLY. This is an unattended scheduled run — no human is "
        "watching to approve anything. You may ONLY read, search, analyze, and "
        "report, and you may use `create_file` to save a document. Do NOT send "
        "emails or messages, create/edit/delete calendar events, modify "
        "QuickBooks or WhatsApp or Drive data, or take any other write/destructive "
        "action. If the task seems to ask for one, do the analysis and clearly "
        "describe what you would send/change instead of doing it."
    ),
    "draft": (
        "AUTONOMY: DRAFT-ONLY. This is an unattended scheduled run. You may read, "
        "search, analyze, report, use `create_file`, and create Gmail DRAFTS "
        "(never send them). Do NOT actually send any email or message, and do NOT "
        "modify or delete calendar/QuickBooks/WhatsApp/Drive data. Leave everything "
        "ready for the user to review and send themselves."
    ),
    "full": (
        "AUTONOMY: FULL. The user explicitly authorized this recurring task to act "
        "on their behalf when they created it, so you may perform the writes this "
        "task requires (e.g. sending the email it describes) without pausing for "
        "confirmation — there is no human in the loop to confirm with. Stay strictly "
        "within what this task's instructions ask for; do nothing beyond them."
    ),
}


def _window_guardrail(task: dict[str, Any]) -> str | None:
    """When a run is scoped to one day of a date window, a hard 'process only this
    day' instruction to prepend — so a 31-day backfill produces one focused batch
    per run instead of the agent trying to read the whole range at once.

    The day is carried on the task dict as ``_window_day`` (an ISO date string),
    set by the firing path right after it claims the day from the cursor."""
    day = _as_date(task.get("_window_day"))
    if not day:
        return None
    tz_name = task.get("timezone") or DEFAULT_TZ
    n, total = window_batch_position(task, day)
    batch = f" (batch {n} of {total})" if total else f" (batch {n})"
    day_str = day.strftime("%A, %B %d, %Y")
    return (
        f"DATE WINDOW — SINGLE DAY{batch}. This run must process EXACTLY ONE day: "
        f"{day_str}, in the {tz_name} timezone (that day's 00:00–23:59 local). "
        f"Only read, count, and report on items dated on {day_str}; ignore anything "
        f"before or after it, even if you see it. This is one batch of a multi-day "
        f"backfill — later runs cover the other days, so do NOT try to cover the "
        f"whole range now. Title your output with this exact date so the batches "
        f"stay distinguishable."
    )


def build_run_prompt(task: dict[str, Any]) -> str:
    """The full instruction handed to the agent for one run: the autonomy
    guardrail, a note that this is an automated run, an optional single-day window
    guardrail, then the task's own prompt."""
    autonomy = task.get("autonomy") or "readonly"
    guardrail = _AUTONOMY_GUARDRAIL.get(autonomy, _AUTONOMY_GUARDRAIL["readonly"])
    today = datetime.now(_tz(task.get("timezone"))).strftime("%A, %B %d, %Y")
    window = _window_guardrail(task)
    parts = [
        f"[Automated scheduled task: \"{task.get('name') or 'Untitled task'}\" — "
        f"running now, {today}.]",
        guardrail,
    ]
    if window:
        parts.append(window)
    # D1: if this task is linked to a playbook, prepend the procedure.
    try:
        from .db.playbooks import playbook_for_task
        _pb = playbook_for_task(task.get("task_id"))
    except Exception:  # noqa: BLE001 — a playbook failure must never break a run
        _pb = None
    if _pb and _pb.get("content_md"):
        parts.append(
            "PLAYBOOK — follow this procedure exactly; deviate only if a step is "
            "impossible, and say so in the report:\n" + _pb["content_md"]
        )
    parts.append(f"Your task:\n{task.get('prompt') or ''}")
    return "\n\n".join(parts)


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    """Execute one run of a task through the Hermes agent and return its result
    dict ({reply, thinking, tool_calls, stop_reason}).

    Reuses `run_turn` — the exact agent loop the chat uses — with the task's
    business locked, its allowed apps as the only tools, and the autonomy
    guardrail prepended. Runs under a throwaway, per-run session key so it never
    shows up in the chat history sidebar or leaks into a real conversation's
    working memory.
    """
    from .agent import run_turn
    from .memory import store

    business = task.get("business") or "all"  # slug, or "all" for unscoped
    allowed_apps = task.get("allowed_apps") or None
    run_prompt = build_run_prompt(task)

    session_key = f"__scheduled__:{task.get('task_id')}"
    try:
        result = run_turn(
            session_key,
            [{"type": "text", "text": run_prompt}],
            voice=False,
            active_business=business,
            allowed_apps=allowed_apps,
            # Unattended run: no human to confirm with. The confirm-before-write
            # guardrail no-ops here; the task's autonomy tier (readonly/draft/full)
            # is the safety gate instead — see build_run_prompt / _AUTONOMY_GUARDRAIL.
            interactive=False,
        )
    finally:
        # Never let a scheduled run accumulate working memory across fires.
        store.reset(session_key)
    return result


def execute_and_record(task: dict[str, Any], trigger: str = "schedule") -> dict[str, Any]:
    """Run one task end-to-end and persist the outcome: open a run row, run the
    agent, close the run, and (for a scheduled fire) roll next_run_at forward.

    A `trigger='manual'` run records the run but leaves the next scheduled fire
    untouched. Returns a small summary ({task_id, run_id, status, reply?, error?}).
    Never raises — an agent failure is recorded as an 'error' run so the schedule
    keeps advancing instead of re-running a poison task every cron tick.

    For a scheduled fire we ATOMICALLY CLAIM the task first (advancing next_run_at
    to its next slot before the multi-minute agent run), so the same still-due
    task can't be re-selected and fired again by every poll in that window, and
    concurrent pollers can't both grab it. If another poll already claimed it we
    return a 'skipped' summary without running. A manual run never claims."""
    from .db import scheduled_tasks as repo

    task_id = task.get("task_id")
    windowed = bool(task.get("window_pace"))

    # Claim before running (scheduled only). This is the fix for the re-entrancy
    # multi-fire: next_run_at moves forward up front, atomically, guarding the
    # whole agent run — not after it finishes minutes later.
    if trigger == "schedule":
        next_run = compute_next_run(task.get("schedule") or {}, task.get("timezone")) \
            if task.get("enabled") else None
        if windowed:
            # A windowed fire also advances the date cursor atomically and tells us
            # which single day it claimed, so we can scope the run to that day.
            claimed_day = repo.claim_due_window(task_id, next_run)
            if not claimed_day:
                return {"task_id": task_id, "status": "skipped", "reason": "already claimed or window done"}
            task["_window_day"] = claimed_day.isoformat()
        elif not repo.claim_due(task_id, next_run):
            return {"task_id": task_id, "status": "skipped", "reason": "already claimed or not due"}
    elif windowed and task.get("window_cursor"):
        # Manual "Run now" on a windowed task: process the current pending day for
        # a test/kick, but do NOT advance the cursor (mirrors how manual runs never
        # move the schedule).
        task["_window_day"] = task["window_cursor"]

    run_id = repo.start_run(task_id, trigger=trigger)

    # next_run_at is already advanced (at claim, for scheduled) or intentionally
    # left alone (manual), so finish_run never touches it here.
    try:
        result = run_task(task)
        status = "error" if result.get("stop_reason") == "refusal" else "success"
        repo.finish_run(
            run_id, task_id,
            status=status,
            reply=result.get("reply"),
            tool_calls=result.get("tool_calls") or [],
            set_next=False,
        )
        # Deliver the result to any "notify to" address(es) on the task (and the
        # owner, for the daily attention report). Done AFTER the run is persisted
        # and guarded, so a send failure never loses the recorded outcome.
        try:
            from . import notify
            notify.deliver_run_result(task, run_id, result)
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            log.warning("deliver_run_result(%s) failed: %s", task_id, exc)
        return {"task_id": task_id, "run_id": run_id, "status": status, "reply": result.get("reply")}
    except Exception as exc:  # noqa: BLE001 — record, don't crash the whole batch
        log.warning("execute_and_record(%s) failed: %s", task_id, exc)
        repo.finish_run(run_id, task_id, status="error", error=str(exc), set_next=False)
        return {"task_id": task_id, "run_id": run_id, "status": "error", "error": str(exc)}


def run_due(limit: int = 25) -> dict[str, Any]:
    """Find every due task and run it. This is what the cron trigger calls.

    Returns a summary of what ran. Each task is isolated — one failing task is
    recorded and the rest still run."""
    from .db import scheduled_tasks as repo

    reaped = repo.reap_stale_runs()  # self-heal runs whose process died mid-run
    tasks = repo.due(limit=limit)
    results = [execute_and_record(t, trigger="schedule") for t in tasks]
    ran = [r for r in results if r.get("status") != "skipped"]
    return {"ran": len(ran), "skipped": len(results) - len(ran), "reaped": reaped, "results": results}


# ── In-process poller ───────────────────────────────────────────────────────
# So a task created in the UI "just fires" without any external cron, on any
# always-on process (local dev, a VM, Railway/Render, plain uvicorn). It's a
# daemon thread that calls run_due() on an interval. On Vercel serverless this
# is OFF (functions are frozen between requests — a thread wouldn't run) and the
# vercel.json cron hits /run-due instead, so tasks fire on exactly one path,
# never both. See Config.scheduler_inprocess().
_poller_started = threading.Event()


def start_background_poller(interval_seconds: int = 60) -> None:
    """Start the in-process due-task poller once (idempotent)."""
    if _poller_started.is_set():
        return
    _poller_started.set()

    def _loop() -> None:
        # A small initial delay lets the app finish booting (DB warmup) first.
        time.sleep(min(interval_seconds, 15))
        while True:
            try:
                result = run_due()
                if result.get("ran"):
                    log.info("scheduler: fired %d due task(s)", result["ran"])
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                log.warning("scheduler poll failed: %s", exc)
            time.sleep(max(15, interval_seconds))

    threading.Thread(target=_loop, name="hermes-scheduler", daemon=True).start()
    log.info("In-process scheduler started (every %ds).", interval_seconds)
