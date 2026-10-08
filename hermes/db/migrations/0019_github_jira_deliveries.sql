-- KAN-24 / HRM-15: GitHub -> Jira push automation.
--
-- Records handled GitHub webhook deliveries so a redelivery (the "Redeliver"
-- button, or an at-least-once retry) does not post the same update onto the
-- Jira issue twice. Keyed on X-GitHub-Delivery, which GitHub reuses across
-- replays of the same delivery.
--
-- Implementation lives in github-jira-automation/gh_jira/ — this file is here
-- because /api/admin/migrate only scans hermes/db/migrations/*.sql.

CREATE TABLE IF NOT EXISTS github_jira_deliveries (
    delivery_id  text PRIMARY KEY,           -- X-GitHub-Delivery
    event        text NOT NULL,              -- push | pull_request | pull_request_review
    repo         text,                       -- owner/name
    issue_keys   text[] NOT NULL DEFAULT '{}',
    result       jsonb,                      -- the handler's outcome, for auditing
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- "What did the automation write to this ticket, and when?" — the lookup you
-- actually make when a comment looks wrong.
CREATE INDEX IF NOT EXISTS github_jira_deliveries_issue_keys_idx
    ON github_jira_deliveries USING GIN (issue_keys);

CREATE INDEX IF NOT EXISTS github_jira_deliveries_processed_at_idx
    ON github_jira_deliveries (processed_at DESC);
