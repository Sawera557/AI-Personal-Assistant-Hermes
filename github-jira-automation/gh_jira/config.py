"""Environment-driven settings for the GitHub -> Jira webhook.

Read through functions, not module-level constants, so a value changed in the
environment (or a test) takes effect on the next call instead of being frozen at
import time — the webhook route is imported once per serverless cold start and
then reused for many deliveries.
"""
from __future__ import annotations

import os

# The default project key set. KAN is the vendor team's execution board (KAN-24
# lives there). Narrow rather than wide on purpose: an unfiltered key regex
# happily matches things like "UTF-8" or "SHA-1" in a commit message.
_DEFAULT_PROJECT_KEYS = "KAN"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def webhook_secret() -> str:
    """Shared secret configured on the GitHub webhook.

    Empty means the endpoint refuses every delivery — an unsigned webhook that
    writes to a live Jira board is an open comment-injection endpoint, so the
    fail-closed direction is the only safe one.
    """
    return os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()


def maton_api_key() -> str | None:
    """Maton key used for the Jira writes, if a dedicated one is configured.

    The automation writes to the vendor team's Jira site, which is reached
    through Sawera's Maton connection — a different account from the default
    `MATON_API_KEY` that serves Gandi's Gmail/Drive/Calendar. Keeping it a
    separate variable means rotating one never silently breaks the other, and
    the ticket's scope rule ("this stays in the vendor team's lane") holds at
    the credential level and not just by convention.

    None falls back to `Config.MATON_API_KEY`.
    """
    return os.getenv("GITHUB_JIRA_MATON_API_KEY", "").strip() or None


def enabled() -> bool:
    """Master switch. Off = accept and acknowledge, write nothing."""
    return _bool(os.getenv("GITHUB_JIRA_ENABLED"), True)


def dry_run() -> bool:
    """Parse, resolve the issue keys, render the comment — but do not post it.

    Useful for confirming the branch -> issue mapping against real traffic
    before letting it write to the board.
    """
    return _bool(os.getenv("GITHUB_JIRA_DRY_RUN"), False)


# The repos this automation is installed on. Defaults are the three Gandi named
# on 2026-07-31; override with GITHUB_JIRA_REPOS to change the set. An entry for
# a repo that never sends a webhook is simply inert.
_DEFAULT_REPOS = (
    "gandikediye-afk/Hermes-Agent-MVP",
    "gandikediye-afk/Jira-Github-update-automation",
    "gandikediye-afk/pinecone-mcp-claude",
    "gandikediye-afk/Hermes-MVP-DataIngestion",
)


def allowed_repos() -> list[str]:
    """`owner/name` allowlist. `*` = any repo the webhook is installed on."""
    configured = _csv(os.getenv("GITHUB_JIRA_REPOS")) or list(_DEFAULT_REPOS)
    if configured == ["*"]:
        return []
    return [repo.lower() for repo in configured]


# ── Who pushed ───────────────────────────────────────────────────────────────
# NOT a roster. Collaborators' real names are read straight off their commits
# (`commits[].author.name`), so Haider, Auroob and anyone else added to a repo
# are named correctly the first time they push with nothing configured.
#
# These entries are CORRECTIONS — needed only where the payload is wrong rather
# than missing. The gandikediye-afk account's commits are Sawera's work, so
# every name GitHub supplies for it (login and display name alike) attributes
# the work to the wrong person.
_DEFAULT_PEOPLE = {
    "gandikediye-afk": "Sawera",
    "gandi kediye": "Sawera",
    "gandimohamed@gmail.com": "Sawera",
}


def people() -> dict[str, str]:
    """Lowercased login / name / email -> the name to print instead.

    Only for identities the payload gets WRONG. Extend via
    GITHUB_JIRA_PEOPLE="login=Name,other=Name2"; configured entries merge over
    the defaults rather than replacing them, so a later addition can never
    silently drop the Sawera correction.
    """
    mapping = {k.lower(): v for k, v in _DEFAULT_PEOPLE.items()}
    for pair in _csv(os.getenv("GITHUB_JIRA_PEOPLE")):
        login, sep, name = pair.partition("=")
        if sep and login.strip() and name.strip():
            mapping[login.strip().lower()] = name.strip()
    return mapping


