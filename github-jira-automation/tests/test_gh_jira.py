"""Offline tests for the GitHub -> Jira push automation (KAN-24 / HRM-15).

No network, no database, no secrets — the Jira transport is stubbed, so this runs
identically on a clean checkout and in CI. Matches the repo's existing test style
(plain asserts, runnable with `python <file>`).

Run:

    python github-jira-automation/tests/test_gh_jira.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)                 # github-jira-automation/
_REPO_ROOT = os.path.dirname(_PKG_ROOT)            # repo root, for `hermes.*`
sys.path.insert(0, _PKG_ROOT)
sys.path.insert(0, _REPO_ROOT)
os.environ["HERMES_DISABLE_DB"] = "1"

from gh_jira import adf, config, events, issue_keys, matcher, people, service, signature  # noqa: E402

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def _env(**values: str | None) -> None:
    """Set/clear env vars — config reads them per call, so this takes effect."""
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _reset_env() -> None:
    """Clear EVERY variable the package reads.

    Must list all of them: a variable left out stays set from whichever test
    last wrote it and silently changes the behaviour of every test that runs
    afterwards — which is how a forgotten GITHUB_JIRA_AI_MATCH=0 turned off AI
    matching for the rest of the suite.
    """
    _env(
        GITHUB_JIRA_ENABLED=None,
        GITHUB_JIRA_DRY_RUN=None,
        # "*" = allow any repo, so fixtures are not coupled to the shipped
        # default allowlist. test_default_repo_allowlist covers that default.
        GITHUB_JIRA_REPOS="*",
        GITHUB_JIRA_PROJECT_KEYS=None,
        GITHUB_JIRA_DEFAULT_ISSUE_KEY=None,
        GITHUB_JIRA_MAX_COMMITS=None,
        GITHUB_JIRA_AI_MATCH=None,
        GITHUB_JIRA_AI_MODEL=None,
        GITHUB_JIRA_AI_MIN_CONFIDENCE=None,
        GITHUB_JIRA_AI_JQL=None,
        GITHUB_JIRA_AI_CANDIDATES=None,
        GITHUB_JIRA_PEOPLE=None,
        GITHUB_JIRA_AUTO_CREATE=None,
        GITHUB_JIRA_AUTO_CREATE_PROJECT=None,
        GITHUB_JIRA_AUTO_CREATE_TYPE=None,
        GITHUB_JIRA_AUTO_CREATE_LABELS=None,
        GITHUB_JIRA_STATUS_ON_COMMIT=None,
        GITHUB_JIRA_STATUS_ON_PR=None,
        GITHUB_JIRA_PROTECTED_STATUSES=None,
    )
    # Same reasoning as the env vars: a stubbed model response left behind by
    # the previous test silently changes what every later test does. Reset it to
    # "the model is unavailable", so a test that wants inference must ask for it.
    matcher._call_model = lambda system, user, model: None  # type: ignore[assignment]


class _StubJira:
    """Stands in for gh_jira.jira_client — records every write it would make."""

    def __init__(self, ok: bool = True, candidates=None, status="To Do"):
        self.ok = ok
        self.calls: list[tuple[str, dict]] = []        # add_comment
        self.created: list[dict] = []                  # create_issue
        self.transitions: list[tuple[str, str]] = []   # transition_issue
        self._candidates = candidates or []
        self._status = status

    def add_comment(self, issue_key, doc, client=None):
        self.calls.append((issue_key, doc))
        if not self.ok:
            return {"ok": False, "issue_key": issue_key, "error": "boom"}
        return {"ok": True, "issue_key": issue_key, "comment_id": "10001", "status": 201}

    def search_issues(self, jql, limit=40):
        return self._candidates

    def create_issue(self, project_key, summary, description_doc, issue_type="Task", labels=None):
        self.created.append({
            "project": project_key, "summary": summary, "type": issue_type,
            "labels": labels or [], "doc": description_doc,
        })
        return {"ok": True, "issue_key": f"KAN-{900 + len(self.created)}"}

    def transition_issue(self, issue_key, target_status, client=None):
        self.transitions.append((issue_key, target_status))
        return {"ok": True, "from": self._status, "to": target_status}


def _with_stub(stub: _StubJira):
    """Swap the Jira client and every DB-backed helper out of the service path."""
    service.jira_client = stub                              # type: ignore[assignment]
    service.dedupe.already_processed = lambda _id: False    # type: ignore[assignment]
    service.dedupe.record = lambda *a, **k: None            # type: ignore[assignment]
    service.branch_links.get = lambda repo, branch: None    # type: ignore[assignment]
    service.branch_links.put = lambda *a, **k: None         # type: ignore[assignment]


# ── payload fixtures ─────────────────────────────────────────────────────────
def _push(ref="refs/heads/feature/KAN-24-webhook", commits=None, **extra):
    payload = {
        "ref": ref,
        "before": "0" * 40,
        "after": "a1b2c3d4e5f6" + "0" * 28,
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": "https://github.com/gandikediye-afk/Hermes-Agent-MVP/compare/aaa...bbb",
        "repository": {"full_name": "gandikediye-afk/Hermes-Agent-MVP"},
        "pusher": {"name": "haider"},
        "sender": {"login": "haider"},
        "commits": commits if commits is not None else [
            {
                "id": "a1b2c3d4e5f6a7b8",
                "message": "add signature check\n\nlonger body",
                "author": {"name": "haider ali"},
                "url": "https://github.com/x/y/commit/a1b2c3d",
            }
        ],
    }
    payload["head_commit"] = payload["commits"][-1] if payload["commits"] else None
    payload.update(extra)
    return payload


def _pr(action="opened", merged=False, state="open", **pr_extra):
    pr = {
        "number": 42,
        "title": "KAN-24 GitHub to Jira push automation",
        "body": "Implements the webhook.",
        "html_url": "https://github.com/gandikediye-afk/Hermes-Agent-MVP/pull/42",
        "state": state,
        "merged": merged,
        "draft": False,
        "head": {"ref": "feature/kan-24-webhook"},
        "base": {"ref": "main"},
        "user": {"login": "haider"},
        "requested_reviewers": [{"login": "sawera"}],
        "merge_commit_sha": "deadbeefcafe1234",
        "merged_by": {"login": "sawera"} if merged else None,
    }
    pr.update(pr_extra)
    return {
        "action": action,
        "number": pr["number"],
        "pull_request": pr,
        "repository": {"full_name": "gandikediye-afk/Hermes-Agent-MVP"},
        "sender": {"login": "haider"},
    }


# ── signature ────────────────────────────────────────────────────────────────
def test_signature_accepts_a_correct_digest():
    secret, body = "s3cret", b'{"zen":"hi"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    check("valid sha256 signature verifies", signature.verify(secret, body, f"sha256={digest}"))
    check("uppercase hex verifies too", signature.verify(secret, body, f"sha256={digest.upper()}"))


def test_signature_rejects_everything_else():
    secret, body = "s3cret", b'{"zen":"hi"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    check("wrong secret rejected", not signature.verify("other", body, f"sha256={digest}"))
    check("tampered body rejected", not signature.verify(secret, body + b" ", f"sha256={digest}"))
    check("missing header rejected", not signature.verify(secret, body, None))
    check("sha1 scheme rejected", not signature.verify(secret, body, f"sha1={digest}"))
    check("empty secret rejected", not signature.verify("", body, f"sha256={digest}"))
    check("garbage header rejected", not signature.verify(secret, body, "nonsense"))


# ── issue-key resolution ─────────────────────────────────────────────────────
def test_extract_finds_keys_and_respects_the_allowlist():
    _reset_env()
    check("uppercase key found", issue_keys.extract("KAN-24 done", ["KAN"]) == ["KAN-24"])
    check("lowercase branch key found", issue_keys.extract("feature/kan-24-x", ["KAN"]) == ["KAN-24"])
    check("other project filtered out", issue_keys.extract("HRM-15 fix", ["KAN"]) == [])
    check("de-duplicated in order", issue_keys.extract("KAN-24 and KAN-9 and KAN-24", ["KAN"]) == ["KAN-24", "KAN-9"])


def test_extract_does_not_invent_keys_from_ordinary_hyphenated_words():
    _reset_env()
    noise = "fix utf-8 handling, bump covid-19 dataset, sha-1 checksum"
    check("no allowlist: lowercase noise ignored", issue_keys.extract(noise, ["*"]) == [])
    check("allowlist: noise ignored", issue_keys.extract(noise, ["KAN"]) == [])
    check("uppercase noise still matches with no allowlist", issue_keys.extract("UTF-8 fix", ["*"]) == ["UTF-8"])


def test_resolve_prefers_the_branch_over_commit_messages():
    _reset_env()
    matches = issue_keys.resolve([("branch", "feature/KAN-24-x"), ("commit", "KAN-99 unrelated")])
    check("one match returned", len(matches) == 1)
    check("branch wins", matches[0].key == "KAN-24")
    check("provenance recorded", matches[0].source == "branch")


def test_provenance_footer_stays_on_one_line():
    """A push carries every commit message joined by newlines as one key source.

    Quoted raw in the footer that spills across several lines and buries the
    sentence it belongs to, so it is collapsed and capped.
    """
    _reset_env()
    blob = "KAN-24: verify signature\nKAN-24: resolve issue key\nMerge pull request #42 from x/feature/kan-24"
    match = issue_keys.resolve([("commit", blob)])[0]
    check("collapsed to one line", "\n" not in match.text)
    check("still readable", match.text.startswith("KAN-24: verify signature KAN-24:"))

    long_blob = "KAN-24: " + " ".join(f"change number {i}" for i in range(40))
    long_match = issue_keys.resolve([("commit", long_blob)])[0]
    check("capped in length", len(long_match.text) <= 120)
    check("truncation is visible", long_match.text.endswith("…"))


def test_resolve_returns_nothing_rather_than_guessing():
    _reset_env()
    check("unlabelled branch resolves to nothing", issue_keys.resolve([("branch", "hotfix/typo")]) == [])


def test_resolve_uses_the_default_key_only_when_configured():
    _reset_env()
    _env(GITHUB_JIRA_DEFAULT_ISSUE_KEY="KAN-7")
    matches = issue_keys.resolve([("branch", "hotfix/typo")])
    check("fallback applied", [m.key for m in matches] == ["KAN-7"])
    check("fallback is labelled as such", matches[0].source == "default")
    _reset_env()


# ── event parsing ────────────────────────────────────────────────────────────
def test_push_parses_branch_commits_and_pusher():
    _reset_env()
    update = events.parse("push", _push())
    check("branch extracted from ref", update.branch == "feature/KAN-24-webhook")
    check("headline counts commits", update.headline.startswith("1 commit pushed to"))
    check("commit sha shortened to 7", update.commits[0]["sha"] == "a1b2c3d")
    check("only the first commit line kept", update.commits[0]["message"] == "add signature check")
    check("pusher recorded, tidied", ("Pushed by", "Haider") in update.facts)
    check("commit author read off the commit", ("Written by", "Haider Ali") in update.facts)
    check("branch is the first key source", update.key_sources[0][0] == "branch")


def test_push_flags_force_push_created_and_deleted():
    _reset_env()
    forced = events.parse("push", _push(forced=True))
    check("force push is called out", any("Force push" == label for label, _ in forced.facts))
    created = events.parse("push", _push(created=True))
    check("branch creation in headline", "created" in created.headline)
    deleted = events.parse("push", _push(deleted=True))
    check("branch deletion in headline", "deleted" in deleted.headline)


def test_push_to_a_tag_is_unsupported():
    _reset_env()
    try:
        events.parse("push", _push(ref="refs/tags/v1.0.0"))
        check("tag push raises UnsupportedEvent", False)
    except events.UnsupportedEvent:
        check("tag push raises UnsupportedEvent", True)


def test_push_reports_commits_it_did_not_list():
    _reset_env()
    _env(GITHUB_JIRA_MAX_COMMITS="2")
    many = [
        {"id": f"{i}" * 12, "message": f"commit {i}", "author": {"name": "haider"}, "url": ""}
        for i in range(5)
    ]
    update = events.parse("push", _push(commits=many))
    check("listing capped", len(update.commits) == 2)
    check("omitted count is exact", update.commits_omitted == 3)
    text = adf.to_plain_text(adf.comment_doc(update))
    check("comment says what was omitted", "3 more commits not listed" in text)
    _reset_env()


def test_pull_request_carries_status_reviewers_and_merge_state():
    _reset_env()
    opened = events.parse("pull_request", _pr())
    facts = dict(opened.facts)
    check("branch shows head -> base", facts["Branch"] == "feature/kan-24-webhook -> main")
    check("open PR status", facts["Status"] == "open")
    check("reviewers listed", facts["Reviewers requested"] == "sawera")

    merged = events.parse("pull_request", _pr(action="closed", merged=True, state="closed"))
    mfacts = dict(merged.facts)
    check("merge state names the merger", mfacts["Status"] == "merged by sawera")
    check("merge commit shortened", mfacts["Merge commit"] == "deadbee")
    check("headline says merged", "merged" in merged.headline)

    closed = events.parse("pull_request", _pr(action="closed", merged=False, state="closed"))
    check("closed-unmerged is distinguished", dict(closed.facts)["Status"] == "closed without merging")


def test_pull_request_review_records_the_reviewer_and_decision():
    _reset_env()
    payload = {
        "action": "submitted",
        "review": {
            "state": "approved",
            "body": "Looks good.",
            "user": {"login": "sawera"},
            "html_url": "https://github.com/x/y/pull/42#review-1",
        },
        "pull_request": {"number": 42, "title": "KAN-24 automation", "head": {"ref": "feature/kan-24-webhook"}},
        "repository": {"full_name": "gandikediye-afk/Hermes-Agent-MVP"},
    }
    update = events.parse("pull_request_review", payload)
    facts = dict(update.facts)
    check("reviewer captured", facts["Reviewer"] == "sawera")
    check("decision captured", facts["Decision"] == "approved")
    check("review note carried over", facts["Review note"] == "Looks good.")


# ── ADF rendering ────────────────────────────────────────────────────────────
def test_comment_doc_is_valid_adf():
    _reset_env()
    doc = adf.comment_doc(events.parse("push", _push()), matched_source="branch", evidence="feature/KAN-24-webhook")
    check("doc node", doc["type"] == "doc" and doc["version"] == 1)
    check("content is a non-empty list", isinstance(doc["content"], list) and doc["content"])
    check("every top-level node is typed", all("type" in node for node in doc["content"]))
    check("serialises as JSON", isinstance(json.dumps(doc), str))
    text = adf.to_plain_text(doc)
    check("headline present", "pushed to" in text)
    check("commit present", "add signature check" in text)
    check("provenance footer present", "matched from the branch" in text)


# ── service orchestration ────────────────────────────────────────────────────
def test_push_with_a_key_comments_on_the_issue():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-1", _push())
    check("reported as commented", result["action"] == "commented")
    check("commented on KAN-24", result["commented"] == ["KAN-24"])
    check("exactly one Jira call", len(stub.calls) == 1)
    check("the call targeted KAN-24", stub.calls[0][0] == "KAN-24")


def test_push_without_a_key_writes_nothing():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-2", _push(ref="refs/heads/hotfix/typo", commits=[]))
    check("skipped", result["action"] == "skipped")
    check("reason is explicit", result["reason"] == "no_issue_key")
    check("nothing posted to Jira", stub.calls == [])


def test_merge_to_main_reports_the_commits_that_landed():
    """The progress update that matters most: a PR landing on main.

    The branch is now `main`, which names no ticket, so resolution falls to the
    merge branch or the commit messages — whichever hits first. This test owns
    the CONTENT of that comment; the two tests above own which source wins.
    """
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    merge_push = _push(
        ref="refs/heads/main",
        commits=[
            {"id": "aaa1111bbb22", "message": "KAN-24: verify webhook signature",
             "author": {"name": "haider ali"}, "url": "https://github.com/x/y/commit/aaa1111"},
            {"id": "ccc3333ddd44", "message": "Merge pull request #42 from gandikediye-afk/feature/kan-24-webhook",
             "author": {"name": "sawera"}, "url": "https://github.com/x/y/commit/ccc3333"},
        ],
    )
    result = service.handle("push", "d-merge", merge_push)
    check("merge push comments on the ticket", result["commented"] == ["KAN-24"])
    check("not credited to the bare `main` branch", result["matched"][0]["source"] != "branch")
    text = adf.to_plain_text(stub.calls[0][1])
    check("the landed commits are in the comment", "verify webhook signature" in text)
    check("the merge commit is in the comment", "Merge pull request #42" in text)


def test_merge_commit_recovers_the_source_branch():
    """Unprefixed commits still reach the ticket via the merge commit's branch.

    The realistic failure this covers: the team writes plain commit messages, so
    the only KAN reference in a push to main is the one GitHub itself puts in
    the merge subject.
    """
    _reset_env()
    check(
        "PR merge subject parsed",
        events.merge_source_branch("Merge pull request #42 from gandikediye-afk/feature/KAN-24-webhook")
        == "feature/KAN-24-webhook",
    )
    check(
        "plain branch merge parsed",
        events.merge_source_branch("Merge branch 'feature/kan-24-webhook' into main")
        == "feature/kan-24-webhook",
    )
    check("ordinary commit is not a merge", events.merge_source_branch("KAN-24: add check") is None)

    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-merge-ref", _push(
        ref="refs/heads/main",
        commits=[
            {"id": "aaa1111bbb22", "message": "verify the webhook signature",
             "author": {"name": "haider ali"}, "url": ""},
            {"id": "bbb2222ccc33", "message": "resolve the issue key",
             "author": {"name": "haider ali"}, "url": ""},
            {"id": "ccc3333ddd44", "message": "Merge pull request #42 from gandikediye-afk/feature/KAN-24-webhook",
             "author": {"name": "sawera"}, "url": ""},
        ],
    ))
    check("still lands on the ticket", result["commented"] == ["KAN-24"])
    check("credited to the merge branch", result["matched"][0]["source"] == "merge_branch")
    text = adf.to_plain_text(stub.calls[0][1])
    check("unprefixed commits still listed", "verify the webhook signature" in text)


def test_commit_messages_remain_a_source_alongside_the_merge_branch():
    """Both paths stay live — a prefixed push with no merge commit still works."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-direct", _push(
        ref="refs/heads/main",
        commits=[{"id": "eee5555fff66", "message": "KAN-24: hotfix the signature check",
                  "author": {"name": "haider ali"}, "url": ""}],
    ))
    check("direct push to main still lands", result["commented"] == ["KAN-24"])
    check("credited to the commit message", result["matched"][0]["source"] == "commit")


