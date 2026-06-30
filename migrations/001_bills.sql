-- Billing & Invoices tables for Hermes
-- Run once against RDS: psql ... -f migrations/001_bills.sql

-- Businesses owned by this account
CREATE TABLE IF NOT EXISTS businesses (
    business_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT        NOT NULL,
    slug          TEXT        UNIQUE NOT NULL,
    emoji         TEXT        NOT NULL DEFAULT '🏢',
    color         TEXT        NOT NULL DEFAULT '#9aa3b2',
    industry      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A bill / invoice record (from upload or connected apps)
CREATE TABLE IF NOT EXISTS bills (
    bill_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id    UUID        REFERENCES businesses(business_id) ON DELETE SET NULL,
    vendor         TEXT        NOT NULL,
    amount         NUMERIC(14,2),
    invoice_number TEXT,
    due_date       DATE,
    issue_date     DATE,
    currency       TEXT        NOT NULL DEFAULT 'USD',
    status         TEXT        NOT NULL DEFAULT 'pending',
    -- pending | due-soon | overdue | paid | processing
    category       TEXT,
    confidence     NUMERIC(4,3),   -- AI confidence in business assignment (0-1)
    source         TEXT        NOT NULL DEFAULT 'upload',
    -- upload | gmail | google-drive
    source_ref     TEXT,           -- email message_id, drive file_id, etc.
    file_url       TEXT,           -- S3 URL of original
    raw_extracted  JSONB,          -- full NuExtract response
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bills_business_id_idx ON bills(business_id);
CREATE INDEX IF NOT EXISTS bills_status_idx      ON bills(status);
CREATE INDEX IF NOT EXISTS bills_due_date_idx    ON bills(due_date);

-- Line items extracted from each bill
CREATE TABLE IF NOT EXISTS bill_line_items (
    item_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id     UUID        NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    description TEXT,
    quantity    NUMERIC(10,3) DEFAULT 1,
    unit_price  NUMERIC(14,2),
    total       NUMERIC(14,2)
);

CREATE INDEX IF NOT EXISTS bill_line_items_bill_id_idx ON bill_line_items(bill_id);

-- updated_at is kept fresh by the Python layer (update_bill sets it explicitly)
