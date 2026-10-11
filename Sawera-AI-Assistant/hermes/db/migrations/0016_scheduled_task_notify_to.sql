-- Hermes — 0016: Scheduled Task "notify to" recipient(s).
--
-- Adds an optional per-task delivery address so a task's result (and any files
-- it produced) is emailed straight to a recipient when the task runs — the
-- platform-level "send the results to this inbox" that used to require baking
-- a send into the prompt (and thus `full`/`draft` autonomy).
--
--   notify_to  comma-separated email address(es); NULL/empty = deliver nothing.
--
-- Delivery is handled in code (hermes/notify.py, called from the scheduler right
-- after a run's outcome is persisted), NOT by the agent — so it bypasses the
-- prompt confirm-policy, and a `readonly` task can still have its result mailed.
-- The recipient here is a deliberate, human-typed address (or the owner's own
-- inbox for the daily report), so it does not need the outbound approval gate.

ALTER TABLE scheduled_tasks
    ADD COLUMN IF NOT EXISTS notify_to text;
