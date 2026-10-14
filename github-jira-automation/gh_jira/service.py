"""Orchestration: a verified GitHub delivery in, Jira comments out.

The route (`router.py`) owns HTTP concerns — signature check, status codes. This
module owns the decision chain, which is deliberately a sequence of explicit
skips so that "nothing was written" always comes with a reason you can read in
the response body and the logs:

    disabled -> repo not allowed -> unsupported event -> already processed
             -> no issue key     -> dry run          -> post

Never raises into the request path. A GitHub webhook that returns 5xx gets
flagged as failing and eventually auto-disabled by GitHub, so a Jira outage must
not take the hook down with it — failures come back as `ok: false` inside a 200.
"""
from __future__ import annotations

import logging
from typing import Any

from . import adf, branch_links, config, dedupe, events, issue_keys, jira_client, matcher

log = logging.getLogger("gh_jira.service")


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "action": "skipped", "reason": reason, **extra}


def _ai_report(ai: Any | None) -> dict[str, Any] | None:
    """The inference, surfaced in the API response as well as the comment — so a
    wrong attribution is visible in GitHub's delivery log too, not only on Jira."""
    if ai is None:
        return None
    return {"key": ai.key, "confidence": round(ai.confidence, 2), "reason": ai.reason, "model": ai.model}


def _ai_assess(update) -> tuple[Any | None, Any | None]:
    """Infer the issue from the live backlog, or propose a new one.

    `(None, None)` on any failure — the caller then skips, exactly as it did
    before this path existed.
    """
    try:
        candidates = jira_client.search_issues(
            config.ai_candidate_jql(), limit=config.ai_max_candidates()
        )
        return matcher.assess(update, candidates)
    except Exception:  # noqa: BLE001 — inference is a bonus, never a dependency
        log.warning("AI issue matching failed", exc_info=True)
        return None, None


def _resolve(repo: str, update) -> tuple[list[Any], Any | None, str | None]:
    """Find the ticket this event belongs to. Returns `(matches, ai, created)`.

    **The branch is the unit of work.** A branch is bound to exactly one ticket
    the first time it is seen, and every later event on that branch resolves
    through the binding — no regex, no model call, no chance of drifting onto a
    different ticket because someone worded a commit differently.

    Order:
      1. an existing binding for this branch
      2. an existing binding for a branch this push MERGED (a push to `main`
         belongs to the ticket of the branch that landed, not to `main`)
      3. an explicit key in the branch / PR / commits
      4. an AI match against the live backlog
      5. a new ticket

    Whatever resolves at 3-5 is then bound, so step 1 answers next time.

    Trunk branches (`main`, `develop`, `release/*` …) are never bound: binding a
    shared branch would nail every future push to it onto one permanent ticket.
    """
    branch = update.branch

    if config.is_bindable(branch):
        bound = branch_links.get(repo, branch)
        if bound:
            return [issue_keys.KeyMatch(key=bound, source="branch_link", text=branch)], None, None

    # A merge landing on a trunk still belongs to the branch that produced it.
    for source_branch in getattr(update, "merge_sources", []):
        bound = branch_links.get(repo, source_branch)
        if bound:
            return (
                [issue_keys.KeyMatch(key=bound, source="merged_branch_link", text=source_branch)],
                None,
                None,
            )

    matches = issue_keys.resolve(update.key_sources)
    if matches:
        _bind(repo, branch, matches[0].key, source=matches[0].source)
        return matches, None, None

    if not config.ai_match():
        return [], None, None

    ai, proposal = _ai_assess(update)
    if ai:
        _bind(repo, branch, ai.key, source="ai")
        return [issue_keys.KeyMatch(key=ai.key, source="ai", text=ai.reason)], ai, None

    if proposal and config.auto_create():
        created_key = _create_ticket(update, proposal)
        if created_key:
            return (
                [issue_keys.KeyMatch(key=created_key, source="auto_created", text=proposal.summary)],
                None,
                created_key,
            )

    return [], None, None


def _bind(repo: str, branch: str | None, issue_key: str, source: str) -> None:
    """Bind a branch to its ticket, unless it is a shared trunk."""
    if config.is_bindable(branch):
        branch_links.put(repo, branch or "", issue_key, source=source)


def _create_ticket(update, proposal) -> str | None:
    """Open a ticket for work nothing on the board covers. Key, or None."""
    project = config.auto_create_project()
    doc = adf.new_issue_doc(update, proposal)
    result = jira_client.create_issue(
        project_key=project,
        summary=proposal.summary,
        description_doc=doc,
        issue_type=config.auto_create_type(),
        labels=config.auto_create_labels(),
    )
    if not result.get("ok"):
        log.warning("Could not open a ticket in %s: %s", project, result.get("error"))
        return None

    key = result.get("issue_key")
    log.info("Opened %s for %s@%s — %s", key, update.branch, update.repo, proposal.summary)
    # Bind it to the branch so the next push comments here instead of opening
    # a second ticket for the same work.
    _bind(update.repo, update.branch, key, source="auto_created")
    return key


