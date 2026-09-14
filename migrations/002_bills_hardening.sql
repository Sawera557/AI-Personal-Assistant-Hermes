-- Billing hardening — run after 001_bills.sql
-- psql ... -f migrations/002_bills_hardening.sql
--
-- 1. De-duplication was advisory only. hermes/bills.py called
--    source_ref_exists() and then inserted, so two scans of the same mailbox
--    running concurrently (the UI fires an auto-scan on open AND the user can
--    press "Scan Connected Apps") both saw "not imported" and both inserted.
--    Nothing in the schema stopped it. insert_bill() now relies on this index.
-- 2. The list query filters on status and orders by due_date; the two
--    single-column indexes could not serve that together.

-- Collapse any duplicates already in the table, keeping the earliest row.
DELETE FROM bills a
USING bills b
WHERE a.source_ref IS NOT NULL
  AND a.source_ref = b.source_ref
  AND a.source     = b.source
  AND (a.created_at, a.bill_id) > (b.created_at, b.bill_id);

CREATE UNIQUE INDEX IF NOT EXISTS bills_source_ref_uniq
    ON bills (source, source_ref)
    WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS bills_status_due_date_idx
    ON bills (status, due_date);

-- 'overdue' and 'due-soon' are derived from due_date at read time
-- (hermes/db/bills.py::derive_status) and must never be persisted — a stored
-- value goes stale the moment the date rolls over. Normalise legacy rows.
UPDATE bills SET status = 'pending' WHERE status IN ('overdue', 'due-soon');

ALTER TABLE bills DROP CONSTRAINT IF EXISTS bills_status_chk;
ALTER TABLE bills ADD CONSTRAINT bills_status_chk
    CHECK (status IN ('pending', 'paid', 'processing'));