def test_merged_pr_comment_reads_as_a_progress_update():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("pull_request", "d-merged", _pr(action="closed", merged=True, state="closed"))
    check("merged PR comments on the ticket", result["commented"] == ["KAN-24"])
    text = adf.to_plain_text(stub.calls[0][1])
    check("says the PR merged", "Pull request #42 merged" in text)
    check("names who merged it", "merged by sawera" in text)
    check("links back to the PR", "View on GitHub" in text)


def test_repo_allowlist_blocks_other_repos():
    _reset_env()
    _env(GITHUB_JIRA_REPOS="gandikediye-afk/Hermes-Agent-MVP")
    stub = _StubJira()
    _with_stub(stub)
    payload = _push()
    payload["repository"]["full_name"] = "someone-else/other-repo"
    result = service.handle("push", "d-3", payload)
    check("foreign repo skipped", result["reason"] == "repo_not_allowed")
    check("nothing posted", stub.calls == [])
    _reset_env()


def test_duplicate_delivery_is_not_posted_twice():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    service.dedupe.already_processed = lambda _id: True  # type: ignore[assignment]
    result = service.handle("push", "d-4", _push())
    check("duplicate skipped", result["reason"] == "duplicate_delivery")
    check("nothing posted", stub.calls == [])
    service.dedupe.already_processed = lambda _id: False  # type: ignore[assignment]


