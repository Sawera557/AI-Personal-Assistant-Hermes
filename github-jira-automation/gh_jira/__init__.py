"""GitHub -> Jira push automation (KAN-24 / HRM-15).

Receives GitHub webhooks, works out which Jira issue the branch/PR maps to, and
writes the update into that issue as a comment: branch, commits, PR status,
reviewers, merge state.

Only the GitHub half lives here. The Jira plumbing (cloud-id resolution, the
Maton-proxied REST transport) already shipped in KAN-81 as `hermes/tools.py`'s
`jira_*` tools; this package reuses `hermes.maton.MatonClient` and adds the one
call KAN-81 did not need — adding a comment to an issue.

Scope rule from the ticket, restated so it does not drift: this writes to the
vendor team's execution tracker only. It is not Gandi's command layer.
"""
from __future__ import annotations

__all__ = ["adf", "config", "dedupe", "events", "issue_keys", "jira_client", "service", "signature"]
