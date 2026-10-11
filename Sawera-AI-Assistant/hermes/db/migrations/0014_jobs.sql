-- Hermes — 0014: Jobs (code-driven cron jobs).
--
-- The "Jobs" tab. Unlike Scheduled Tasks (0012), which run an agent PROMPT, a
-- Job runs a registered Python FUNCTION (hermes/job_runner.py REGISTRY) — the
-- first is the Google-reviews scraper (hermes/reviews_scraper.py). The firing
-- model is identical to scheduled tasks: pull-based, one secured trigger
-- (POST /api/jobs/run-due) finds every job whose next_run_at <= now, runs it,
-- records the run, and rolls next_run_at forward. Start = run now; Stop = set
-- cancel_requested, which the job function polls between steps.
--
-- Jobs are system-wide crons (not business-scoped like tasks), so user_id is
-- optional/nullable and there is no entity_id.

CREATE TABLE IF NOT EXISTS jobs (
    job_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Optional owner; the seeded system job leaves this NULL.
    user_id          uuid REFERENCES users (user_id) ON DELETE SET NULL,

    -- Registry key → the Python callable that runs (job_runner.REGISTRY).
    job_type         text NOT NULL,
    name             text NOT NULL,

    -- Free-form knobs handed to the job function (e.g. the scraper's
    -- {full_sweep, places_per_bucket, reviews_per_tier, buckets}).
    config           jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Same structured schedule shape as scheduled_tasks:
    -- {frequency, times:["HH:MM"], days_of_week:[0-6]}. next_run_at is the
    -- resolved UTC instant the due-scan filters on.
    schedule         jsonb NOT NULL DEFAULT '{}'::jsonb,
    timezone         text NOT NULL DEFAULT 'America/Chicago',

    enabled          boolean NOT NULL DEFAULT true,
    next_run_at      timestamptz,   -- NULL when disabled or nothing scheduled
    last_run_at      timestamptz,
    last_status      text,          -- 'running' | 'success' | 'error' | 'cancelled' | NULL

    -- Set true by a Stop request; the running job function polls this and aborts
    -- cleanly. Cleared when a run starts.
    cancel_requested boolean NOT NULL DEFAULT false,

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- The due-scan filters on (enabled, next_run_at) every trigger, so index it.
CREATE INDEX IF NOT EXISTS jobs_due_idx
    ON jobs (next_run_at)
    WHERE enabled;


CREATE TABLE IF NOT EXISTS job_runs (
    run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       uuid NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,

    -- 'manual' when a user pressed Start, 'schedule' when the cron fired it.
    trigger      text NOT NULL DEFAULT 'schedule'
                 CHECK (trigger IN ('manual', 'schedule')),
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'success', 'error', 'cancelled')),

    -- The job function's summary (counts, s3_uri, searches_used, …).
    result       jsonb,
    error        text,           -- populated when status = 'error'

    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE INDEX IF NOT EXISTS job_runs_job_idx
    ON job_runs (job_id, started_at DESC);


-- Seed the first job: the Google-reviews scraper. Idempotent (only if absent).
-- next_run_at = now() makes it due on the first cron tick — that first run
-- validates the wiring, then the runner rolls it forward to the daily 07:00 slot.
INSERT INTO jobs (job_type, name, config, schedule, timezone, enabled, next_run_at)
SELECT
    'google_reviews_scraper',
    'Google Reviews Scraper (hammams & spas)',
    '{}'::jsonb,
    '{"frequency": "daily", "times": ["07:00"], "days_of_week": []}'::jsonb,
    'America/Chicago',
    true,
    now()
WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE job_type = 'google_reviews_scraper');
