# Scheduled Tasks — Full Context, Purpose & Design

This is the authoritative reference for the Scheduled Tasks feature. It's written
for the next person building on top of it: it explains *why* it exists, *how
Gandi uses it*, the *mental model*, the *end-to-end data flow*, every *design
decision and its rationale*, and the *extension points* — so you can change it
confidently without re-deriving the intent.

---

## 1. One line

**Scheduled Tasks let Hermes run itself on a schedule** — the same agent that
answers you in chat, but firing automatically (daily briefing, inbox triage, a
rent-arrears sweep) with a locked business, a chosen set of apps, and a fixed
prompt, instead of waiting for you to type.

---

## 2. Business context — who this is for and why

Hermes is the AI executive assistant for **Gandi**, a founder running a
multi-entity portfolio. Each entity has its own connected accounts (Gmail,
Google Calendar/Drive, QuickBooks, WATI/WhatsApp, Notion, Zoom) reached through
the **Maton** gateway, keyed per business:

| Slug   | Entity                         | Typical work |
|--------|--------------------------------|--------------|
| `afk`  | Afro Farm Holding (Kenya)      | Farm ops, 54 staff payroll, GLOBALG.A.P., market prices |
| `gak`  | GAK Properties LLC             | Landlord — leases, rent, tenants (720 E Lake) |
| `gif`  | GIF Properties LLC             | Landlord — leases, rent, tenants |
| `gakc` | GAK Commodities Ltd            | Trading/commodities |
| `nh`   | Nour Hammam LLC                | Spa pre-launch (buildout at 720 E Lake) |
| `per`  | Personal & Family             | Personal inbox, father's care, prayer/halal-aware scheduling |

Before this feature, everything Hermes did was **pull** — Gandi had to open the
app and ask. But a lot of his real work is **recurring and predictable**: "every
morning tell me what needs attention," "every Monday find out who owes rent,"
"every Friday summarize the week across entities." Asking for the same thing
every day is friction, and things fall through when he's busy or traveling
(Kenya Window calls, prayer times, multiple time zones).

**Scheduled Tasks flips the model to push:** Gandi describes a recurring job once,
and Hermes performs it on its own and leaves the result waiting for him.

This mirrors Claude's own "Scheduled tasks" UX (the screenshot that seeded this
work: *Daily Business Context File · Every day at 7:00 AM*, plus templates like
Monitor a topic, Inbox triage, Daily briefing, Meeting prep, Weekly review).

---

## 3. Purpose & goals

**Purpose:** turn Hermes from an on-demand assistant into a proactive one for the
predictable, repeating parts of running the portfolio.

**Goals (what "good" means here):**

1. **Zero-friction recurring work.** Define once in the UI; it just runs. No
   per-task infrastructure, no manual triggering.
2. **Full parity with chat.** A scheduled run has the *same* capabilities as a
   chat turn — same tools, same business scoping, same memory grounding — because
   it literally reuses the chat agent. Anything Gandi can ask in chat, he can
   schedule.
3. **Safe when unattended.** No human is watching to approve a send/delete, so a
   task must not do irreversible things unless Gandi explicitly authorized that
   specific task to.
4. **Scoped and auditable.** Each task is locked to one business and a chosen set
   of apps; every run is stored (reply + tool trace) and reviewable.
5. **Portable firing.** Works identically in local dev and on Vercel serverless.

**Non-goals (deliberately out of scope for v1):**

