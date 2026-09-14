-- Proposed amounts for register lines the Sheet marks "AMOUNT IN PDF - UNVERIFIED".
-- psql ... -f migrations/003_amount_proposals.sql
--
-- The Google Sheet remains the source of truth for the bill register. Hermes
-- only ever PROPOSES a figure it read out of a document it already holds, and a
-- human approves before the Sheet is touched. That keeps the register's own
-- rule intact: nothing is estimated, and an amount that cannot be evidenced
-- stays unverified.
--
-- Every proposal therefore carries its evidence (which bill, which stored file,
-- which extractor produced it) so an approver can check the number against the
-- document rather than trusting the model.

CREATE TABLE IF NOT EXISTS amount_proposals (
    proposal_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Where this belongs in the Sheet.
    register_ref      TEXT        NOT NULL,       -- the register's Ref#
    register_row      INTEGER     NOT NULL,       -- sheet row, for write-back
    vendor            TEXT,                       -- snapshot, so a proposal stays
    entity            TEXT,                       -- readable if the Sheet moves on
    previous_value    TEXT,                       -- what the cell said before

    -- What we read out of the document.
    proposed_amount   NUMERIC(14,2),
    proposed_currency TEXT,
    invoice_number    TEXT,
    issue_date        DATE,
    due_date          DATE,
    line_items        JSONB,

    -- What we read it from.
    evidence_bill_id  UUID        REFERENCES bills(bill_id) ON DELETE SET NULL,
    evidence_file_url TEXT,
    evidence_source   TEXT,                       -- gmail message id / drive file id
    extractor         TEXT,                       -- nuextract | ocr-fallback
    match_score       NUMERIC(4,3),               -- vendor/entity match confidence

    status            TEXT        NOT NULL DEFAULT 'pending',
    -- pending | approved | rejected | superseded
    decided_at        TIMESTAMPTZ,
    decided_note      TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE amount_proposals DROP CONSTRAINT IF EXISTS amount_proposals_status_chk;
ALTER TABLE amount_proposals ADD CONSTRAINT amount_proposals_status_chk
    CHECK (status IN ('pending', 'approved', 'rejected', 'superseded'));

CREATE INDEX IF NOT EXISTS amount_proposals_ref_idx    ON amount_proposals (register_ref);
CREATE INDEX IF NOT EXISTS amount_proposals_status_idx ON amount_proposals (status);

-- At most one live proposal per register line: re-running verification
-- supersedes the previous suggestion rather than stacking duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS amount_proposals_one_pending
    ON amount_proposals (register_ref)
    WHERE status = 'pending';
