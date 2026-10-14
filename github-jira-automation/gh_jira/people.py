"""Who did the work, read out of the push itself.

No roster to maintain. GitHub already carries every collaborator's real name in
the payload — `commits[].author.name` is whatever they set as `git config
user.name` — so Haider, Auroob and anyone else added to a repo are named
correctly the first time they push, with nothing configured in advance.

Resolution for one identity:

1. an explicit correction (`config.people()`) — see below
2. the real name on a commit authored by that same person in this push,
   matched on GitHub login or on email
3. the name GitHub put in the payload (`pusher.name`, `author.name`)
4. the raw login — never a guess dressed up as a name

The only configured entry is a *correction*, and it exists because the payload
is wrong rather than missing: the `gandikediye-afk` account's commits are
Sawera's work, so every name GitHub supplies for it — login or display name —
attributes the work to the wrong person. Everyone else is read, not mapped.
"""
from __future__ import annotations

from . import config


def _pretty(name: str) -> str:
    """Tidy a git `user.name` for display.

    Only touches all-lowercase names ("haider ali" -> "Haider Ali"). Anything
    with existing capitals is left exactly as written, so "McDonald", "de Vries"
    and "Auroob F." survive intact — mangling someone's own spelling of their
    name is worse than leaving it lowercase.
    """
    cleaned = " ".join(name.split())
    if cleaned and cleaned == cleaned.lower():
        return cleaned.title()
    return cleaned


def display_name(login: str | None, fallback_name: str | None = None) -> str:
    """The name to print for one GitHub identity."""
    mapping = config.people()

    if login:
        mapped = mapping.get(login.strip().lower())
        if mapped:
            return mapped

    if fallback_name:
        mapped = mapping.get(fallback_name.strip().lower())
        if mapped:
            return mapped
        return _pretty(fallback_name)

    return (login or "").strip() or "someone"


def display_names(logins: list[str]) -> list[str]:
    """Map a list of logins, dropping blanks and keeping order."""
    return [display_name(login) for login in logins if login]


def _commit_identities(commits: list[dict]) -> list[tuple[str, str, str]]:
    """`(login, email, name)` for every commit author, lowercased keys."""
    out: list[tuple[str, str, str]] = []
    for commit in commits or []:
        author = commit.get("author") or {}
        out.append((
            (author.get("username") or "").strip().lower(),
            (author.get("email") or "").strip().lower(),
            (author.get("name") or "").strip(),
        ))
    return out


def pusher_name(sender_login: str | None, pusher: dict | None, commits: list[dict] | None) -> str:
    """Who pushed, preferring the real name carried on their own commits.

    `pusher.name` is often just the GitHub login, so a push by "haider-dev"
    would read as "haider-dev" while their commits sitting directly underneath
    say "Haider Ali". Matching the pusher to their commits — by login, then by
    email — recovers the real name with nothing configured.
    """
    pusher = pusher or {}
    login = (sender_login or "").strip()
    raw_name = (pusher.get("name") or "").strip()
    email = (pusher.get("email") or "").strip().lower()

    # A configured correction always wins — it exists because the payload lies.
    mapping = config.people()
    for candidate in (login.lower(), raw_name.lower()):
        if candidate and candidate in mapping:
            return mapping[candidate]

    for commit_login, commit_email, commit_name in _commit_identities(commits or []):
        if not commit_name:
            continue
        if (login and commit_login == login.lower()) or (email and commit_email == email):
            # Run it back through display_name so a correction keyed on the
            # commit's own name still applies.
            return display_name(commit_login or None, commit_name)

    return display_name(login or None, raw_name or None)


def contributors(commits: list[dict] | None) -> list[str]:
    """Distinct commit authors in this push, in the order they appear.

    A branch is rarely one person's for its whole life; naming everyone who
    contributed to a push is the difference between "Sawera pushed" and knowing
    Haider wrote two of the four commits.
    """
    seen: list[str] = []
    for commit_login, _email, commit_name in _commit_identities(commits or []):
        if not (commit_name or commit_login):
            continue
        name = display_name(commit_login or None, commit_name or None)
        if name and name not in seen:
            seen.append(name)
    return seen
