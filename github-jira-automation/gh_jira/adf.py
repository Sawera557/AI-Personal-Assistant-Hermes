"""Render an `events.Update` as an Atlassian Document Format comment body.

Jira REST v3 takes ADF, not wiki markup or plain strings — a plain string in the
`body` field is rejected with a 400. ADF is a small tree: a `doc` node holding
paragraphs, bullet lists, and inline text nodes that carry `marks` for styling.

Layout of a generated comment:

    **12 commits pushed to `feature/KAN-24-webhook`**      <- headline
    • Repository: gandikediye-afk/Hermes-Agent-MVP        <- facts
    • Branch: feature/KAN-24-webhook
    • Pushed by: haider
    Commits:                                              <- push events only
    • a1b2c3d  add signature check — haider
    View on GitHub                                        <- link
    Posted automatically from a GitHub push event ...     <- provenance footer
"""
from __future__ import annotations

from typing import Any

from .events import Update

_FOOTER = (
    "Posted automatically by the Hermes GitHub -> Jira automation (KAN-24). "
    "Issue matched from the {source}: {evidence}"
)


def _text(value: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def _strong(value: str) -> dict[str, Any]:
    return _text(value, [{"type": "strong"}])


def _code(value: str) -> dict[str, Any]:
    return _text(value, [{"type": "code"}])


def _link(value: str, href: str) -> dict[str, Any]:
    return _text(value, [{"type": "link", "attrs": {"href": href}}])


def _paragraph(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(nodes)}


def _bullet_list(items: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_paragraph(*nodes)]}
            for nodes in items
        ],
    }


def comment_doc(
    update: Update,
    matched_source: str = "",
    evidence: str = "",
    ai_match: Any | None = None,
) -> dict[str, Any]:
    """Build the full ADF document for one update.

    `ai_match` (a `matcher.AiMatch`) replaces the provenance footer with an
    explicit disclosure. An inferred attribution that reads like a certain one
    is the failure mode here: the reader must be able to tell at a glance that
    a model chose this ticket, and on what basis, so they can challenge it.
    """
    content: list[dict[str, Any]] = [_paragraph(_strong(update.headline))]

    if update.facts:
        content.append(
            _bullet_list([
                [_strong(f"{label}: "), _text(value)]
                for label, value in update.facts
                if value
            ])
        )

    if update.commits:
        content.append(_paragraph(_strong("Commits")))
        items: list[list[dict[str, Any]]] = []
        for commit in update.commits:
            nodes: list[dict[str, Any]] = []
            sha, url = commit.get("sha", ""), commit.get("url", "")
            if sha:
                nodes.append(_link(sha, url) if url else _code(sha))
                nodes.append(_text("  "))
            nodes.append(_text(commit.get("message") or "(no message)"))
            if commit.get("author"):
                nodes.append(_text(f" — {commit['author']}"))
            items.append(nodes)
        content.append(_bullet_list(items))

        if update.commits_omitted:
            # Say what was left out rather than quietly truncating: a comment
            # that looks complete but is not is the failure mode to avoid.
            content.append(
                _paragraph(_text(
                    f"...and {update.commits_omitted} more commit"
                    f"{'s' if update.commits_omitted != 1 else ''} not listed here "
                    f"(see the GitHub link for the full range)."
                ))
            )

    if update.url:
        content.append(_paragraph(_link("View on GitHub", update.url)))

    if ai_match is not None:
        content.append(_paragraph(
            _strong("⚠ Ticket chosen by AI, not stated in the code. "),
            _text(
                f"No issue key appeared in the branch or commit messages, so "
                f"{ai_match.model} picked this ticket at {ai_match.confidence:.0%} confidence: "
                f"\"{ai_match.reason}\" Reference the key in your branch or commit "
                f"message to remove the guesswork.",
                [{"type": "em"}],
            ),
        ))
    else:
        footer = _FOOTER.format(
            source=matched_source or "event", evidence=evidence or update.branch or "-"
        )
        content.append(_paragraph(_text(footer, [{"type": "em"}])))

    return {"type": "doc", "version": 1, "content": content}


def new_issue_doc(update: Update, proposal: Any) -> dict[str, Any]:
    """Description body for a ticket the automation opens itself.

    Says up front that a machine wrote it and on what evidence. A ticket that
    looks hand-written but is not will be trusted more than it has earned —
    someone reading the board should know a model chose this title, and be able
    to see the commits it read to get there.
    """
    content: list[dict[str, Any]] = []

    if proposal.description:
        content.append(_paragraph(_text(proposal.description)))

    facts: list[list[dict[str, Any]]] = [
        [_strong("Repository: "), _text(update.repo)],
        [_strong("Branch: "), _text(update.branch or "-")],
    ]
    for label, value in update.facts:
        if label in {"Pushed by", "Written by", "Author"} and value:
            facts.append([_strong(f"{label}: "), _text(value)])
    content.append(_bullet_list(facts))

    if update.commits:
        content.append(_paragraph(_strong("Commits that prompted this ticket")))
        content.append(_bullet_list([
            [
                _link(c["sha"], c["url"]) if c.get("url") else _code(c.get("sha", "")),
                _text("  " + (c.get("message") or "")),
                _text(f" — {c['author']}" if c.get("author") else ""),
            ]
            for c in update.commits
        ]))

    if update.url:
        content.append(_paragraph(_link("View on GitHub", update.url)))

    content.append(_paragraph(
        _strong("⚠ Opened automatically. "),
        _text(
            f"No existing issue matched this work, so {proposal.model} drafted this "
            f"ticket from the commits above. Retitle, re-scope or close it as needed — "
            f"and reference a ticket key in the branch name to stop this happening.",
            [{"type": "em"}],
        ),
    ))

    return {"type": "doc", "version": 1, "content": content}


def to_plain_text(doc: dict[str, Any]) -> str:
    """Flatten an ADF doc to text — for dry-run output and test assertions."""
    parts: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content") or []:
            walk(child)
        if node.get("type") in {"paragraph", "listItem"}:
            parts.append("\n")

    walk(doc)
    return "".join(parts).strip()
