"""Normalise GitHub webhook payloads into one shape the rest of the package uses.

Everything GitHub-specific stops here. `adf.py` renders an `Update` and
`service.py` routes it; neither needs to know what a `pull_request.synchronize`
payload looks like.

Supported events (the five facts KAN-24 asks for — branch, commit, PR status,
reviewer, merge state — are covered across them):

  push                  branch + commits, incl. created / deleted / force-push
  pull_request          PR status, reviewers, merge state
  pull_request_review   who reviewed and what they decided

Anything else is returned as unsupported and acknowledged without a write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import config, people

SUPPORTED_EVENTS = ("push", "pull_request", "pull_request_review")

# GitHub's own merge-commit subjects. A merge lands on `main`, which names no
# ticket, so without this the only thing tying the merge back to the issue is
# whether every individual commit happened to carry the key. These recover the
# SOURCE branch from the merge commit itself, which is the durable reference.
#
#   Merge pull request #42 from gandikediye-afk/feature/KAN-24-webhook
#   Merge branch 'feature/KAN-24-webhook' into main
_MERGE_PATTERNS = (
    re.compile(r"^Merge pull request #\d+ from [^/\s]+/(?P<branch>\S+)"),
    re.compile(r"^Merge branch '(?P<branch>[^']+)'"),
    re.compile(r"^Merge remote-tracking branch '[^/']+/(?P<branch>[^']+)'"),
)


def merge_source_branch(message: str) -> str | None:
    """The branch a merge commit came FROM, or None if it is not a merge commit."""
    subject = _first_line(message)
    for pattern in _MERGE_PATTERNS:
        found = pattern.match(subject)
        if found:
            return found.group("branch")
    return None


@dataclass
class Update:
    """A GitHub event flattened into something worth reading in a Jira comment."""

    event: str
    action: str | None
    repo: str
    headline: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    commits: list[dict[str, str]] = field(default_factory=list)
    commits_omitted: int = 0
    url: str | None = None
    branch: str | None = None
    # (source_name, text) pairs handed to issue_keys.resolve, in priority order.
    key_sources: list[tuple[str, str]] = field(default_factory=list)
    # Signals used only by the AI matcher (matcher.py) when no explicit key was
    # found. Kept off the comment itself — a file list is evidence for choosing
    # a ticket, not something worth pasting into every update.
    files: list[str] = field(default_factory=list)
    pr_text: str = ""
    # Branches a merge commit in this push came FROM. On a push to `main` these
    # are the units of work that actually own the tickets — `main` never does.
    merge_sources: list[str] = field(default_factory=list)
    # True when this event is a pull request being merged.
    merged: bool = False


class UnsupportedEvent(Exception):
    """Raised for a well-formed delivery this automation deliberately ignores."""


def _branch_from_ref(ref: str) -> str | None:
    """`refs/heads/feature/x` -> `feature/x`. Tags and other refs return None."""
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return None


def _short(sha: str | None) -> str:
    return (sha or "")[:7]


def _first_line(message: str | None) -> str:
    return (message or "").strip().splitlines()[0] if (message or "").strip() else ""


def parse(event: str, payload: dict[str, Any]) -> Update:
    """Turn a raw webhook payload into an `Update`, or raise `UnsupportedEvent`."""
    if event == "push":
        return _parse_push(payload)
    if event == "pull_request":
        return _parse_pull_request(payload)
    if event == "pull_request_review":
        return _parse_review(payload)
    raise UnsupportedEvent(event)


# ── push ─────────────────────────────────────────────────────────────────────
def _parse_push(payload: dict[str, Any]) -> Update:
    repo = (payload.get("repository") or {}).get("full_name") or "unknown/unknown"
    ref = payload.get("ref") or ""
    branch = _branch_from_ref(ref)
    if branch is None:
        # Tag pushes carry no branch to map to an issue, and the ticket is about
        # branch activity. Acknowledge, write nothing.
        raise UnsupportedEvent(f"push to non-branch ref {ref!r}")

    raw_commits = payload.get("commits") or []
    limit = config.max_commits_listed()
    commits = [
        {
            "sha": _short(c.get("id")),
            "message": _first_line(c.get("message")),
            "author": people.display_name(
                (c.get("author") or {}).get("username"),
                (c.get("author") or {}).get("name"),
            ),
            "url": c.get("url") or "",
        }
        for c in raw_commits[:limit]
    ]
    omitted = max(0, len(raw_commits) - limit)

    created, deleted, forced = payload.get("created"), payload.get("deleted"), payload.get("forced")
    pusher = people.pusher_name(
        (payload.get("sender") or {}).get("login"),
        payload.get("pusher"),
        raw_commits,
    )
    # Who actually wrote the code, read off the commits — not the same question
    # as who ran `git push`, and often not the same person.
    wrote = people.contributors(raw_commits)

    if deleted:
        headline = f"Branch `{branch}` deleted"
    elif created:
        headline = f"Branch `{branch}` created"
    else:
        count = len(raw_commits)
        headline = f"{count} commit{'s' if count != 1 else ''} pushed to `{branch}`"

    facts: list[tuple[str, str]] = [("Repository", repo), ("Branch", branch), ("Pushed by", pusher)]
    # Only worth a line when it says something "Pushed by" does not — one person
    # pushing their own single-author work needs no second attribution.
    if wrote and wrote != [pusher]:
        facts.append(("Written by", ", ".join(wrote)))
    if forced:
        facts.append(("Force push", "yes — history on this branch was rewritten"))
    head = payload.get("head_commit") or {}
    if head.get("id"):
        facts.append(("Head commit", f"{_short(head.get('id'))} {_first_line(head.get('message'))}".strip()))
    elif payload.get("after"):
        facts.append(("Head commit", _short(payload.get("after"))))

    # Branch first: it is the intentional, durable mapping. Then any branch
    # recovered from a merge commit — that is what carries a merge landing on
    # `main` back to its ticket. Commit messages come last, being the noisiest.
    key_sources: list[tuple[str, str]] = [("branch", branch)]

    merged_branches = [
        found
        for found in (merge_source_branch(c.get("message") or "") for c in raw_commits)
        if found
    ]
    if merged_branches:
        key_sources.append(("merge_branch", " ".join(dict.fromkeys(merged_branches))))

    commit_text = "\n".join(_first_line(c.get("message")) for c in raw_commits)
    if commit_text.strip():
        key_sources.append(("commit", commit_text))

    # Which files moved is the strongest signal for "what work is this?" when
    # nobody wrote a ticket number down.
    touched: list[str] = []
    for commit in raw_commits:
        for bucket in ("added", "modified", "removed"):
            for path in commit.get(bucket) or []:
                if path not in touched:
                    touched.append(path)

    return Update(
        event="push",
        action=None,
        repo=repo,
        headline=headline,
        facts=facts,
        commits=commits,
        commits_omitted=omitted,
        url=payload.get("compare") or head.get("url"),
        branch=branch,
        key_sources=key_sources,
        files=touched,
        merge_sources=list(dict.fromkeys(merged_branches)),
    )


# ── pull_request ─────────────────────────────────────────────────────────────
def _pr_state(pr: dict[str, Any]) -> str:
    """The merge state the ticket asks for, in one readable phrase."""
    if pr.get("merged"):
        merged_by = people.display_name((pr.get("merged_by") or {}).get("login")) or "unknown"
        return f"merged by {merged_by}"
    if pr.get("state") == "closed":
        return "closed without merging"
    if pr.get("draft"):
        return "open (draft)"
    return "open"


def _parse_pull_request(payload: dict[str, Any]) -> Update:
    action = payload.get("action") or ""
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or "unknown/unknown"
    number = payload.get("number") or pr.get("number")
    head = (pr.get("head") or {}).get("ref") or ""
    base = (pr.get("base") or {}).get("ref") or ""
    title = pr.get("title") or ""

    verb = {
        "opened": "opened",
        "reopened": "reopened",
        "synchronize": "updated with new commits",
        "ready_for_review": "marked ready for review",
        "converted_to_draft": "converted back to draft",
        "closed": "merged" if pr.get("merged") else "closed",
        "review_requested": "review requested on",
        "review_request_removed": "review request removed from",
        "edited": "edited",
        "assigned": "assigned on",
        "unassigned": "unassigned on",
        "labeled": "labelled on",
        "unlabeled": "unlabelled on",
    }.get(action, action or "updated")

    headline = f"Pull request #{number} {verb}: {title}".strip()

    facts: list[tuple[str, str]] = [
        ("Repository", repo),
        ("Branch", f"{head} -> {base}" if head and base else (head or base)),
        ("Status", _pr_state(pr)),
        ("Author", people.display_name((pr.get("user") or {}).get("login"))),
    ]

    reviewers = people.display_names([
        (r or {}).get("login")
        for r in (pr.get("requested_reviewers") or [])
        if (r or {}).get("login")
    ])
    teams = [
        (t or {}).get("name")
        for t in (pr.get("requested_teams") or [])
        if (t or {}).get("name")
    ]
    if reviewers or teams:
        facts.append(("Reviewers requested", ", ".join(reviewers + [f"@{t}" for t in teams])))

    if pr.get("merged") and pr.get("merge_commit_sha"):
        facts.append(("Merge commit", _short(pr.get("merge_commit_sha"))))

    key_sources: list[tuple[str, str]] = []
    if head:
        key_sources.append(("branch", head))
    if title:
        key_sources.append(("pr_title", title))
    if pr.get("body"):
        key_sources.append(("pr_body", str(pr.get("body"))))

    return Update(
        event="pull_request",
        action=action,
        repo=repo,
        headline=headline,
        facts=facts,
        url=pr.get("html_url"),
        branch=head or None,
        key_sources=key_sources,
        pr_text=f"{title}\n{pr.get('body') or ''}".strip(),
        merged=bool(pr.get("merged")),
    )


# ── pull_request_review ──────────────────────────────────────────────────────
def _parse_review(payload: dict[str, Any]) -> Update:
    action = payload.get("action") or ""
    if action != "submitted":
        # dismissed / edited reviews are noise on a Jira ticket.
        raise UnsupportedEvent(f"pull_request_review action {action!r}")

    review = payload.get("review") or {}
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or "unknown/unknown"
    number = pr.get("number")
    reviewer = people.display_name((review.get("user") or {}).get("login"))
    state = (review.get("state") or "").lower()
    decision = {
        "approved": "approved",
        "changes_requested": "requested changes on",
        "commented": "commented on",
    }.get(state, state or "reviewed")

    head = (pr.get("head") or {}).get("ref") or ""
    title = pr.get("title") or ""

    facts: list[tuple[str, str]] = [
        ("Repository", repo),
        ("Branch", head),
        ("Reviewer", reviewer),
        ("Decision", decision.replace(" on", "")),
    ]
    body = (review.get("body") or "").strip()
    if body:
        facts.append(("Review note", body[:500] + ("..." if len(body) > 500 else "")))

    key_sources: list[tuple[str, str]] = []
    if head:
        key_sources.append(("branch", head))
    if title:
        key_sources.append(("pr_title", title))

    return Update(
        event="pull_request_review",
        action=action,
        repo=repo,
        headline=f"{reviewer} {decision} pull request #{number}: {title}".strip(),
        facts=facts,
        url=review.get("html_url") or pr.get("html_url"),
        branch=head or None,
        key_sources=key_sources,
    )