# ── Auto-created tickets ─────────────────────────────────────────────────────
def auto_create() -> bool:
    """Open a Jira task for work that matches nothing on the board."""
    return _bool(os.getenv("GITHUB_JIRA_AUTO_CREATE"), True)


def auto_create_project() -> str:
    """Project new tasks land in. Defaults to the first allowed project key."""
    configured = os.getenv("GITHUB_JIRA_AUTO_CREATE_PROJECT", "").strip().upper()
    if configured:
        return configured
    keys = [k for k in project_keys() if k != "*"]
    return keys[0] if keys else "KAN"


def auto_create_type() -> str:
    return os.getenv("GITHUB_JIRA_AUTO_CREATE_TYPE", "Task").strip() or "Task"


def auto_create_labels() -> list[str]:
    """Stamped on every auto-created ticket so they stay findable and cleanable.

    Without a marker there is no way to tell a machine-opened ticket from a
    human-written one when reviewing the board later.
    """
    raw = os.getenv("GITHUB_JIRA_AUTO_CREATE_LABELS", "github-automation,auto-created")
    return _csv(raw)


# ── Status transitions ───────────────────────────────────────────────────────
def transition_on_commit() -> str:
    """Status a ticket moves to when commits land. Empty disables."""
    return os.getenv("GITHUB_JIRA_STATUS_ON_COMMIT", "In Progress").strip()


def transition_on_pr() -> str:
    """Status a ticket moves to when a PR opens. Empty disables."""
    return os.getenv("GITHUB_JIRA_STATUS_ON_PR", "In Review").strip()


def transition_on_merge() -> str:
    """Status a ticket moves to when its branch merges. Empty disables.

    Deleting a branch deliberately does NOT close its ticket: branches are often
    deleted long after merging, and a branch abandoned without merging would
    otherwise mark unfinished work Done.
    """
    return os.getenv("GITHUB_JIRA_STATUS_ON_MERGE", "Done").strip()


# ── Which branches are units of work ─────────────────────────────────────────
# A bound branch keeps its ticket forever, so binding a long-lived branch would
# nail every future push to it onto one permanent issue. These are shared trunks,
# not units of work, and are never bound.
_DEFAULT_UNBINDABLE = "main,master,develop,dev,trunk,staging,production,release"


def unbindable_branches() -> list[str]:
    """Branch names (and `prefix/` roots) that never get bound to a ticket."""
    return [b.lower() for b in _csv(os.getenv("GITHUB_JIRA_TRUNK_BRANCHES", _DEFAULT_UNBINDABLE))]


def is_bindable(branch: str | None) -> bool:
    """True if this branch is a unit of work that may own a ticket."""
    if not branch:
        return False
    name = branch.strip().lower()
    for trunk in unbindable_branches():
        # Exact name, or a namespace like `release/2026.07`.
        if name == trunk or name.startswith(f"{trunk}/"):
            return False
    return True


def protected_statuses() -> list[str]:
    """Statuses the automation refuses to move a ticket OUT of.

    Done is protected by default: a stray commit on an old branch should not
    silently reopen finished work and put it back on someone's board.
    """
    raw = os.getenv("GITHUB_JIRA_PROTECTED_STATUSES", "Done")
    return [s.lower() for s in _csv(raw)]


def project_keys() -> list[str]:
    """Jira project keys an issue reference is allowed to belong to.

    Set to `*` to accept any project key (case-sensitive matching then applies —
    see `issue_keys.resolve`).
    """
    raw = os.getenv("GITHUB_JIRA_PROJECT_KEYS", _DEFAULT_PROJECT_KEYS)
    return [key.strip().upper() for key in _csv(raw)]


def any_project_key() -> bool:
    return project_keys() == ["*"]