def test_dry_run_resolves_but_does_not_post():
    _reset_env()
    _env(GITHUB_JIRA_DRY_RUN="1")
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-5", _push())
    check("reported as dry run", result["action"] == "dry_run")
    check("issue still resolved", result["issue_keys"] == ["KAN-24"])
    check("preview rendered", "add signature check" in result["preview"])
    check("nothing posted", stub.calls == [])
    _reset_env()


def test_disabled_switch_stops_everything():
    _reset_env()
    _env(GITHUB_JIRA_ENABLED="0")
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-6", _push())
    check("skipped when disabled", result["reason"] == "automation_disabled")
    check("nothing posted", stub.calls == [])
    _reset_env()


def test_ping_is_acknowledged():
    _reset_env()
    _with_stub(_StubJira())
    result = service.handle("ping", "d-7", {"zen": "Design for failure."})
    check("ping answered", result["action"] == "pong")


def test_jira_failure_is_reported_not_raised():
    _reset_env()
    stub = _StubJira(ok=False)
    _with_stub(stub)
    result = service.handle("push", "d-8", _push())
    check("failure reported", result["ok"] is False and result["action"] == "failed")
    check("failure detail carried", result["failures"][0]["issue_key"] == "KAN-24")
    check("no exception escaped", isinstance(result, dict))