def _target_status(update) -> str:
    """Which status this event should move the ticket to. Empty = leave alone."""
    if update.event == "pull_request":
        if update.action == "closed" and getattr(update, "merged", False):
            # The branch's work landed — the unit of work is finished.
            return config.transition_on_merge()
        if update.action in {"opened", "reopened", "ready_for_review", "review_requested"}:
            return config.transition_on_pr()
        return ""

    if update.event == "push":
        # A branch DELETION never closes a ticket: branches are routinely
        # deleted long after merging, and a branch abandoned without merging
        # would otherwise mark unfinished work Done.
        if not update.commits:
            return ""
        return config.transition_on_commit()

    return ""


def _apply_status(issue_keys_to_move: list[str], update) -> list[dict[str, Any]]:
    """Move the matched tickets to the status this event implies.

    Never fails the delivery: a ticket that could not be transitioned still got
    its comment, which is the part that carries the information.
    """
    target = _target_status(update)
    if not target:
        return []

    moves: list[dict[str, Any]] = []
    for key in issue_keys_to_move:
        try:
            result = jira_client.transition_issue(key, target)
        except Exception as exc:  # noqa: BLE001
            log.warning("Transitioning %s to %s failed", key, target, exc_info=True)
            result = {"ok": False, "error": str(exc)}
        moves.append({"issue_key": key, "target": target, **result})
    return moves


def handle(event: str, delivery_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Process one verified delivery. Blocking — call via run_in_threadpool."""
    if not config.enabled():
        return _skip("automation_disabled")

    if event == "ping":
        # GitHub's handshake when the webhook is first saved.
        return {"ok": True, "action": "pong", "zen": payload.get("zen")}

    repo = (payload.get("repository") or {}).get("full_name") or ""
    allowed = config.allowed_repos()
    if allowed and repo.lower() not in allowed:
        return _skip("repo_not_allowed", repo=repo, allowed=allowed)

    try:
        update = events.parse(event, payload)
    except events.UnsupportedEvent as exc:
        return _skip("unsupported_event", event=event, detail=str(exc))

    if dedupe.already_processed(delivery_id):
        return _skip("duplicate_delivery", delivery_id=delivery_id)

    matches, ai, created_key = _resolve(repo, update)

    if not matches:
        # The honest outcome: nothing names a ticket and nothing could be
        # inferred with confidence, so there is none to comment on.
        log.info(
            "No Jira issue key in %s on %s (branch=%s) — nothing written",
            event, repo, update.branch,
        )
        return _skip(
            "no_issue_key",
            event=event,
            repo=repo,
            branch=update.branch,
            searched=[name for name, _ in update.key_sources],
            ai_attempted=bool(config.ai_match()),
        )

    primary = matches[0]
    doc = adf.comment_doc(
        update, matched_source=primary.source, evidence=primary.text, ai_match=ai
    )

    if config.dry_run():
        return {
            "ok": True,
            "action": "dry_run",
            "event": event,
            "repo": repo,
            "issue_keys": [m.key for m in matches],
            "matched": [m.as_dict() for m in matches],
            "ai": _ai_report(ai),
            "preview": adf.to_plain_text(doc),
        }

    results: list[dict[str, Any]] = []
    for match in matches:
        try:
            results.append(jira_client.add_comment(match.key, doc))
        except Exception as exc:  # noqa: BLE001 — see module docstring
            log.warning("Commenting on %s failed", match.key, exc_info=True)
            results.append({"ok": False, "issue_key": match.key, "error": str(exc)})

    commented = [r["issue_key"] for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    # Status last: the comment is the payload, the transition is the flourish.
    # Only move tickets that actually received their update.
    moves = _apply_status(commented, update) if commented else []

    outcome = {
        "ok": not failed,
        "action": "commented" if commented else "failed",
        "event": event,
        "repo": repo,
        "branch": update.branch,
        "commented": commented,
        "created": created_key,
        "matched": [m.as_dict() for m in matches],
        "ai": _ai_report(ai),
        "transitions": moves,
        "failures": failed,
    }

    # Only a fully successful delivery is recorded, so a retry after a partial
    # Jira failure is allowed to finish the job rather than being deduped away.
    if not failed:
        dedupe.record(delivery_id, event, repo, commented, outcome)

    log.info(
        "%s on %s -> commented on %s%s",
        event, repo, commented or "nothing",
        f" (failed: {[f.get('issue_key') for f in failed]})" if failed else "",
    )
    return outcome