def default_issue_key() -> str | None:
    """Issue to fall back to when a push carries no recognisable key.

    Deliberately unset by default: silently attributing an unlabelled branch to
    some catch-all ticket is a fabricated link, and a wrong Jira comment is
    worse than a missing one. Set it only if you actually want a catch-all.
    """
    return (os.getenv("GITHUB_JIRA_DEFAULT_ISSUE_KEY", "").strip() or None)


# ── AI issue matching ────────────────────────────────────────────────────────
# Only ever runs when no explicit key was found, i.e. on pushes the automation
# would otherwise skip entirely. It cannot override a human-stated ticket.
def ai_match() -> bool:
    """Infer the issue when the branch and commits name none.

    On by default because the alternative for these pushes is silence, and
    because it is inert without an OpenAI key. Every AI-matched comment
    discloses that it was inferred, with the confidence and the reason.
    """
    return _bool(os.getenv("GITHUB_JIRA_AI_MATCH"), True)


def ai_model() -> str:
    """Cheapest OpenAI text model by default — this is a short classification
    over a list of ticket titles, not a reasoning task worth paying for."""
    return os.getenv("GITHUB_JIRA_AI_MODEL", "gpt-4.1-nano").strip() or "gpt-4.1-nano"


def openai_api_key() -> str | None:
    """Override key for the matcher; falls back to Hermes' `OPENAI_API_KEY`."""
    return os.getenv("GITHUB_JIRA_OPENAI_API_KEY", "").strip() or None


def ai_min_confidence() -> float:
    """Below this, the match is discarded and the push is skipped.

    0.7 is deliberately strict: skipping restores the previous behaviour and
    costs nothing, while a wrong attribution puts false progress on a ticket
    someone else is judged on.
    """
    try:
        return min(1.0, max(0.0, float(os.getenv("GITHUB_JIRA_AI_MIN_CONFIDENCE", "0.7"))))
    except ValueError:
        return 0.7


def ai_candidate_jql() -> str:
    """Which issues the model is allowed to choose between.

    Defaults to unresolved issues in the allowed projects, most recent first —
    the live backlog. Closed tickets are excluded: work rarely belongs to
    something already marked done, and every extra candidate is a chance to
    pick wrong.
    """
    custom = os.getenv("GITHUB_JIRA_AI_JQL", "").strip()
    if custom:
        return custom
    keys = [k for k in project_keys() if k != "*"]
    scope = f"project in ({', '.join(keys)}) AND " if keys else ""
    return f"{scope}resolution = Unresolved ORDER BY updated DESC"


def ai_max_candidates() -> int:
    try:
        return max(1, min(100, int(os.getenv("GITHUB_JIRA_AI_CANDIDATES", "40"))))
    except ValueError:
        return 40


def max_commits_listed() -> int:
    """Commits shown in full in a push comment before the rest are summarised.

    The tail is never dropped silently — `events.py` records how many were left
    out and the comment says so.
    """
    try:
        return max(1, int(os.getenv("GITHUB_JIRA_MAX_COMMITS", "10")))
    except ValueError:
        return 10


def snapshot() -> dict[str, object]:
    """Non-secret view of the current config, for the health endpoint."""
    return {
        "enabled": enabled(),
        "dry_run": dry_run(),
        "secret_configured": bool(webhook_secret()),
        "dedicated_maton_key": bool(maton_api_key()),
        "allowed_repos": allowed_repos() or "any",
        "project_keys": project_keys(),
        "default_issue_key": default_issue_key(),
        "max_commits_listed": max_commits_listed(),
        "ai_match": ai_match(),
        "ai_model": ai_model(),
        "ai_min_confidence": ai_min_confidence(),
        "ai_candidate_jql": ai_candidate_jql(),
        "auto_create": auto_create(),
        "status_on_commit": transition_on_commit(),
        "status_on_pr": transition_on_pr(),
        "status_on_merge": transition_on_merge(),
        "trunk_branches": unbindable_branches(),
        "people_mapped": sorted(people()),
    }
