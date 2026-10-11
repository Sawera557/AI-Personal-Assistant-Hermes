"""Offline functional test for hermes/guardrails.py + the Pinecone isolation
guard (feat/guardrails). The branch ships no tests.

This is a SAFETY feature, so the tests are mostly about what it REFUSES.
Everything is in-process; no DB, no network, no tool ever executes.
Scratch file — not intended to be committed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"     # forces the in-process override store
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import guardrails as g          # noqa: E402
from hermes import pinecone_store as ps     # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  -- ' + detail}")
    if not cond:
        FAILS.append(label)


def reset() -> None:
    g._overrides.clear()
    g._pending.clear()
    g.set_confirm_writes(True, None)


# ── 1. classification ────────────────────────────────────────────────────────
print("\n[1] action classification")
reset()
check("send_email -> send", g.classify("send_email", {}) == g.SEND)
check("jira_create_issue -> write", g.classify("jira_create_issue", {}) == g.WRITE)
check("delete_scheduled_task -> destructive", g.classify("delete_scheduled_task", {}) == g.DESTRUCTIVE)
check("create_file -> internal", g.classify("create_file", {}) == g.INTERNAL)
check("read-only tool -> None", g.classify("search_business_knowledge", {}) is None)
check("maton_request GET -> None", g.classify("maton_request", {"method": "GET"}) is None)
check("maton_request POST -> write", g.classify("maton_request", {"method": "POST"}) == g.WRITE)
check("maton_request patch (lowercase) -> write", g.classify("maton_request", {"method": "patch"}) == g.WRITE)
check("maton_request DELETE -> destructive", g.classify("maton_request", {"method": "DELETE"}) == g.DESTRUCTIVE)
check("maton_request no method defaults GET -> None", g.classify("maton_request", {}) is None)

print("\n[1b] what is actually GUARDED")
check("send_email guarded", g._is_guarded("send_email", {}))
check("jira_create_issue guarded", g._is_guarded("jira_create_issue", {}))
check("email_with_attachment NOT double-guarded (self-gated)",
      not g._is_guarded("email_with_attachment", {}))
check("create_file (internal) not guarded", not g._is_guarded("create_file", {}))
check("delete_scheduled_task (destructive) not in locked scope",
      not g._is_guarded("delete_scheduled_task", {}))
check("read-only not guarded", not g._is_guarded("search_business_knowledge", {}))


# ── 2. the gate: block then allow ────────────────────────────────────────────
print("\n[2] gate — first call blocks, approved call runs")
reset()
S = "sess-1"
args = {"to": "vendor@example.com", "subject": "Invoice", "body": "hi"}
d1 = g.check(S, "send_email", dict(args), "afk")
check("first call BLOCKED", d1.allow is False)
check("carries a token", bool(d1.approval_token), str(d1))
check("summary names recipient + subject",
      "vendor@example.com" in d1.summary and "Invoice" in d1.summary, d1.summary)

d2 = g.check(S, "send_email", {**args, "approval_token": d1.approval_token}, "afk")
check("re-call with token ALLOWED", d2.allow is True)

print("\n[2b] token is single-use (replay blocked)")
d3 = g.check(S, "send_email", {**args, "approval_token": d1.approval_token}, "afk")
check("replaying a consumed token is BLOCKED", d3.allow is False)


# ── 3. THE core security property: token binds to the args ──────────────────
print("\n[3] tampering — approved args cannot be swapped")
reset()
d1 = g.check(S, "send_email", dict(args), "afk")
tampered = {**args, "to": "attacker@evil.com", "approval_token": d1.approval_token}
d2 = g.check(S, "send_email", tampered, "afk")
check("changed recipient + old token -> BLOCKED", d2.allow is False)
check("re-block issues a DIFFERENT token", d2.approval_token != d1.approval_token,
      f"{d1.approval_token} vs {d2.approval_token}")
check("new summary shows the tampered recipient", "attacker@evil.com" in d2.summary, d2.summary)

reset()
d1 = g.check(S, "send_email", dict(args), "afk")
body_changed = {**args, "body": "wire $50,000", "approval_token": d1.approval_token}
check("changed BODY + old token -> BLOCKED",
      g.check(S, "send_email", body_changed, "afk").allow is False)

print("\n[3b] token scoping")
reset()
t_afk = g.token_for("send_email", args, "afk")
t_gak = g.token_for("send_email", args, "gak")
check("same args, different business -> different token", t_afk != t_gak)
check("same args, same business -> stable token",
      g.token_for("send_email", args, "afk") == t_afk)
check("approval_token itself excluded from the hash",
      g.token_for("send_email", {**args, "approval_token": "xyz"}, "afk") == t_afk)

print("\n[3c] cross-session replay")
reset()
d1 = g.check("sess-A", "send_email", dict(args), "afk")
d2 = g.check("sess-B", "send_email", {**args, "approval_token": d1.approval_token}, "afk")
check("token issued in session A is NOT valid in session B", d2.allow is False)

print("\n[3d] clear_session drops pending approvals")
reset()
d1 = g.check(S, "send_email", dict(args), "afk")
g.clear_session(S)
check("after clear_session the token no longer works",
      g.check(S, "send_email", {**args, "approval_token": d1.approval_token}, "afk").allow is False)


# ── 4. bypasses that are intentional ─────────────────────────────────────────
print("\n[4] intentional pass-throughs")
reset()
check("unattended run (interactive=False) is NOT gated",
      g.check(S, "send_email", dict(args), "afk", interactive=False).allow is True)

reset()
g.set_confirm_writes(False, None)
check("guardrail OFF globally -> allowed",
      g.check(S, "send_email", dict(args), "afk").allow is True)

reset()
g.set_confirm_writes(False, None)
g.set_confirm_writes(True, "afk")
check("per-business ON overrides global OFF",
      g.check(S, "send_email", dict(args), "afk").allow is False)
check("other business still follows global OFF",
      g.check(S, "send_email", dict(args), "gak").allow is True)

reset()
g.set_confirm_writes(True, None)
g.set_confirm_writes(False, "gak")
check("per-business OFF overrides global ON",
      g.check(S, "send_email", dict(args), "gak").allow is True)
check("'all' scope normalizes to global",
      g.check(S, "send_email", dict(args), "all").allow is False)


# ── 5. fail-safe ─────────────────────────────────────────────────────────────
print("\n[5] fail-safe — config errors must not weaken the gate")
reset()
orig = g._db_backed
g._db_backed = lambda: (_ for _ in ()).throw(RuntimeError("settings backend down"))
try:
    check("config read explodes -> still GUARDED",
          g.check(S, "send_email", dict(args), "afk").allow is False)
finally:
    g._db_backed = orig


# ── 6. guardrail registry (Settings UI contract) ─────────────────────────────
print("\n[6] registry")
reset()
keys = {m["key"] for m in g.GUARDRAILS_META}
check("both guardrails registered", keys == {"confirm_writes", "business_isolation"}, str(keys))
check("list_guardrails reports enabled state",
      all("enabled" in r for r in g.list_guardrails()))
try:
    g.set_guardrail_enabled("nope", True)
    check("unknown guardrail key raises", False, "no raise")
except KeyError:
    check("unknown guardrail key raises", True)


# ── 7. guardrail #2 — business data isolation ────────────────────────────────
print("\n[7] Pinecone business isolation")
check("no expected business -> keep (nothing to verify)",
      ps._business_ok({"entity_id": "x"}, None, None) is True)
check("untagged vector -> keep (namespace is the guard)",
      ps._business_ok({"text": "hello"}, "uuid-1", "afro_farm") is True)
check("matching entity_id -> keep",
      ps._business_ok({"entity_id": "UUID-1"}, "uuid-1", "afro_farm") is True)
check("matching slug -> keep",
      ps._business_ok({"entity_slug": "afro_farm"}, "uuid-1", "afro_farm") is True)
check("MISMATCHED business -> DROP",
      ps._business_ok({"entity_id": "uuid-999"}, "uuid-1", "afro_farm") is False)
check("mismatched slug -> DROP",
      ps._business_ok({"entity_slug": "nour_hammam"}, "uuid-1", "afro_farm") is False)
check("alternate key 'tenant' honoured",
      ps._business_ok({"tenant": "nour_hammam"}, "uuid-1", "afro_farm") is False)
check("one matching tag among several -> keep",
      ps._business_ok({"entity_id": "uuid-1", "business": "afro_farm"}, "uuid-1", "afro_farm") is True)

print("\n[7b] empty namespace must never fan out")
_orig_enabled = ps.Config.rag_pinecone_enabled
ps.Config.rag_pinecone_enabled = staticmethod(lambda: True)
try:
    check("empty entity_slug -> query returns [] (no catch-all namespace)",
          ps.query(entity_slug="", query_text="anything") == [])
    check("whitespace entity_slug -> query returns []",
          ps.query(entity_slug="   ", query_text="anything") == [])
finally:
    ps.Config.rag_pinecone_enabled = _orig_enabled


print("\n" + "=" * 62)
print(f"RESULT: {len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
