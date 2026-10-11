-- Stop the daily attention report from being duplicated on every boot.
--
-- app.py's lifespan calls notify.ensure_digest_task() on every process start.
-- It looks the task up by name and creates one if absent — idempotent for a
-- single sequential boot, but not against concurrent cold starts (Vercel runs
-- many), and not if the task is ever renamed. The live database had accumulated
-- twelve copies of it, six of them enabled and firing daily.
--
-- A partial unique index makes the race impossible at the storage layer: the
-- second concurrent INSERT fails, create_task already catches and returns None,
-- and ensure_digest_task's caller is wrapped in try/except. Scoped to this one
-- task name so ordinary user-created tasks may still share a name.

CREATE UNIQUE INDEX IF NOT EXISTS scheduled_tasks_digest_uniq
    ON scheduled_tasks (user_id)
    WHERE name = 'Daily attention report';