def test_unsupported_event_is_acknowledged_without_writing():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("issue_comment", "d-9", {"repository": {"full_name": "a/b"}})
    check("unsupported event skipped", result["reason"] == "unsupported_event")
    check("nothing posted", stub.calls == [])


# ── AI issue matching ────────────────────────────────────────────────────────
_CANDIDATES = [
    {"key": "KAN-24", "summary": "GitHub to Jira push automation", "status": "Idea",
     "labels": ["devops"], "epic": "Hermes - Automation and DevOps"},
    {"key": "KAN-30", "summary": "Voice agent latency tuning", "status": "In Progress",
     "labels": ["voice"], "epic": None},
]


def _fake_model(payload):
    """Replace the network call with a canned JSON response."""
    matcher._call_model = lambda system, user, model: payload  # type: ignore[assignment]


def _unlabelled_push():
    return _push(
        ref="refs/heads/webhook-work",
        commits=[{"id": "aaa1111bbb22", "message": "verify signatures and post the comment",
                  "author": {"name": "haider ali"}, "url": "",
                  "modified": ["github-jira-automation/gh_jira/router.py"]}],
    )


def test_ai_matcher_picks_a_candidate_and_reports_its_reasoning():
    _reset_env()
    _fake_model({"issue_key": "KAN-24", "confidence": 0.88, "reason": "Adds the GitHub webhook that comments on Jira."})
    m = matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES)
    check("a match came back", m is not None)
    check("the right candidate", m.key == "KAN-24")
    check("confidence carried", m.confidence == 0.88)
    check("reason carried", "webhook" in m.reason.lower())


