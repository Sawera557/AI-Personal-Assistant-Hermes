-- KAN-24 / HRM-15: remembers which Jira issue a branch was auto-assigned to.
--
-- The automation opens a ticket for work that matches nothing on the board.
-- Without this table that means a NEW ticket on every push to the same
-- unlabelled branch. The first unmatched push records the link here; every
-- push after it comments on the ticket already opened.
--
-- Implementation: github-jira-automation/gh_jira/branch_links.py
-- This file lives here because /api/admin/migrate only scans hermes/db/migrations/*.sql.

CREATE TABLE IF NOT EXISTS github_jira_branch_issues (
    repo       text NOT NULL,               -- owner/name, lowercased
    branch     text NOT NULL,               -- branch name as GitHub reports it
    issue_key  text NOT NULL,               -- e.g. KAN-24
    source     text NOT NULL DEFAULT 'auto_created',  -- auto_created | ai | explicit
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch)
);

-- "Which branches were pointed at this ticket?" — the reverse lookup you make
-- when an auto-created ticket looks wrong and you want to find its source.
CREATE INDEX IF NOT EXISTS github_jira_branch_issues_key_idx
    ON github_jira_branch_issues (issue_key);
