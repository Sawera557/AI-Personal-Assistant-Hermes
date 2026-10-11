-- Hermes — 0015: Jobs "allow multiple runs per day" flag.
--
-- By default a job runs at most once per calendar day (in its own timezone):
-- if it already has a successful/in-progress run today, another fire (manual or
-- scheduled) is skipped. Set allow_multiple_runs = true to lift that guard for a
-- job that legitimately runs several times a day.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS allow_multiple_runs boolean NOT NULL DEFAULT false;