- Result *delivery* channels (email/Slack/WhatsApp of the output). Results live
  in run history; a task's prompt can ask the agent to email/file its output
  itself. See [Extension points](#12-extension-points).
- Task-to-task dependencies / DAGs.
- Per-task model selection (uses the global active model).
- Multi-user tenancy (single owner, like the rest of Hermes today).

---

## 4. How Gandi actually uses it (concrete)

These are the shapes of task the feature was designed around — they're also the
four built-in templates in the editor:

- **Daily briefing** (`afk`, Gmail+Calendar, read-only, every day 07:00) — "Scan
  Gmail and Calendar for today and write a short briefing: what's urgent, what's
  coming up, any bills/deadlines due."
- **Inbox triage** (Gmail, *draft*, weekdays 08:00) — "Group new emails by
  urgency and prepare draft replies to anything that needs one."
- **Rent arrears sweep** (`gak`/`gif`, QuickBooks+Gmail, read-only, Mon 09:00) —
  "Pull AR aging, list who's overdue/how much/how late, cross-check email for
  payment promises."
- **Weekly review** (Gmail+Calendar+QuickBooks, read-only, Fri 16:00) —
  "Cash position, arrears, this/next week's calendar, open items; end with the
  top 3 things for next week."

The common thread: **read the connected accounts, synthesize, report; sometimes
draft; occasionally act — but only when told to.**

---

## 5. Mental model

> **A scheduled task is a saved chat turn plus a clock.**

A task carries exactly the inputs a chat turn needs, plus *when* to run:

| Input        | In chat this is…                | In a task |
|--------------|----------------------------------|-----------|
| **Prompt**   | what you type                    | `prompt` |
| **Business** | the business dropdown (lock)     | `entity_id` (or NULL = "All Businesses") |
| **Apps/tools** | all connected apps             | `allowed_apps` (subset; empty = all) |
| **When**     | now (you pressed enter)          | `schedule` + `timezone` → `next_run_at` |
| **Autonomy** | you're present to approve writes | `autonomy` (readonly / draft / full) — the substitute for your presence |

When a task fires, it builds a prompt (autonomy guardrail + the task prompt) and
calls the **exact same** `run_turn()` the chat uses, scoped to the business and
restricted to the allowed apps. The result is stored as a *run*.

---

## 6. End-to-end data flow

```
        CREATE (UI or POST /api/scheduled-tasks)
          │  validate schedule + autonomy; resolve business slug -> entity_id
          │  compute_next_run(schedule, tz)  ─────────────► next_run_at (UTC)
          ▼
     scheduled_tasks row  (enabled, next_run_at, …)
          │
          │        ┌─────────────── FIRING (one of two, never both) ───────────────┐
          │        │  In-process poller (always-on hosts)   Vercel Cron (serverless)│
          │        │  thread loops every N s ──┐            GET /run-due every 15m ──┐│
          │        └───────────────────────────┼───────────────────────────────────┼┘
          ▼                                     ▼                                    ▼
     scheduler.run_due(limit)  ◄────────────────┴────────────────────────────────────
          │  repo.due(now)  → tasks where enabled AND next_run_at <= now
          ▼
     reap_stale_runs()  → close any run stuck 'running' (dead process) as 'error'
     for each due task:  scheduler.execute_and_record(task, trigger="schedule")
          │  next = compute_next_run(schedule, tz)          # advance from NOW
          │  repo.claim_due(task, next)  → atomic UPDATE next_run_at = next
          │       └─ no row claimed? another poll took it → return 'skipped'
          │  repo.start_run() → run row (status=running)
          │  result = run_task(task):
          │       build_run_prompt(task)  = autonomy guardrail + task prompt
          │       run_turn(session_key, prompt, active_business=biz,
          │                allowed_apps=apps)               # THE CHAT AGENT
          │       store.reset(session_key)                  # throwaway memory
          ▼
     repo.finish_run(status, reply, tool_calls, set_next=False)
          │  → scheduled_task_runs row filled in
          │  → scheduled_tasks.last_run_at/last_status set (next_run_at already
          │    advanced at claim time — finish_run doesn't touch it)
          ▼
     Review in UI (task detail modal → run history) or via GET /{id}/runs
```

**Manual "Run now"** takes the same path with `trigger="manual"`, except it
**skips the claim** entirely — it records the run but does **not** move the next
scheduled fire.

---

## 7. Firing model (why it's pull-based, and the two paths)

The app deploys to **Vercel serverless**, where functions are frozen between
requests — a long-lived scheduler thread wouldn't survive. So firing is
**pull-based**: something external asks "what's due?" and runs it. There are two
implementations, and **exactly one is active per deployment** so a task never
double-fires:

1. **In-process poller** — `scheduler.start_background_poller()` spawns a daemon
   thread that calls `run_due()` every `SCHEDULER_POLL_SECONDS` (default 60).
   Started from `app.py`'s lifespan. **ON by default on any always-on process**
   (local dev, a VM, Railway/Render, plain uvicorn).
2. **External cron** — a scheduler hits `GET|POST /api/scheduled-tasks/run-due`.
   `vercel.json` has a `crons` entry every 15 min. **Used on Vercel** (and any
   host where you'd rather not run the thread).

The selector is `Config.scheduler_inprocess()`: returns True unless the `VERCEL`
env var is present; force with `SCHEDULER_INPROCESS=true/false`.

**Firing resolution = the poll/cron interval.** A task set for 08:30 fires on the
first scan at/after 08:30 (so within ≤60 s locally, ≤15 min on Vercel).

**Securing run-due:** when `SCHEDULER_SECRET` or Vercel's `CRON_SECRET` is set,
the caller must present it (`Authorization: Bearer <secret>` or `?token=<secret>`).
Vercel Cron sends `CRON_SECRET` automatically. Unset = open (local dev only).

---

## 8. Safety model — autonomy (the most important design point)

A scheduled run has **no human in the loop** to approve a write. The normal chat
agent is told to "summarize the write and stop for confirmation" before sending
an email, modifying QuickBooks, etc. That confirmation can't happen unattended,
so each task declares how far it may go, and the level is injected as a **hard
guardrail prepended to the prompt every run** (`scheduler.build_run_prompt` +
`_AUTONOMY_GUARDRAIL`):

| Level        | May do | Must not |
|--------------|--------|----------|
| `readonly` (default) | read, search, analyze, report, `create_file` | send/modify/delete anything |
| `draft`      | readonly + create **Gmail drafts** | actually send; modify/delete |
| `full`       | send/modify as the task requires | anything beyond the task's instructions |

Rationale:
- **Default is `readonly`** because the highest-value, lowest-risk tasks
  (briefings, sweeps, reviews) are pure read+report, and a wrong autonomous send
  is the worst failure mode.
- **`full` is an explicit, per-task standing authorization.** Choosing it *is*
  Gandi pre-approving that specific recurring action (e.g. "every Friday email
  the tenant list the rent reminder"). It's the only path where the agent acts
  unattended, and the guardrail tells it to stay strictly within the task.
- This composes with the platform's instruction-source boundary: a task's prompt
  is a *user instruction* (Gandi wrote it in the UI), not untrusted content.

> When extending autonomy (e.g. an "approve queue"), keep the invariant:
> **nothing irreversible happens unattended unless the task's own config says so.**

---

## 9. Data model

Migration: `hermes/db/migrations/0012_scheduled_tasks.sql`.

### `scheduled_tasks`
| Column | Type | Notes |
|--------|------|-------|
| `task_id` | uuid PK | |
| `user_id` | uuid | owner (same single-owner resolution as the rest of the app) |
| `entity_id` | uuid NULL | the locked business; **NULL = "All Businesses"** |
| `name` | text | display name |
| `prompt` | text | the instruction, written as you'd ask in chat |
| `allowed_apps` | jsonb | array of Maton slugs (+ pseudo-slug `"web"`); `[]` = all apps |
| `autonomy` | text | `readonly` \| `draft` \| `full` (CHECK-constrained) |
| `schedule` | jsonb | `{frequency, times:["HH:MM"], days_of_week:[0-6]}` |
| `timezone` | text | IANA tz; `times` are wall-clock in it |
| `enabled` | bool | paused tasks have `next_run_at = NULL` and never fire |
| `next_run_at` | timestamptz | resolved UTC instant of the next fire; the due-scan filters on it |
| `last_run_at`, `last_status` | | stamped after each run |
| `created_at`, `updated_at` | | |

Indexes: partial `(next_run_at) WHERE enabled` (the hot due-scan), plus user and
entity indexes.

### `scheduled_task_runs`
| Column | Type | Notes |
|--------|------|-------|
| `run_id` | uuid PK | |
| `task_id` | uuid FK | ON DELETE CASCADE (delete task → its runs go too) |
| `trigger` | text | `schedule` (cron/poller) \| `manual` (Run now) |
| `status` | text | `running` → `success` \| `error` |
| `reply` | text | the agent's final text |
| `tool_calls` | jsonb | the tool trace (same shape chat stores) |
| `error` | text | set when `status = error` |
| `started_at`, `finished_at` | | |

**Schedule JSON semantics** (`hermes/scheduler.py`):
- `frequency`: `daily` (every day at each time), `weekdays` (Mon–Fri), `weekly`
  (only `days_of_week`), `hourly` (every hour at the minute of `times[0]`).
- `days_of_week`: **0 = Monday … 6 = Sunday** (Python `datetime.weekday()`); the
  frontend builds its Mon–Sun chips against the same numbering.
- `times`: `["07:00","13:00","19:00"]` → a 3×/day task.

---

## 10. Code map

| File | Responsibility |
|------|----------------|
| `hermes/db/migrations/0012_scheduled_tasks.sql` | the two tables + indexes |
| `hermes/db/scheduled_tasks.py` | **storage only** — `create_task`, `get_task`, `list_tasks`, `update_task`, `delete_task`, `due`, `claim_due` (atomic claim that advances `next_run_at` up front), `reap_stale_runs` (close dead-process runs), `start_run`, `finish_run`, `list_runs`. Mirrors `hermes/db/meetings.py`'s owner/entity resolution and graceful-degradation style. Never runs the agent. |
| `hermes/scheduler.py` | **when + what** — schedule math (`compute_next_run`, `describe_schedule`), autonomy guardrails (`build_run_prompt`), the run (`run_task`), the record-and-advance orchestration (`execute_and_record`, `run_due`), and the in-process poller (`start_background_poller`). Also exports `VALID_FREQUENCIES`, `VALID_AUTONOMY`, `DEFAULT_TZ`. |
| `hermes/tools.py` | `build_tools(active_business, allowed_apps=…)` narrows the toolset; `_restrict_tools` + `_TOOL_REQUIRED_APPS` drop app-gated tools whose app isn't allowed; `maton_request`'s app enum is hard-narrowed. |
| `hermes/agent.py` | `run_turn` / `stream_turn` gained an `allowed_apps` param (threaded to `build_tools`); everything else identical to chat. |
| `hermes/config.py` | `SCHEDULER_SECRET`, `SCHEDULER_DEFAULT_TZ`, `SCHEDULER_POLL_SECONDS`, `scheduler_inprocess()`. |
| `app.py` | the `/api/scheduled-tasks/*` endpoints + starts the poller in the lifespan. |
| `static/tasks.js` | the whole Tasks UI (list, editor, schedule builder, app picker, autonomy, detail modal, run history). Exposes `window._tasksInit`. |
| `templates/index.html` | the `Tasks` strip-tab, `#panel-tasks` sidebar, `#tasks-view` main, and the two modals. |
| `static/style.css` | the Scheduled Tasks CSS block (`.tk-*`) + the `data-view="tasks"` display rule. |
| `vercel.json` | the `crons` entry. |
| `SCHEDULED_TASKS.md` | this document. |

**Layering rule to preserve:** `db/scheduled_tasks.py` is pure storage;
`scheduler.py` owns time + the agent call; `app.py` is transport/validation. Keep
the agent out of the DB layer and the DB out of the scheduler's math functions.

---

## 11. API reference

Base: `/api/scheduled-tasks`. All JSON. `entity_id`/`business` are **business
slugs** (`afk`, `gak`, …) or `"all"`, matching the chat convention.

| Method & path | Purpose | Body / notes |
|---------------|---------|--------------|
| `GET /` | list tasks | `?entity_id=<slug|all>`; each task carries a `schedule_summary` |
| `POST /` | create | `{name, prompt, business, allowed_apps[], autonomy, schedule{frequency,times,days_of_week}, timezone, enabled}` → computes `next_run_at` |
| `GET /{id}` | task + recent runs | returns `{task, runs}` |
| `PATCH /{id}` | edit | only sent fields change; any change to schedule/timezone/enabled recomputes `next_run_at` |
| `DELETE /{id}` | hard delete | cascades to runs |
| `GET /{id}/runs` | run history | `?limit=` |
| `POST /{id}/run` | **Run now** | manual trigger; blocks until the agent finishes; does not move `next_run_at` |
| `GET\|POST /run-due` | **cron trigger** | runs all due tasks; secured by `SCHEDULER_SECRET`/`CRON_SECRET` |

Validation lives in `app.py` (`_validate_schedule`, autonomy/name/prompt checks).

---

## 12. Extension points

Where to build next, and how the current design accommodates it:

- **Result delivery.** Today results sit in run history; a task's prompt can tell
  the agent to email/file its own output. To add first-class delivery, add a
  `deliver_to` config (email/Notion/WhatsApp) and, after `run_task`, hand the
  reply to a sender. Keep it separate from the agent run so a delivery failure
  doesn't lose the result (it's already persisted).
- **Notifications on run.** `finish_run` is the single choke point — emit an
  event there (webhook, push) when `status` flips.
- **Retries / backoff.** `execute_and_record` catches failures and advances the
  schedule so a poison task can't spin. To retry, add attempt tracking on the run
  and re-enqueue by setting `next_run_at` to a short delay instead of the next
  scheduled slot.
- **New frequencies** (e.g. "every N hours", "monthly", cron expressions). Add to
  `VALID_FREQUENCIES`, extend `compute_next_run` and `describe_schedule`, and add
  a tab in the editor's `#tk-freq-tabs`. Keep the `{schedule}` JSON shape.
- **Per-task model / effort.** `run_task` calls `run_turn`, which reads the global
  active model. Thread a `model` through `run_turn`/`build_tools` and store it on
  the task if some tasks want a cheaper/faster model.
- **Approval queue** (a middle ground above `draft`). Have `full` writes go to a
  pending table for one-tap approval instead of executing — preserves the
  "nothing irreversible unattended" invariant while enabling action.
- **Task chaining / dependencies.** Model as `depends_on` and gate `due()` on the
  upstream task's last successful run.

---

## 13. Known limitations & gotchas

- **Double-fire is guarded by an atomic claim.** A scheduled fire now goes
  through `repo.claim_due(task_id, next_run)` *before* the agent runs: a single
  `UPDATE … SET next_run_at = <next>, last_status='running' WHERE task_id = …
  AND enabled AND next_run_at <= now() RETURNING …` both advances `next_run_at`
  up front and returns a row only if it actually changed it. `execute_and_record`
  runs the task only if it won the claim, else returns `status='skipped'`. This
  fixes two things: (1) the **re-entrancy** case — the agent run takes minutes,
  and previously `next_run_at` wasn't moved until `finish_run` afterward, so
  every poll in that window re-selected the still-due task and fired it again
  (single poller, no multi-worker needed); (2) **concurrent pollers** — only the
  first UPDATE matches `next_run_at <= now`, the rest get no row. `finish_run`
  now records the outcome only and never touches `next_run_at` (the claim owns
  it). Manual "Run now" never claims (it must not move the schedule).
- **Orphaned 'running' runs self-heal.** If a process dies mid-run, `finish_run`
  never fires and the run would show 'running' forever. `run_due` calls
  `repo.reap_stale_runs()` first, flipping any run stuck 'running' past a cutoff
  (default 15 min) to 'error'. On the pure-cron (Vercel) path this runs every
  trigger; if you ever disable both firing paths, nothing reaps.
- **Firing precision = interval.** Not to-the-second; a task fires at the first
  scan after its time. Tighten `SCHEDULER_POLL_SECONDS` / the cron cadence if
  needed (Vercel sub-daily crons require a Pro plan).
- **Timezone display.** The task's `times` are in its own `timezone`; the UI's
  "Next run" is rendered in the **viewer's** browser timezone (via
  `toLocaleString`). That's intentional but can look off if you forget which zone
  you're reading. The *firing* always honors the task's timezone.
- **DST transitions.** `compute_next_run` builds aware datetimes at wall-clock
  times; the rare ambiguous/nonexistent hour during a DST switch isn't specially
  handled. Acceptable for daily/weekly business tasks; revisit if you add
  minute-precise schedules.
- **Unattended writes are real.** `full` autonomy will send/modify without asking.
  That's the point, but it's the sharpest edge — surface it clearly in any UI
  changes (the editor labels it "act on my behalf").
- **Scheduled runs don't touch chat history or memory.** `run_task` uses a
  throwaway session key and resets it, and does not run memory extraction. If you
  want tasks to *write* to long-term memory, wire it explicitly.

---

## 14. Setup / operations (quick)

1. Apply the migration once after deploy: `POST /api/admin/migrate` (the DB is
   only reachable from inside the platform's network, so this is the working path
   — not a local `init_db`).
2. Set `SCHEDULER_SECRET` (or, on Vercel, `CRON_SECRET`) so `run-due` is
   protected. On Vercel the `crons` entry then fires it every 15 min.
3. On any always-on host, the in-process poller runs automatically — nothing to
   configure. Tune with `SCHEDULER_POLL_SECONDS`; force with `SCHEDULER_INPROCESS`.
4. Create tasks in the **Tasks** tab (⏰). **Run now** tests any task instantly.

---

## 15. Verification done at build time

- Schedule math for daily/weekdays/weekly/hourly, timezone-aware, DST-region tz.
- Tool restriction: a Gmail+Calendar task drops Zoom/WATI and narrows
  `maton_request`'s app enum; app-agnostic tools stay.
- Full CRUD, detail modal, run history, enable/disable (PATCH recomputes
  `next_run_at`), against the real DB.
- `run-due` auth both ways (401 unauthenticated / clean run authenticated).
- **Live end-to-end:** an inserted due task was fired *by the running server's
  in-process poller* with no manual trigger — real agent run recorded
  (`trigger=schedule`, `status=success`), `next_run_at` advanced. Test data
  cleaned up.