def test_ai_matcher_accepts_none_as_an_answer():
    _reset_env()
    _fake_model({"issue_key": None, "confidence": 0.0, "reason": "Routine dependency bump, no ticket covers it."})
    m = matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES)
    check("declining is honoured", m is None)


def test_ai_matcher_discards_a_key_outside_the_candidate_set():
    """The closed-set guard: a hallucinated key must never reach Jira."""
    _reset_env()
    _fake_model({"issue_key": "KAN-999", "confidence": 0.99, "reason": "Very confident about a ticket that does not exist."})
    check("invented key rejected", matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES) is None)


def test_ai_matcher_enforces_the_confidence_floor():
    _reset_env()
    _fake_model({"issue_key": "KAN-24", "confidence": 0.55, "reason": "Might be this one."})
    check("below the default floor, discarded", matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES) is None)

    _env(GITHUB_JIRA_AI_MIN_CONFIDENCE="0.5")
    m = matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES)
    check("a lowered floor lets it through", m is not None and m.key == "KAN-24")
    _reset_env()


def test_ai_matcher_survives_a_broken_model_response():
    _reset_env()
    for payload in (None, {}, {"issue_key": ""}, {"issue_key": "KAN-24", "confidence": "high"}):
        _fake_model(payload)
        check(f"declines on {payload!r}", matcher.match(events.parse("push", _unlabelled_push()), _CANDIDATES) is None)


def test_ai_match_never_overrides_an_explicit_key():
    """A key in the branch is a human stating intent — no model gets a vote."""
    _reset_env()
    called = {"n": 0}

    def _spy(system, user, model):
        called["n"] += 1
        return {"issue_key": "KAN-30", "confidence": 0.99, "reason": "hijack attempt"}

    matcher._call_model = _spy  # type: ignore[assignment]
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-explicit", _push())   # branch names KAN-24
    check("explicit key wins", result["commented"] == ["KAN-24"])
    check("the model was never consulted", called["n"] == 0)
    check("no AI attribution recorded", result["ai"] is None)


def test_ai_matched_comment_discloses_that_it_was_inferred():
    _reset_env()
    _fake_model({"issue_key": "KAN-24", "confidence": 0.82, "reason": "Adds the webhook receiver this ticket describes."})
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)

    result = service.handle("push", "d-ai", _unlabelled_push())
    check("an unlabelled push now lands", result["commented"] == ["KAN-24"])
    check("source recorded as ai", result["matched"][0]["source"] == "ai")
    check("confidence surfaced in the response", result["ai"]["confidence"] == 0.82)
    check("model surfaced in the response", result["ai"]["model"] == "gpt-4.1-nano")

    text = adf.to_plain_text(stub.calls[0][1])
    check("comment flags the guess", "chosen by AI" in text)
    check("comment states the confidence", "82%" in text)
    check("comment states the reason", "webhook receiver" in text)
    check("comment tells you how to stop guessing", "Reference the key in your branch" in text)


def test_ai_match_can_be_switched_off():
    _reset_env()
    _env(GITHUB_JIRA_AI_MATCH="0")
    _fake_model({"issue_key": "KAN-24", "confidence": 0.99, "reason": "sure"})
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-off", _unlabelled_push())
    check("falls back to skipping", result["reason"] == "no_issue_key")
    check("response says AI was not attempted", result["ai_attempted"] is False)
    check("nothing posted", stub.calls == [])
    _reset_env()


def test_default_ai_model_is_the_cheap_one():
    _reset_env()
    check("defaults to gpt-4.1-nano", config.ai_model() == "gpt-4.1-nano")
    check("candidate JQL is scoped to the allowed projects", "project in (KAN)" in config.ai_candidate_jql())
    check("candidate JQL excludes resolved work", "Unresolved" in config.ai_candidate_jql())


# ── the three tracked repos ──────────────────────────────────────────────────
_TRACKED = (
    "gandikediye-afk/Hermes-Agent-MVP",
    "gandikediye-afk/Jira-Github-update-automation",
    "gandikediye-afk/pinecone-mcp-claude",
    "gandikediye-afk/Hermes-MVP-DataIngestion",
)


def test_default_repo_allowlist_covers_every_tracked_repo():
    _env(GITHUB_JIRA_REPOS=None)
    allowed = config.allowed_repos()
    for repo in _TRACKED:
        check(f"tracks {repo}", repo.lower() in allowed)
    check("nothing else is tracked by default", len(allowed) == len(_TRACKED))
    _reset_env()


def test_each_tracked_repo_is_processed():
    _reset_env()
    _env(GITHUB_JIRA_REPOS=None)   # the shipped default
    for repo in _TRACKED:
        stub = _StubJira()
        _with_stub(stub)
        payload = _push()
        payload["repository"]["full_name"] = repo
        result = service.handle("push", f"d-{repo}", payload)
        check(f"{repo} commented", result.get("commented") == ["KAN-24"])
    _reset_env()


