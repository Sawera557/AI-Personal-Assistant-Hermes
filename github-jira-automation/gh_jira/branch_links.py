"""Remembers which Jira issue a branch was auto-assigned to.

Without this, "create a ticket when the work matches nothing" means a NEW ticket
on every single push to the same unlabelled branch — ten pushes, ten tickets,
all describing the same work. The first unmatched push to a branch opens one
ticket; every push after that finds it here and comments on it instead.

It also outranks the AI matcher for that branch, so a branch cannot drift from
the ticket opened for it just because a later commit message reads differently.

Backed by `github_jira_branch_issues` (migration 0020). Every failure degrades
to "no link known", which costs at worst a duplicate ticket — never a lost push.
"""
from __future__ import annotations

import logging

log = logging.getLogger("gh_jira.branch_links")


def _cursor():
    from hermes.db import cursor  # deferred: keeps this importable with no DB

    return cursor()


def get(repo: str, branch: str) -> str | None:
    """The issue key already linked to this branch, if any."""
    if not repo or not branch:
        return None
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT issue_key FROM github_jira_branch_issues WHERE repo = %s AND branch = %s;",
                (repo.lower(), branch),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:  # noqa: BLE001 — see module docstring
        log.debug("Branch-link lookup unavailable for %s@%s", branch, repo, exc_info=True)
        return None


def put(repo: str, branch: str, issue_key: str, source: str = "auto_created") -> None:
    """Link a branch to an issue. Best-effort; never raises into the request path."""
    if not (repo and branch and issue_key):
        return
    try:
        with _cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_jira_branch_issues (repo, branch, issue_key, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (repo, branch) DO UPDATE SET issue_key = EXCLUDED.issue_key;
                """,
                (repo.lower(), branch, issue_key.upper(), source),
            )
    except Exception:  # noqa: BLE001
        log.debug("Could not record branch link %s@%s -> %s", branch, repo, issue_key, exc_info=True)
