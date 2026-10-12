"""End-to-end test of the deployed surface: real HTTP route, real signatures.

Where `test_gh_jira.py` unit-tests the pieces, this drives the ACTUAL FastAPI
app the way GitHub will — real `X-Hub-Signature-256` HMACs over real
GitHub-shaped payloads, through `POST /api/webhooks/github`. Only the Jira
transport is stubbed, so nothing is written to the board.

It exists because the unit tests cannot catch the failure modes that live in the
wiring: a webhook configured with the wrong content type, an unmounted route, a
signature computed over re-serialised JSON.

Run:

    python github-jira-automation/tests/test_end_to_end.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_PKG_ROOT)
sys.path.insert(0, _PKG_ROOT)
sys.path.insert(0, _REPO_ROOT)

SECRET = "test-webhook-secret"
REPO = "gandikediye-afk/Hermes-Agent-MVP"

os.environ["HERMES_DISABLE_DB"] = "1"   # must precede the import

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402  — the exact Vercel entrypoint
from gh_jira import adf, service  # noqa: E402

# AFTER the import, deliberately. `hermes/config.py` runs
# `load_dotenv(override=True)` at import time, so anything set before this line
# is replaced by the developer's real .env — including the live webhook secret,
# which would make every signature here fail against the wrong key. The gh_jira
# config reads env per call, so setting it now takes effect immediately.
os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
os.environ["GITHUB_JIRA_REPOS"] = REPO
os.environ["GITHUB_JIRA_AI_MATCH"] = "0"      # no network; explicit keys only
for _leftover in ("GITHUB_JIRA_DRY_RUN", "GITHUB_JIRA_ENABLED", "GITHUB_JIRA_PEOPLE"):
    os.environ.pop(_leftover, None)

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


class _StubJira:
    """Records every Jira write the delivery would have made."""

    def __init__(self):
        self.comments: list[tuple[str, str]] = []   # (issue_key, plain text)
        self.created: list[dict] = []
        self.transitions: list[tuple[str, str]] = []

    def add_comment(self, issue_key, doc, client=None):
        self.comments.append((issue_key, adf.to_plain_text(doc)))
        return {"ok": True, "issue_key": issue_key, "comment_id": "1", "status": 201}

    def search_issues(self, jql, limit=40):
        return []

    def create_issue(self, project_key, summary, description_doc, issue_type="Task", labels=None):
        self.created.append({"project": project_key, "summary": summary})
        return {"ok": True, "issue_key": f"KAN-{900 + len(self.created)}"}

    def transition_issue(self, issue_key, target_status, client=None):
        self.transitions.append((issue_key, target_status))
        return {"ok": True, "from": "To Do", "to": target_status}


_BINDINGS: dict[tuple[str, str], str] = {}


def _install_stub() -> _StubJira:
    stub = _StubJira()
    service.jira_client = stub                                # type: ignore[assignment]
    service.dedupe.already_processed = lambda _id: False      # type: ignore[assignment]
    service.dedupe.record = lambda *a, **k: None              # type: ignore[assignment]
    service.branch_links.get = lambda repo, branch: _BINDINGS.get((repo.lower(), branch))  # type: ignore[assignment]
    service.branch_links.put = lambda repo, branch, key, source="": _BINDINGS.__setitem__(  # type: ignore[assignment]
        (repo.lower(), branch), key
    )
    return stub


client = TestClient(app)


def deliver(event: str, payload: dict, delivery: str = "d-1", *,
            secret: str = SECRET, content_type: str = "application/json",
            sign: bool = True, form_encoded: bool = False):
    """Send a delivery exactly as GitHub would, and return the response."""
    if form_encoded:
        # GitHub's `application/x-www-form-urlencoded` mode: the JSON is wrapped
        # in a `payload=` form field rather than sent as the body.
        raw = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    else:
        raw = json.dumps(payload).encode()

    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": content_type,
        "User-Agent": "GitHub-Hookshot/abc123",
    }
    if sign:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/webhooks/github", content=raw, headers=headers)


# ── payload builders ─────────────────────────────────────────────────────────
def push(ref, commits, sender="haider-dev", pusher_email="haider@example.com", **extra):
    payload = {
        "ref": ref,
        "before": "0" * 40,
        "after": commits[-1]["id"] if commits else "0" * 40,
        "created": False, "deleted": False, "forced": False,
        "compare": f"https://github.com/{REPO}/compare/aaa...bbb",
        "repository": {"full_name": REPO, "default_branch": "main"},
        "sender": {"login": sender},
        "pusher": {"name": sender, "email": pusher_email},
        "commits": commits,
    }
    payload["head_commit"] = commits[-1] if commits else None
    payload.update(extra)
    return payload


def commit(sha, message, name, username, email, files=("app.py",)):
    return {
        "id": sha, "message": message,
        "author": {"name": name, "username": username, "email": email},
        "url": f"https://github.com/{REPO}/commit/{sha[:7]}",
        "added": [], "modified": list(files), "removed": [],
    }


def pull_request(action, head, merged=False, state="open", title="Add bounded retries", body=""):
    return {
        "action": action, "number": 42,
        "pull_request": {
            "number": 42, "title": title, "body": body,
            "html_url": f"https://github.com/{REPO}/pull/42",
            "state": state, "merged": merged, "draft": False,
            "head": {"ref": head}, "base": {"ref": "main"},
            "user": {"login": "haider-dev"},
            "requested_reviewers": [{"login": "auroob-dev"}],
            "merge_commit_sha": "7e3f9a10bc2d4455",
            "merged_by": {"login": "auroob-dev"} if merged else None,
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "haider-dev"},
    }


# ── the wiring GitHub's form gets wrong ──────────────────────────────────────
def test_the_route_is_actually_mounted():
    r = client.get("/api/webhooks/github/health")
    check("health endpoint responds", r.status_code == 200)
    body = r.json()
    check("reports the right path", body["endpoint"] == "/api/webhooks/github")
    check("secret is detected", body["config"]["secret_configured"] is True)
    check("lists the events to subscribe to", "push" in body["supported_events"])


def test_ping_is_answered():
    r = deliver("ping", {"zen": "Design for failure.", "hook_id": 1,
                         "repository": {"full_name": REPO}}, "d-ping")
    check("ping returns 200", r.status_code == 200)
    check("answered with a pong", r.json()["action"] == "pong")


def test_form_urlencoded_content_type_is_rejected_loudly():
    """GitHub's DEFAULT content type, which does not work.

    The form defaults to `application/x-www-form-urlencoded`, which wraps the
    JSON in a `payload=` field. This must fail visibly rather than silently
    doing nothing, so a misconfigured hook is obvious in the delivery log.
    """
    _install_stub()
    r = deliver("push", push("refs/heads/feature/KAN-24-x",
                             [commit("a" * 12, "KAN-24: work", "haider ali", "haider-dev", "haider@example.com")]),
                "d-form", content_type="application/x-www-form-urlencoded", form_encoded=True)
    check("rejected, not silently ignored", r.status_code == 400)
    check("the error names the cause", "JSON" in r.json()["error"])


def test_unsigned_and_forged_deliveries_are_rejected():
    r = deliver("push", push("refs/heads/x", []), "d-nosig", sign=False)
    check("unsigned rejected with 401", r.status_code == 401)

    r = deliver("push", push("refs/heads/x", []), "d-wrongsecret", secret="not-the-secret")
    check("wrong secret rejected with 401", r.status_code == 401)


def test_a_foreign_repo_is_ignored():
    _install_stub()
    payload = push("refs/heads/feature/KAN-24-x",
                   [commit("b" * 12, "KAN-24: work", "haider ali", "haider-dev", "haider@example.com")])
    payload["repository"]["full_name"] = "someone-else/other-repo"
    r = deliver("push", payload, "d-foreign")
    check("acknowledged with 200", r.status_code == 200)
    check("but skipped", r.json()["reason"] == "repo_not_allowed")


def test_an_unsupported_event_is_acknowledged():
    """'Send me everything' delivers issues, stars, forks — none must 500."""
    _install_stub()
    for event in ("issues", "star", "fork", "workflow_run", "check_suite"):
        r = deliver(event, {"repository": {"full_name": REPO}, "action": "created"}, f"d-{event}")
        check(f"{event} acknowledged with 200", r.status_code == 200)
        check(f"{event} written nothing", r.json()["reason"] == "unsupported_event")


# ── the real lifecycle ───────────────────────────────────────────────────────
def test_full_branch_lifecycle_over_http():
    """push -> push -> PR -> review -> merge -> push to main. One ticket."""
    _BINDINGS.clear()
    stub = _install_stub()
    branch = "feature/KAN-24-github-jira"

    # 1. Haider pushes; the branch names the ticket.
    r = deliver("push", push(f"refs/heads/{branch}", [
        commit("a1b2c3d4e5f6", "KAN-24: verify the webhook signature",
               "haider ali", "haider-dev", "haider@example.com",
               files=["gh_jira/signature.py"]),
    ]), "d-1")
    body = r.json()
    check("1. push accepted", r.status_code == 200 and body["ok"])
    check("   commented on KAN-24", body["commented"] == ["KAN-24"])
    check("   moved to In Progress", stub.transitions[-1] == ("KAN-24", "In Progress"))
    check("   branch bound", _BINDINGS[(REPO.lower(), branch)] == "KAN-24")
    text = stub.comments[-1][1]
    check("   names the pusher properly", "Pushed by: Haider Ali" in text)
    check("   carries the commit sha", "a1b2c3d" in text)
    check("   carries the commit message", "verify the webhook signature" in text)

    # 2. Auroob pushes to the same branch — no ticket key anywhere this time.
    r = deliver("push", push(f"refs/heads/{branch}", [
        commit("c4d8e12f3a4b", "cover the backoff with tests",
               "Auroob Fatima", "auroob-dev", "auroob@example.com", files=["tests/test_x.py"]),
    ], sender="auroob-dev", pusher_email="auroob@example.com"), "d-2")
    body = r.json()
    check("2. second push landed on the same ticket", body["commented"] == ["KAN-24"])
    check("   resolved via the branch binding", body["matched"][0]["source"] == "branch_link")
    check("   credited to Auroob", "Pushed by: Auroob Fatima" in stub.comments[-1][1])

    # 3. PR opened — title and body deliberately name no ticket.
    r = deliver("pull_request", pull_request("opened", branch, title="Bounded retries", body=""), "d-3")
    body = r.json()
    check("3. PR found the same ticket", body["commented"] == ["KAN-24"])
    check("   moved to In Review", stub.transitions[-1] == ("KAN-24", "In Review"))
    check("   reviewer named", "auroob-dev" in stub.comments[-1][1] or "Auroob" in stub.comments[-1][1])

    # 4. Review submitted.
    r = deliver("pull_request_review", {
        "action": "submitted",
        "review": {"state": "changes_requested", "body": "Fail closed on a missing secret.",
                   "user": {"login": "auroob-dev"},
                   "html_url": f"https://github.com/{REPO}/pull/42#r1"},
        "pull_request": {"number": 42, "title": "Bounded retries", "head": {"ref": branch}},
        "repository": {"full_name": REPO},
    }, "d-4")
    body = r.json()
    check("4. review commented on the ticket", body["commented"] == ["KAN-24"])
    check("   review did not change status", stub.transitions[-1] == ("KAN-24", "In Review"))
    check("   review note carried", "Fail closed" in stub.comments[-1][1])

    # 5. Merged.
    r = deliver("pull_request", pull_request("closed", branch, merged=True, state="closed",
                                             title="Bounded retries"), "d-5")
    body = r.json()
    check("5. merge commented on the ticket", body["commented"] == ["KAN-24"])
    check("   marked Done", stub.transitions[-1] == ("KAN-24", "Done"))

    # 6. The push to main that follows the merge.
    r = deliver("push", push("refs/heads/main", [
        commit("a1b2c3d4e5f6", "KAN-24: verify the webhook signature",
               "haider ali", "haider-dev", "haider@example.com"),
        commit("7e3f9a10bc2d", f"Merge pull request #42 from gandikediye-afk/{branch}",
               "Auroob Fatima", "auroob-dev", "auroob@example.com"),
    ], sender="auroob-dev", pusher_email="auroob@example.com"), "d-6")
    body = r.json()
    check("6. merge push landed on the same ticket", body["commented"] == ["KAN-24"])
    check("   main was never bound", (REPO.lower(), "main") not in _BINDINGS)
    check("   still exactly one binding", len(_BINDINGS) == 1)
    check("   no ticket was ever auto-created", stub.created == [])
    check("   six deliveries, six comments", len(stub.comments) == 6)
    check("   every comment on KAN-24", {k for k, _ in stub.comments} == {"KAN-24"})


def test_a_duplicate_delivery_is_not_posted_twice():
    _BINDINGS.clear()
    stub = _install_stub()
    service.dedupe.already_processed = lambda _id: True   # type: ignore[assignment]
    r = deliver("push", push("refs/heads/feature/KAN-24-x", [
        commit("d" * 12, "KAN-24: work", "haider ali", "haider-dev", "haider@example.com")]), "d-dup")
    check("acknowledged", r.status_code == 200)
    check("skipped as duplicate", r.json()["reason"] == "duplicate_delivery")
    check("nothing posted", stub.comments == [])


def test_a_branch_naming_no_ticket_writes_nothing_when_ai_is_off():
    _BINDINGS.clear()
    stub = _install_stub()
    r = deliver("push", push("refs/heads/chore/deps", [
        commit("e" * 12, "bump requests to 2.32.3", "haider ali", "haider-dev", "haider@example.com")]), "d-chore")
    check("acknowledged", r.status_code == 200)
    check("skipped with a reason", r.json()["reason"] == "no_issue_key")
    check("nothing posted", stub.comments == [])
    check("no ticket opened", stub.created == [])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} checks passed across {len(tests)} end-to-end tests.")
