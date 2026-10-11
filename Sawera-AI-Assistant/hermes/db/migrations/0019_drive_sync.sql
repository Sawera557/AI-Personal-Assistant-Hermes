-- Hermes — 0019: Drive → S3 sync (lookup table + the sync Job).
--
-- A code-driven Job (hermes/drive_sync.py, registered in job_runner.REGISTRY as
-- 'drive_s3_sync') sweeps every connected business's Google Drive and copies any
-- file that isn't already in S3 into a category folder for navigation. This table
-- is the SYNC LEDGER: one row per (business, Drive file), so a re-run never copies
-- the same file twice or files it into two folders, and a file that CHANGED in
-- Drive re-syncs by overwriting its own deterministic S3 key.
--
-- Keyed on business_slug (the S3/Files scheme: afk, gak, gif, gakc, nh, per) to
-- match the drive-files/{business}/... prefixes, NOT entity_id. Additive; safe to
-- re-run.

CREATE TABLE IF NOT EXISTS drive_sync_files (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Tenant scope (S3/Files slug scheme) + the file's Drive identity.
    business_slug       text NOT NULL,
    drive_file_id       text NOT NULL,

    drive_name          text,
    mime_type           text,

    -- Change-detection: skip when both are unchanged since the last sync, else
    -- re-download and overwrite the same s3_key. md5Checksum is absent for
    -- Google-native (exported) files, so modifiedTime is the fallback signal.
    drive_modified_time timestamptz,
    drive_md5           text,

    -- LLM-assigned folder segment (Invoices, Contracts, …) and where the bytes landed.
    category            text,
    s3_key              text,
    s3_uri              text,
    size_bytes          bigint,

    -- True when a Google-native Doc/Sheet/Slide was exported to PDF.
    exported_pdf        boolean NOT NULL DEFAULT false,

    status              text NOT NULL DEFAULT 'synced'
                        CHECK (status IN ('synced', 'skipped', 'error')),
    error               text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    synced_at           timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- The dedup guard: one row per Drive file per business. upsert_synced() targets
-- this constraint with ON CONFLICT.
CREATE UNIQUE INDEX IF NOT EXISTS ux_drive_sync_file_ident
    ON drive_sync_files (business_slug, drive_file_id);

-- History listing per business (newest first).
CREATE INDEX IF NOT EXISTS drive_sync_business_idx
    ON drive_sync_files (business_slug, synced_at DESC);


-- Seed the sync Job. Idempotent (only if absent). next_run_at = now() makes it
-- due on the first cron tick; allow_multiple_runs lets a manual Start be pressed
-- repeatedly to drain a large first-sweep backlog (each run skips already-synced
-- files and copies the next batch). daily 06:00 keeps it fresh thereafter.
INSERT INTO jobs (job_type, name, config, schedule, timezone, enabled,
                  allow_multiple_runs, next_run_at)
SELECT
    'drive_s3_sync',
    'Drive → S3 Sync (all businesses)',
    '{}'::jsonb,
    '{"frequency": "daily", "times": ["06:00"], "days_of_week": []}'::jsonb,
    'America/Chicago',
    true,
    true,
    now()
WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE job_type = 'drive_s3_sync');
