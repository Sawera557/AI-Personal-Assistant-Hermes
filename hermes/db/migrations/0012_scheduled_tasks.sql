-- Hermes — 0012: Scheduled Tasks.
--
-- Claude-style recurring, unattended agent runs. Each task carries everything a
-- normal chat turn needs — a locked business (entity_id), a prompt, and a
-- restricted set of connected apps it may use — plus a schedule that decides
-- WHEN it fires and an autonomy level that decides how far it may act without a
-- human in the loop.
--
-- Firing is pull-based, not a background thread (the app runs serverless): a
-- single secured trigger (POST /api/scheduled-tasks/run-due, hit by a cron)
-- finds every task whose next_run_at <= now, runs it, records the run, and
-- recomputes next_run_at. That keeps scheduling dynamic per-task with one fixed
-- external trigger, and works identically locally and on Vercel.

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- NULL entity_id == "All Businesses" scope (agent picks per call), mirroring
    -- how chat treats an unlocked conversation.
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,

    name          text NOT NULL,
    prompt        text NOT NULL,

    -- Which connected apps this task may use. A JSON array of Maton app slugs
    -- (e.g. ["google-mail","quickbooks"]). Empty array / NULL == no restriction
    -- (every enabled app), same default as a normal chat turn.
    allowed_apps  jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- How far the task may act unattended:
    --   'readonly' — read / analyze / report + generate files only (default)
    --   'draft'    — readonly + may create Gmail drafts (never send)
    --   'full'     — may send / modify / delete (explicit standing authorization)
    autonomy      text NOT NULL DEFAULT 'readonly'
                   CHECK (autonomy IN ('readonly', 'draft', 'full')),

    -- Structured schedule: {frequency, times:["HH:MM",...], days_of_week:[0-6]}.
    -- times are wall-clock in `timezone`; next_run_at is the resolved UTC instant.
    schedule      jsonb NOT NULL,
    timezone      text NOT NULL DEFAULT 'America/Chicago',

    enabled       boolean NOT NULL DEFAULT true,
    next_run_at   timestamptz,       -- NULL when disabled or nothing scheduled
    last_run_at   timestamptz,
    last_status   text,              -- 'success' | 'error' | 'running' | NULL

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- The due-scan filters on (enabled, next_run_at) every trigger, so index it.
CREATE INDEX IF NOT EXISTS scheduled_tasks_due_idx
    ON scheduled_tasks (next_run_at)
    WHERE enabled;
CREATE INDEX IF NOT EXISTS scheduled_tasks_user_idx
    ON scheduled_tasks (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS scheduled_tasks_entity_idx
    ON scheduled_tasks (entity_id);


CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      uuid NOT NULL REFERENCES scheduled_tasks (task_id) ON DELETE CASCADE,

    -- 'manual' when a user pressed Run now, 'schedule' when the cron fired it.
    trigger      text NOT NULL DEFAULT 'schedule',
    status       text NOT NULL DEFAULT 'running',   -- running | success | error

    reply        text,           -- the agent's final text output
    tool_calls   jsonb,          -- the tool trace, same shape chat stores
    error        text,           -- populated when status = 'error'

    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE INDEX IF NOT EXISTS scheduled_task_runs_task_idx
    ON scheduled_task_runs (task_id, started_at DESC);