# ── who pushed ───────────────────────────────────────────────────────────────
def test_gandikediye_pushes_are_attributed_to_sawera():
    """That account's commits are Sawera's work — naming Gandi credits the
    wrong person for it on a ticket people are judged from."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    payload = _push()
    payload["sender"] = {"login": "gandikediye-afk"}
    payload["pusher"] = {"name": "gandikediye-afk"}
    payload["commits"][0]["author"] = {"name": "gandikediye-afk", "username": "gandikediye-afk"}
    service.handle("push", "d-name", payload)
    text = adf.to_plain_text(stub.calls[0][1])
    check("pusher shown as Sawera", "Pushed by: Sawera" in text)
    check("commit author shown as Sawera", "— Sawera" in text)
    # The login still appears inside the repo path and compare URL, which is
    # correct — it is the org there, not a claim about who did the work.
    check("never attributed to the raw login", "Pushed by: gandikediye-afk" not in text)
    check("never credited as commit author", "— gandikediye-afk" not in text)


def test_collaborators_are_named_from_their_commits_with_no_config():
    """Haider and Auroob are never configured — their names come off the commits."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    payload = _push(commits=[
        {"id": "aaa1111bbb22", "message": "KAN-24: add the retry loop",
         "author": {"name": "haider ali", "username": "haider-dev", "email": "haider@example.com"},
         "url": ""},
        {"id": "ccc3333ddd44", "message": "KAN-24: cover it with a test",
         "author": {"name": "Auroob Fatima", "username": "auroob-dev", "email": "auroob@example.com"},
         "url": ""},
    ])
    payload["sender"] = {"login": "haider-dev"}
    payload["pusher"] = {"name": "haider-dev", "email": "haider@example.com"}

    service.handle("push", "d-collab", payload)
    text = adf.to_plain_text(stub.calls[0][1])
    check("pusher gets his real name, not his login", "Pushed by: Haider Ali" in text)
    check("both authors credited", "Written by: Haider Ali, Auroob Fatima" in text)
    check("commit attributed to Haider", "— Haider Ali" in text)
    check("commit attributed to Auroob", "— Auroob Fatima" in text)
    check("no raw login leaks into attribution", "haider-dev" not in text)


def test_a_name_is_never_reformatted_beyond_lowercase():
    _reset_env()
    check("all-lowercase gets title cased", people.display_name(None, "haider ali") == "Haider Ali")
    check("existing capitals are left alone", people.display_name(None, "Auroob Fatima") == "Auroob Fatima")
    check("mixed caps survive", people.display_name(None, "Rory McDonald") == "Rory McDonald")
    check("particles survive", people.display_name(None, "Sanne de Vries") == "Sanne de Vries")
    check("whitespace tidied", people.display_name(None, "  haider   ali ") == "Haider Ali")


def test_the_sawera_correction_survives_every_form_the_payload_takes():
    """The one identity the payload gets wrong, however it words it."""
    _reset_env()
    check("by login", people.display_name("gandikediye-afk") == "Sawera")
    check("by display name", people.display_name(None, "Gandi Kediye") == "Sawera")
    check("by name on a commit", people.display_name("gandikediye-afk", "Gandi Kediye") == "Sawera")
    check(
        "by email on the pusher",
        people.pusher_name(None, {"name": "", "email": "gandimohamed@gmail.com"}, []) == "Sawera"
        or people.display_name(None, "gandimohamed@gmail.com") == "Sawera",
    )
    _env(GITHUB_JIRA_PEOPLE="someone=Someone")
    check("a later addition cannot drop it", people.display_name("gandikediye-afk") == "Sawera")
    _reset_env()


def test_unknown_identities_are_never_invented():
    _reset_env()
    check("payload name wins over login", people.display_name("someone-else", "Real Name") == "Real Name")
    check("no name means the login stands", people.display_name("someone-else") == "someone-else")
    check("nothing at all is not fabricated", people.display_name(None, None) == "someone")


# ── auto-created tickets ─────────────────────────────────────────────────────
def test_work_matching_nothing_opens_a_ticket():
    _reset_env()
    _fake_model({
        "issue_key": None, "confidence": 0.0, "reason": "No candidate covers this.",
        "proposed_summary": "Add retry handling to the Pinecone ingestion worker",
        "proposed_description": "The worker drops records when Pinecone rate-limits. Adds bounded retries.",
    })
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)
    result = service.handle("push", "d-create", _unlabelled_push())

    check("a ticket was opened", result["created"] == "KAN-901")
    check("the comment went to the new ticket", result["commented"] == ["KAN-901"])
    check("source recorded as auto_created", result["matched"][0]["source"] == "auto_created")

    made = stub.created[0]
    check("opened in the right project", made["project"] == "KAN")
    check("opened as a Task", made["type"] == "Task")
    check("labelled as machine-opened", "auto-created" in made["labels"] and "github-automation" in made["labels"])
    check("uses the proposed title", made["summary"].startswith("Add retry handling"))

    body = adf.to_plain_text(made["doc"])
    check("body says it was opened automatically", "Opened automatically" in body)
    check("body carries the evidence", "verify signatures and post the comment" in body)
    check("body names the branch", "webhook-work" in body)


def test_matching_and_proposing_are_separate_calls():
    """Locks in the split.

    Both decisions were once made in a single call, and it made the model worse
    at each — it proposed a duplicate of a ticket it had matched at 90%, while
    declining to propose anything for genuinely untracked work. If these ever
    collapse back into one call, this fails.
    """
    _reset_env()
    seen: list[str] = []

    def _record(system, user, model):
        seen.append("match" if "CANDIDATE JIRA ISSUES" in user else "propose")
        return {"issue_key": "KAN-24", "confidence": 0.9, "reason": "fits"}

    matcher._call_model = _record  # type: ignore[assignment]
    m, prop = matcher.assess(events.parse("push", _unlabelled_push()), _CANDIDATES)
    check("a match short-circuits the second call", seen == ["match"])
    check("the match came back", m is not None and prop is None)

    seen.clear()
    matcher._call_model = lambda s, u, mo: (   # type: ignore[assignment]
        {"issue_key": None} if "CANDIDATE JIRA ISSUES" in u
        else {"summary": "Add retry handling to the worker", "description": "Details."}
    )
    m2, prop2 = matcher.assess(events.parse("push", _unlabelled_push()), _CANDIDATES)
    check("no match triggers the proposal call", prop2 is not None and m2 is None)
    check("proposal used the second prompt", prop2.summary == "Add retry handling to the worker")


