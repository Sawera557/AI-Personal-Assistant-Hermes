"""The one Jira call KAN-81 did not ship: add a comment to an issue.

Everything else reuses what already exists — `hermes.maton.MatonClient` is the
transport (it injects the managed OAuth token and proxies native Jira REST), and
the cloud-id resolution mirrors `hermes/tools.py::_jira_cloud_id`.

Cloud id is cached per process. `hermes.tools` resolves it fresh on every call,
which is fine for an interactive agent turn but wasteful here: a busy repo can
fire several webhooks a second and each would otherwise pay an extra round trip
to `/oauth/token/accessible-resources`.

Imports of `hermes.*` are deferred into the functions so this module can be
imported (and unit-tested) without pulling in the whole Hermes config/env graph.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from . import config

log = logging.getLogger("gh_jira.jira")

_cloud_id: str | None = None
_lock = threading.Lock()


class JiraError(RuntimeError):
    pass


def _client() -> Any:
    from hermes.maton import MatonClient  # deferred: see module docstring

    # GITHUB_JIRA_MATON_API_KEY when set (the vendor team's own Maton account,
    # which is what holds the Jira connection), else the default MATON_API_KEY.
    return MatonClient(api_key=config.maton_api_key())


def cloud_id(client: Any | None = None, refresh: bool = False) -> str:
    """Resolve (and cache) the Jira Cloud id this connection can reach.

    Every Jira REST path needs it — `/ex/jira/{cloudId}/rest/api/3/...` — and
    Maton does not substitute it the way it does QuickBooks' `:realmId`.
    """
    global _cloud_id
    if _cloud_id and not refresh:
        return _cloud_id

    with _lock:
        if _cloud_id and not refresh:
            return _cloud_id
        client = client or _client()
        resp = client.request("jira", "GET", "/oauth/token/accessible-resources")
        if not resp.get("ok"):
            raise JiraError(
                f"Could not resolve the Jira site (accessible-resources returned {resp.get('status')})."
            )
        resources = resp.get("data") or []
        if not resources:
            raise JiraError("No accessible Jira site found for this Maton connection.")
        _cloud_id = resources[0]["id"]
        return _cloud_id


def add_comment(issue_key: str, doc: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
    """POST an ADF comment onto `issue_key`.

    Returns `{ok, status, comment_id, url, error}`. Never raises for an ordinary
    API rejection (a 404 for a key that does not exist is expected traffic when
    someone names a stale ticket in a branch) — the caller records the outcome
    per issue and the webhook still returns 200 so GitHub does not flag the hook
    as failing.
    """
    client = client or _client()
    site = cloud_id(client)
    resp = client.request(
        "jira",
        "POST",
        f"/ex/jira/{site}/rest/api/3/issue/{issue_key}/comment",
        body={"body": doc},
    )

    data = resp.get("data") or {}
    if not resp.get("ok"):
        # Jira's error envelope: {"errorMessages": [...], "errors": {...}}
        messages = data.get("errorMessages") if isinstance(data, dict) else None
        detail = "; ".join(messages) if messages else f"HTTP {resp.get('status')}"
        log.warning("Jira comment on %s failed: %s", issue_key, detail)
        return {"ok": False, "status": resp.get("status"), "issue_key": issue_key, "error": detail}

    comment_id = data.get("id") if isinstance(data, dict) else None
    return {
        "ok": True,
        "status": resp.get("status"),
        "issue_key": issue_key,
        "comment_id": comment_id,
        "url": data.get("self") if isinstance(data, dict) else None,
    }


def search_issues(jql: str, limit: int = 40, client: Any | None = None) -> list[dict[str, Any]]:
    """Candidate issues for the AI matcher, flattened to the fields it needs.

    Returns [] on any failure rather than raising: no candidates means the
    matcher declines and the push is skipped, which is the same safe outcome as
    a low-confidence match.

    Note `/search/jql`'s index lags writes by minutes, so a ticket created
    seconds ago may be missing here. That is acceptable for matching against an
    existing backlog and is why explicit keys never route through this path.
    """
    client = client or _client()
    try:
        site = cloud_id(client)
        resp = client.request(
            "jira", "GET", f"/ex/jira/{site}/rest/api/3/search/jql",
            query={
                "jql": jql,
                "maxResults": limit,
                "fields": "summary,status,labels,assignee,issuetype,parent",
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("Jira candidate search failed", exc_info=True)
        return []

    if not resp.get("ok"):
        log.warning("Jira candidate search returned %s", resp.get("status"))
        return []

    issues = (resp.get("data") or {}).get("issues") or []
    candidates: list[dict[str, Any]] = []
    for issue in issues:
        fields = issue.get("fields") or {}
        parent = fields.get("parent") or {}
        candidates.append({
            "key": issue.get("key"),
            "summary": fields.get("summary") or "",
            "status": ((fields.get("status") or {}).get("name")) or "",
            "labels": fields.get("labels") or [],
            "assignee": ((fields.get("assignee") or {}).get("displayName")) or None,
            "type": ((fields.get("issuetype") or {}).get("name")) or "",
            "epic": ((parent.get("fields") or {}).get("summary")) if parent else None,
        })
    return [c for c in candidates if c["key"]]


def create_issue(
    project_key: str,
    summary: str,
    description_doc: dict[str, Any],
    issue_type: str = "Task",
    labels: list[str] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Open a new issue. Returns `{ok, issue_key, url, error}`.

    Used only when a push matches nothing on the board. The caller is
    responsible for making sure this happens once per branch, not once per
    push — see `branch_links.py`.
    """
    client = client or _client()
    site = cloud_id(client)
    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary[:255],
        "issuetype": {"name": issue_type},
        "description": description_doc,
    }
    if labels:
        # Jira rejects labels containing spaces with a confusing 400.
        fields["labels"] = [label.replace(" ", "-") for label in labels]

    resp = client.request("jira", "POST", f"/ex/jira/{site}/rest/api/3/issue", body={"fields": fields})
    data = resp.get("data") or {}
    if not resp.get("ok"):
        messages = data.get("errorMessages") if isinstance(data, dict) else None
        errors = data.get("errors") if isinstance(data, dict) else None
        detail = "; ".join(messages or []) or str(errors or f"HTTP {resp.get('status')}")
        log.warning("Creating an issue in %s failed: %s", project_key, detail)
        return {"ok": False, "error": detail, "status": resp.get("status")}

    return {"ok": True, "issue_key": data.get("key"), "url": data.get("self")}


