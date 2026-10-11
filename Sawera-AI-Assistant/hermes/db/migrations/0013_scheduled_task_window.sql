-- Hermes — 0013: Scheduled Task date-window (backfill) support.
--
-- Adds an optional per-task DATE WINDOW so a task can process a bounded date
-- range ONE DAY PER RUN, advancing a cursor each fire — the missing piece for
-- "read one day's messages per run, batch a report, march across June 17→July 17".
--
-- The base scheduled-tasks feature (0012) fires the SAME prompt every run with a
-- throwaway session and no cross-run state, so it could never sequence "day N on
-- run N". These columns add that state:
--
--   window_start   first day to process (inclusive)
--   window_end     last day to process (inclusive); NULL = open-ended (forward)
--   window_cursor  the NEXT day this task will process; advances by window_step
--                  after each successful claim. Initialized to window_start.
--   window_step    days to advance per run (default 1 = one day per run)
--   window_pace    NULL      → no window (an ordinary recurring task, unchanged)
--                  'catchup' → drain the range as fast as the poller ticks
--                              (next_run_at re-armed to now until exhausted) —
--                              a 31-day backfill finishes in minutes, not weeks
--                  'scheduled' → process one day per normal scheduled fire
--
-- When the cursor passes window_end the task stops firing (next_run_at → NULL)
-- but stays enabled, so its run history and last result remain visible.
--
-- The firing path (claim_due_window in hermes/db/scheduled_tasks.py) advances the
-- cursor atomically inside the same claim that guards against multi-fire, and the
-- claimed day is injected into the run prompt as a hard "process only this day"
-- guardrail (hermes/scheduler.py build_run_prompt).

ALTER TABLE scheduled_tasks
    ADD COLUMN IF NOT EXISTS window_start  date,
    ADD COLUMN IF NOT EXISTS window_end    date,
    ADD COLUMN IF NOT EXISTS window_cursor date,
    ADD COLUMN IF NOT EXISTS window_step   integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS window_pace   text
        CHECK (window_pace IS NULL OR window_pace IN ('catchup', 'scheduled'));