def test_routine_upkeep_does_not_open_a_ticket():
    """The guard against turning every dependency bump into board noise."""
    _reset_env()
    _fake_model({
        "issue_key": None, "confidence": 0.0, "reason": "Routine upkeep.",
        "proposed_summary": None, "proposed_description": None,
    })
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)
    result = service.handle("push", "d-chore", _unlabelled_push())
    check("skipped", result["action"] == "skipped")
    check("no ticket opened", stub.created == [])
    check("no comment posted", stub.calls == [])


def test_auto_create_can_be_switched_off():
    _reset_env()
    _env(GITHUB_JIRA_AUTO_CREATE="0")
    _fake_model({
        "issue_key": None, "confidence": 0.0, "reason": "No match.",
        "proposed_summary": "Something worth tracking", "proposed_description": "Details.",
    })
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)
    result = service.handle("push", "d-nocreate", _unlabelled_push())
    check("falls back to skipping", result["action"] == "skipped")
    check("no ticket opened", stub.created == [])
    _reset_env()


def test_a_branch_only_ever_opens_one_ticket():
    """Second push to the same branch must comment, not open a second ticket."""
    _reset_env()
    _fake_model({
        "issue_key": None, "confidence": 0.0, "reason": "No candidate covers this.",
        "proposed_summary": "Add retry handling to the ingestion worker",
        "proposed_description": "Details.",
    })
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)

    store: dict[tuple[str, str], str] = {}
    service.branch_links.get = lambda repo, branch: store.get((repo.lower(), branch))   # type: ignore[assignment]
    service.branch_links.put = lambda repo, branch, key, source="": store.__setitem__((repo.lower(), branch), key)  # type: ignore[assignment]

    first = service.handle("push", "d-b1", _unlabelled_push())
    second = service.handle("push", "d-b2", _unlabelled_push())

    check("first push opens a ticket", first["created"] == "KAN-901")
    check("second push opens nothing", second["created"] is None)
    check("second push comments on the same ticket", second["commented"] == ["KAN-901"])
    check("only one ticket exists", len(stub.created) == 1)
    check("the link, not the model, resolved it", second["matched"][0]["source"] == "branch_link")


# ── the branch is the unit of work ───────────────────────────────────────────
def _binding_store(stub):
    """Give the stub a real in-memory branch-binding table."""
    store: dict[tuple[str, str], str] = {}
    service.branch_links.get = lambda repo, branch: store.get((repo.lower(), branch))  # type: ignore[assignment]
    service.branch_links.put = lambda repo, branch, key, source="": store.__setitem__(  # type: ignore[assignment]
        (repo.lower(), branch), key
    )
    return store


def test_a_branch_binds_to_one_ticket_and_stays_there():
    """The whole point: after the first event, a branch resolves with no regex
    and no model call, so it cannot drift onto a different ticket."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    store = _binding_store(stub)

    first = service.handle("push", "d-u1", _push())          # branch names KAN-24
    check("first push binds the branch", store[("gandikediye-afk/hermes-agent-mvp", "feature/KAN-24-webhook")] == "KAN-24")
    check("first push resolved explicitly", first["matched"][0]["source"] == "branch")

    # A later push whose commits name a DIFFERENT ticket must not move it.
    calls: list[str] = []
    matcher._call_model = lambda s, u, m: calls.append("model") or None  # type: ignore[assignment]
    second = service.handle("push", "d-u2", _push(
        commits=[{"id": "ffff1111aaaa", "message": "KAN-30 unrelated wording",
                  "author": {"name": "haider"}, "url": ""}],
    ))
    check("second push stays on the bound ticket", second["commented"] == ["KAN-24"])
    check("resolved through the binding", second["matched"][0]["source"] == "branch_link")
    check("no model call was needed", calls == [])


def test_trunk_branches_are_never_bound():
    """Binding `main` would nail every future push to it onto one ticket."""
    _reset_env()
    check("main is not bindable", not config.is_bindable("main"))
    check("develop is not bindable", not config.is_bindable("develop"))
    check("a release namespace is not bindable", not config.is_bindable("release/2026.07"))
    check("a feature branch is bindable", config.is_bindable("feature/kan-24-webhook"))

    stub = _StubJira()
    _with_stub(stub)
    store = _binding_store(stub)
    service.handle("push", "d-trunk", _push(
        ref="refs/heads/main",
        commits=[{"id": "aaa1111bbb22", "message": "KAN-24: hotfix", "author": {"name": "haider"}, "url": ""}],
    ))
    check("main was not bound to anything", store == {})


def test_a_merge_to_main_resolves_through_the_source_branch_binding():
    """A push to main belongs to the ticket of the branch that landed — even
    when not one commit in it mentions a key."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    store = _binding_store(stub)
    store[("gandikediye-afk/hermes-agent-mvp", "feature/retry-logic")] = "KAN-902"

    result = service.handle("push", "d-mergelink", _push(
        ref="refs/heads/main",
        commits=[
            {"id": "aaa1111bbb22", "message": "add bounded retries", "author": {"name": "haider"}, "url": ""},
            {"id": "ccc3333ddd44", "message": "Merge pull request #7 from gandikediye-afk/feature/retry-logic",
             "author": {"name": "sawera"}, "url": ""},
        ],
    ))
    check("landed on the branch's ticket", result["commented"] == ["KAN-902"])
    check("credited to the merged branch's binding", result["matched"][0]["source"] == "merged_branch_link")
    check("main still not bound", ("gandikediye-afk/hermes-agent-mvp", "main") not in store)