def current_status(issue_key: str, client: Any | None = None) -> str | None:
    """The issue's status name, or None if it could not be read."""
    client = client or _client()
    site = cloud_id(client)
    resp = client.request(
        "jira", "GET", f"/ex/jira/{site}/rest/api/3/issue/{issue_key}", query={"fields": "status"}
    )
    if not resp.get("ok"):
        return None
    return (((resp.get("data") or {}).get("fields") or {}).get("status") or {}).get("name")


def transition_issue(issue_key: str, target_status: str, client: Any | None = None) -> dict[str, Any]:
    """Move an issue to `target_status`, matching on the status NAME.

    Names rather than hard-coded ids: transition ids are per-workflow and change
    when the board is edited, and a stale id silently moves tickets to the wrong
    column. The name is what people actually agreed on.

    Returns `{ok, from, to, skipped}`. A no-op (already there, protected status,
    no such transition) is reported as `ok` with a reason — none of those are
    failures worth surfacing as a broken webhook.
    """
    client = client or _client()
    site = cloud_id(client)

    status_now = current_status(issue_key, client)
    if status_now and status_now.lower() == target_status.lower():
        return {"ok": True, "skipped": "already_in_status", "from": status_now, "to": status_now}

    if status_now and status_now.lower() in config.protected_statuses():
        # Never drag finished work back onto the board off the back of a push.
        log.info("%s is %s (protected) — not transitioning", issue_key, status_now)
        return {"ok": True, "skipped": "protected_status", "from": status_now, "to": status_now}

    resp = client.request("jira", "GET", f"/ex/jira/{site}/rest/api/3/issue/{issue_key}/transitions")
    if not resp.get("ok"):
        return {"ok": False, "error": f"could not list transitions (HTTP {resp.get('status')})"}

    available = (resp.get("data") or {}).get("transitions") or []
    chosen = next(
        (t for t in available if (t.get("to") or {}).get("name", "").lower() == target_status.lower()),
        None,
    ) or next(
        (t for t in available if t.get("name", "").lower() == target_status.lower()),
        None,
    )
    if not chosen:
        names = [(t.get("to") or {}).get("name") for t in available]
        log.info("No transition to %r on %s; available: %s", target_status, issue_key, names)
        return {"ok": True, "skipped": "no_such_transition", "from": status_now, "available": names}

    done = client.request(
        "jira", "POST", f"/ex/jira/{site}/rest/api/3/issue/{issue_key}/transitions",
        body={"transition": {"id": chosen["id"]}},
    )
    if not done.get("ok"):
        return {"ok": False, "error": f"transition failed (HTTP {done.get('status')})", "from": status_now}

    return {"ok": True, "from": status_now, "to": (chosen.get("to") or {}).get("name") or target_status}


def reset_cache() -> None:
    """Drop the cached cloud id — for tests and for re-pointing at a new site."""
    global _cloud_id
    _cloud_id = None
