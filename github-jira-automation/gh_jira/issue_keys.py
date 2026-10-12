"""Work out which Jira issue a GitHub event belongs to.

The whole automation hinges on this: a comment on the wrong ticket is worse than
no comment, so every resolved key carries the source it came from and nothing is
inferred beyond an explicit textual reference.

Resolution order (first source that yields keys wins, unless
`collect_all=True`):

1. the branch name          — `feature/KAN-24-webhook`
2. the merge commit's branch — `Merge pull request #42 from owner/feature/KAN-24-webhook`
3. the PR title             — `KAN-24 GitHub to Jira automation`
4. the PR body              — `Closes KAN-24`
5. the commit messages      — `KAN-24: add signature check`

Source 2 exists because a merge lands on `main`, which names no ticket. Without
it the merge comment depends entirely on whether every individual commit was
prefixed; with it, GitHub's own merge subject carries the reference.

Case handling: with a configured project allowlist (the default, `KAN`) matching
is case-insensitive, because branch names are conventionally lowercase
(`feature/kan-24-...`) and the allowlist keeps the false-positive surface tiny.
With `GITHUB_JIRA_PROJECT_KEYS=*` there is no allowlist to lean on, so matching
requires the canonical uppercase form — otherwise ordinary hyphenated words
("utf-8", "covid-19", "sha-1") would resolve as issue keys.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

# A Jira key: project key (letter, then letters/digits) + "-" + issue number.
# Bounded at 10 project-key chars, which is Jira's own limit.
_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{0,9})-([0-9]+)\b")


def _evidence(text: str, limit: int = 120) -> str:
    """One tidy line quoting what the key was read out of.

    The commit source is a newline-joined blob of every message in the push, and
    pasting that raw into the comment footer spills it across several lines and
    buries the sentence it belongs to. Collapse to a single line and cap it.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class KeyMatch:
    """One issue key and the thing it was read out of."""

    key: str      # canonical uppercase, e.g. "KAN-24"
    source: str   # "branch" | "pr_title" | "pr_body" | "commit" | "default"
    text: str     # the fragment it came from, for provenance in logs

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "source": self.source, "from": self.text}


def extract(text: str, allowed: list[str] | None = None) -> list[str]:
    """Every Jira key in `text`, canonicalised and de-duplicated in order.

    `allowed` is a list of uppercase project keys; `["*"]` or None means any
    project, which switches matching to case-sensitive (see module docstring).
    """
    if not text:
        return []

    any_project = not allowed or allowed == ["*"]
    found: list[str] = []
    for match in _KEY_RE.finditer(text):
        project, number = match.group(1), match.group(2)
        if any_project:
            # No allowlist to filter against — only trust the canonical form.
            if project != project.upper():
                continue
        elif project.upper() not in allowed:
            continue

        key = f"{project.upper()}-{number}"
        if key not in found:
            found.append(key)
    return found


def resolve(sources: list[tuple[str, str]], collect_all: bool = False) -> list[KeyMatch]:
    """Resolve issue keys from `[(source_name, text), ...]` in priority order.

    Returns [] when nothing matches — the caller must then skip, not guess. The
    only exception is `GITHUB_JIRA_DEFAULT_ISSUE_KEY`, which is opt-in and
    reported with source `"default"` so it is never mistaken for a real
    reference found in the event.
    """
    allowed = config.project_keys()
    matches: list[KeyMatch] = []
    seen: set[str] = set()

    for source_name, text in sources:
        keys = extract(text, allowed)
        if not keys:
            continue
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            matches.append(KeyMatch(key=key, source=source_name, text=_evidence(text)))
        if not collect_all:
            break

    if matches:
        return matches

    fallback = config.default_issue_key()
    if fallback:
        return [KeyMatch(key=fallback.upper(), source="default", text="GITHUB_JIRA_DEFAULT_ISSUE_KEY")]
    return []