def test_a_pr_resolves_through_its_head_branch_binding():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    store = _binding_store(stub)
    store[("gandikediye-afk/hermes-agent-mvp", "feature/kan-24-webhook")] = "KAN-902"

    result = service.handle("pull_request", "d-prlink", _pr(action="opened"))
    check("PR uses the head branch's ticket", result["commented"] == ["KAN-902"])
    check("and moves it to review", stub.transitions == [("KAN-902", "In Review")])


def test_the_full_branch_lifecycle():
    """push -> push -> PR -> merge, all on one ticket, bound once."""
    _reset_env()
    _fake_model({"issue_key": None, "confidence": 0.0, "reason": "none",
                 "summary": "Add bounded retries to the ingestion worker",
                 "description": "Retries Pinecone upserts on rate-limit."})
    stub = _StubJira(candidates=_CANDIDATES)
    _with_stub(stub)
    store = _binding_store(stub)

    def push(ref="refs/heads/feature/retry-logic"):
        return _push(ref=ref, commits=[{"id": "aaa1111bbb22", "message": "add bounded retries",
                                        "author": {"name": "haider"}, "url": ""}])

    a = service.handle("push", "d-l1", push())
    check("1. first push opens a ticket", a["created"] == "KAN-901")
    check("   and moves it to In Progress", stub.transitions[-1] == ("KAN-901", "In Progress"))

    b = service.handle("push", "d-l2", push())
    check("2. second push reuses it", b["commented"] == ["KAN-901"] and b["created"] is None)
    check("   only one ticket ever opened", len(stub.created) == 1)

    pr = _pr(action="opened")
    pr["pull_request"]["head"]["ref"] = "feature/retry-logic"
    pr["pull_request"]["title"] = "Add bounded retries"      # no key anywhere
    pr["pull_request"]["body"] = ""
    c = service.handle("pull_request", "d-l3", pr)
    check("3. PR finds the same ticket", c["commented"] == ["KAN-901"])
    check("   and moves it to In Review", stub.transitions[-1] == ("KAN-901", "In Review"))

    merged = _pr(action="closed", merged=True, state="closed")
    merged["pull_request"]["head"]["ref"] = "feature/retry-logic"
    merged["pull_request"]["title"] = "Add bounded retries"
    merged["pull_request"]["body"] = ""
    d = service.handle("pull_request", "d-l4", merged)
    check("4. merge finds the same ticket", d["commented"] == ["KAN-901"])
    check("   and marks it Done", stub.transitions[-1] == ("KAN-901", "Done"))
    check("   one binding, one ticket, four events", len(store) == 1)


# ── status transitions ───────────────────────────────────────────────────────
def test_a_commit_moves_the_ticket_to_in_progress():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-t1", _push())
    check("transitioned", stub.transitions == [("KAN-24", "In Progress")])
    check("reported in the response", result["transitions"][0]["to"] == "In Progress")


def test_opening_a_pr_moves_the_ticket_to_in_review():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    service.handle("pull_request", "d-t2", _pr(action="opened"))
    check("transitioned to review", stub.transitions == [("KAN-24", "In Review")])


def test_merging_the_branch_marks_the_ticket_done():
    """The branch is the unit of work, so its merge finishes the work."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    service.handle("pull_request", "d-t3", _pr(action="closed", merged=True, state="closed"))
    check("merge closes the ticket out", stub.transitions == [("KAN-24", "Done")])


def test_closing_a_pr_without_merging_does_not_mark_it_done():
    """Abandoned work is not finished work."""
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    service.handle("pull_request", "d-t3b", _pr(action="closed", merged=False, state="closed"))
    check("no transition on an unmerged close", stub.transitions == [])


def test_reviewing_does_not_change_status():
    _reset_env()
    stub2 = _StubJira()
    _with_stub(stub2)
    service.handle("pull_request_review", "d-t4", {
        "action": "submitted",
        "review": {"state": "approved", "body": "", "user": {"login": "sawera"}, "html_url": ""},
        "pull_request": {"number": 42, "title": "KAN-24 automation", "head": {"ref": "feature/kan-24"}},
        "repository": {"full_name": "gandikediye-afk/Hermes-Agent-MVP"},
    })
    check("a review leaves status alone", stub2.transitions == [])


def test_a_branch_deletion_does_not_move_the_ticket():
    _reset_env()
    stub = _StubJira()
    _with_stub(stub)
    service.handle("push", "d-t5", _push(deleted=True, commits=[]))
    check("no transition on a branch delete", stub.transitions == [])


def test_transitions_can_be_disabled():
    _reset_env()
    _env(GITHUB_JIRA_STATUS_ON_COMMIT="", GITHUB_JIRA_STATUS_ON_PR="")
    stub = _StubJira()
    _with_stub(stub)
    result = service.handle("push", "d-t6", _push())
    check("nothing transitioned", stub.transitions == [])
    check("the comment still posted", result["commented"] == ["KAN-24"])
    _reset_env()


def test_done_is_protected_by_default():
    _reset_env()
    check("Done is protected", "done" in config.protected_statuses())
    check("commit target is In Progress", config.transition_on_commit() == "In Progress")
    check("PR target is In Review", config.transition_on_pr() == "In Review")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} checks passed across {len(tests)} tests.")
