# Scheduled Tasks

Claude-style recurring, unattended Hermes runs. A task is the *same* agent turn
as chat — a locked business, an allowed-apps toolset, and a prompt — but it runs
on a schedule instead of when you type. Use it for a daily briefing, weekday
inbox triage, a Monday rent-arrears sweep, a Friday weekly review, etc.

Open it from the **Tasks** tab (⏰) in the left rail.

## What a task carries

| Field        | Meaning |
|--------------|---------|
| **Prompt**   | What Hermes should do each run — written exactly as you'd ask in chat. |
| **Business** | Locks every tool call to that entity's connected account (or "All Businesses"). |
| **Apps**     | Which connected apps the run may use (Gmail, QuickBooks, Calendar, …). None selected = all apps. The toolset is hard-restricted to this list. |
| **Schedule** | `daily` / `weekdays` / `weekly` (pick days) / `hourly`, at one or more wall-clock times, in the task's timezone. |
| **Autonomy** | How far it may act unattended (see below). |

## Autonomy — the safety model

A scheduled run has no human to approve a write, so the default is safe:

- **Read-only** (default) — reads, analyzes, reports, and can save a file. Never
  sends or changes anything.
- **Draft** — read-only plus creating Gmail drafts for you to review and send.
- **Full** — may send/modify unattended. This is your explicit standing
  authorization for that specific recurring task; only pick it for tasks you
  fully trust.

The autonomy level is injected as a hard guardrail ahead of the task prompt on
every run (see `hermes/scheduler.py::build_run_prompt`).

## How firing works (pull-based)

A task created in the UI fires on its own — you don't wire anything per task.
There are two firing paths, and exactly one is active per deployment:

- **In-process poller** (always-on hosts: local dev, a VM, Railway/Render, plain
  uvicorn). The app runs a background thread that scans for due tasks every
  `SCHEDULER_POLL_SECONDS` (default 60) and runs them. ON by default everywhere
  except Vercel. This is why a task "just works" the moment you create it locally.
- **External cron** (Vercel serverless). A background thread wouldn't survive
  between frozen invocations, so instead a cron pings
  **`/api/scheduled-tasks/run-due`**; `vercel.json` already has that entry
  (every 15 min). Auto-selected when the `VERCEL` env var is present.

Either way the mechanics are the same: each task stores a resolved `next_run_at`
(UTC); when it passes, the task runs, the run (reply + tool trace) is recorded in
`scheduled_task_runs`, and `next_run_at` advances. The poll/cron interval is the
firing *resolution* — a task set for 08:30 fires on the first scan at/after 08:30.
`SCHEDULER_INPROCESS=true/false` forces the poller on or off.

### Wiring the cron

- **Vercel** — `vercel.json` already has a `crons` entry hitting `run-due` every
  15 minutes. Set the `CRON_SECRET` env var; Vercel sends it as a Bearer token
  and the endpoint requires it. (Sub-daily crons need a Vercel Pro plan; on
  Hobby, use an external cron instead.)
- **Anything else** (cron-job.org, GitHub Actions, a box with `cron`, a phone
  shortcut) — hit `POST https://<host>/api/scheduled-tasks/run-due?token=<secret>`
  on your interval, where `<secret>` is `SCHEDULER_SECRET` (or `CRON_SECRET`).

If neither secret is set the endpoint is open — fine for local dev only.

**Run now** on any task triggers it immediately for testing, without touching its
next scheduled fire.

## Files

- `hermes/db/migrations/0012_scheduled_tasks.sql` — `scheduled_tasks` + `scheduled_task_runs`
- `hermes/db/scheduled_tasks.py` — storage (CRUD, due-scan, run recording)
- `hermes/scheduler.py` — schedule math (`compute_next_run`, `describe_schedule`), autonomy guardrails, and the run/record orchestration
- `hermes/tools.py::build_tools(allowed_apps=…)` — the per-task tool restriction
- `app.py` — the `/api/scheduled-tasks/*` endpoints
- `static/tasks.js`, the `#tasks-view` markup in `templates/index.html`, and the Scheduled Tasks CSS block in `static/style.css` — the UI

Apply the migration after deploy with `POST /api/admin/migrate`.
