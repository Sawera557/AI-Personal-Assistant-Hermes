"""Offline functional test for hermes/notify.py (feat/daily-attention-report).

The branch ships no tests. This exercises the notification channel with the
Maton/Gmail transport replaced by a fake, so NO mail can leave. Scratch file —
not intended to be committed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import notify as n  # noqa: E402

FAILS: list[str] = []
SENT: list[dict] = []          # every send that WOULD have gone out


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  -- ' + detail}")
    if not cond:
        FAILS.append(label)


class FakeClient:
    """Stands in for MatonClient — records the send instead of performing it."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def request(self, app, method, path, body=None, query=None):
        SENT.append({"app": app, "method": method, "path": path,
                     "raw_len": len((body or {}).get("raw", ""))})
        return {"ok": self.ok, "status": 200 if self.ok else 500, "data": {}}


class FakeStorage:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def key_of(self, uri):
        return uri.split("/", 3)[-1]

    def get_bytes(self, uri):
        if uri not in self.blobs:
            raise RuntimeError("no such object")
        return self.blobs[uri], "application/pdf"

    def presign(self, uri, expires=0):
        return f"https://signed.example/{uri.rsplit('/',1)[-1]}"


def arm(ok=True, blobs=None):
    SENT.clear()
    n._notify_client = lambda: FakeClient(ok)          # ← no real Gmail, ever
    n.storage = FakeStorage(blobs or {})


# ── 1. pure helpers ──────────────────────────────────────────────────────────
print("\n[1] helpers")
check("recipients split on commas",
      n._parse_recipients("a@x.com, b@y.com ,, c@z.com") == ["a@x.com", "b@y.com", "c@z.com"],
      str(n._parse_recipients("a@x.com, b@y.com ,, c@z.com")))
check("empty recipients -> []", n._parse_recipients(None) == [] and n._parse_recipients("  ") == [])
check("headline = first non-empty line",
      n._headline("\n\n  Three approvals today  \nmore text") == "Three approvals today",
      repr(n._headline("\n\n  Three approvals today  \nmore text")))
check("headline fallback when reply empty", n._headline("") == "Task completed.")

n.Config.PUBLIC_BASE_URL = "https://hermes.example"
check("deep link built", n._task_deep_link("t1") == "https://hermes.example/#task/t1",
      str(n._task_deep_link("t1")))
n.Config.PUBLIC_BASE_URL = ""
check("no link without PUBLIC_BASE_URL", n._task_deep_link("t1") is None)
check("no link without task_id", n._task_deep_link(None) is None)


# ── 2. send_system_email ─────────────────────────────────────────────────────
print("\n[2] send_system_email")
arm()
ok = n.send_system_email(["owner@x.com"], "Daily report", "body text")
check("returns True on 2xx", ok is True)
check("one send attempted", len(SENT) == 1, str(len(SENT)))
check("uses google-mail send endpoint",
      SENT and SENT[0]["app"] == "google-mail" and SENT[0]["path"].endswith("/messages/send"),
      str(SENT[:1]))

arm()
check("no recipients -> no send", n.send_system_email([], "s", "b") is False and not SENT)
check("None recipients -> no send", n.send_system_email(None, "s", "b") is False and not SENT)

arm()
n._notify_client = lambda: None
check("no maton key -> no send, returns False",
      n.send_system_email(["o@x.com"], "s", "b") is False and not SENT)

arm(ok=False)
check("non-2xx -> returns False (never raises)", n.send_system_email(["o@x.com"], "s", "b") is False)


# ── 3. subject prefixing ─────────────────────────────────────────────────────
print("\n[3] subject prefix")
import base64


def last_subject() -> str:
    return ""   # raw is base64url MIME; assert via _build_mime_raw directly below


arm()
raw = n._build_mime_raw(to=["o@x.com"], subject="[Hermes] Already", body="b")
decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
check("prefixed subject not doubled", decoded.count("[Hermes]") == 1, str(decoded.count("[Hermes]")))


# ── 4. deliver_run_result ────────────────────────────────────────────────────
print("\n[4] deliver_run_result")
arm()
n.deliver_run_result({"name": "T", "task_id": "t1"}, "r1", {"reply": "done"})
check("no notify_to -> silent no-op", not SENT, str(SENT))

arm()
n.deliver_run_result({"name": "Weekly", "task_id": "t1", "notify_to": "o@x.com"},
                     "r1", {"reply": "3 approvals today\nitem one\nitem two"})
check("sends when notify_to set", len(SENT) == 1, str(len(SENT)))

arm()
long_reply = "HEADLINE\n" + ("x" * 9000)
n.deliver_run_result({"name": "T", "task_id": "t1", "notify_to": "o@x.com"},
                     "r1", {"reply": long_reply})
check("long body still sends (truncated)", len(SENT) == 1, str(len(SENT)))
check("truncated below raw of full reply", SENT and SENT[0]["raw_len"] < 9000, str(SENT[:1]))

print("\n[4b] attachments")
arm(blobs={"s3://b/report.pdf": b"P" * 500})
n.deliver_run_result(
    {"name": "T", "task_id": "t1", "notify_to": "o@x.com"}, "r1",
    {"reply": "done", "tool_calls": [{"file": {"s3_uri": "s3://b/report.pdf",
                                               "filename": "report.pdf"}}]})
check("run file attached, one send", len(SENT) == 1, str(len(SENT)))

arm(blobs={"s3://b/huge.pdf": b"P" * (n.MAX_ATTACH_BYTES + 10)})
n.deliver_run_result(
    {"name": "T", "task_id": "t1", "notify_to": "o@x.com"}, "r1",
    {"reply": "done", "tool_calls": [{"file": {"s3_uri": "s3://b/huge.pdf",
                                               "filename": "huge.pdf"}}]})
check("oversize file -> still sends (linked not attached)", len(SENT) == 1, str(len(SENT)))

arm(blobs={})   # object missing from S3
n.deliver_run_result(
    {"name": "T", "task_id": "t1", "notify_to": "o@x.com"}, "r1",
    {"reply": "done", "tool_calls": [{"file": {"s3_uri": "s3://b/gone.pdf",
                                               "filename": "gone.pdf"}}]})
check("unreadable file skipped, notification still sent", len(SENT) == 1, str(len(SENT)))

arm()
n._notify_client = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    n.deliver_run_result({"name": "T", "task_id": "t1", "notify_to": "o@x.com"},
                         "r1", {"reply": "done"})
    check("transport blowing up never raises to caller", True)
except Exception as e:
    check("transport blowing up never raises to caller", False, repr(e))


# ── 5. ensure_digest_task ────────────────────────────────────────────────────
print("\n[5] ensure_digest_task (idempotency)")
import types

fake_repo = types.SimpleNamespace(
    list_tasks=lambda _x: [{"name": "Daily attention report", "task_id": "existing-1"}],
    create_task=lambda **kw: "SHOULD-NOT-CREATE",
)
sys.modules["hermes.db.scheduled_tasks"] = fake_repo
check("existing task returned, not recreated", n.ensure_digest_task() == "existing-1",
      str(n.ensure_digest_task()))

created = {}
fake_repo.list_tasks = lambda _x: []
def _create(**kw):
    created.update(kw)
    return "new-1"
fake_repo.create_task = _create
tid = n.ensure_digest_task()
check("creates when absent", tid == "new-1", str(tid))
check("created readonly", created.get("autonomy") == "readonly", str(created.get("autonomy")))
check("created enabled daily 07:00",
      created.get("schedule", {}).get("times") == ["07:00"], str(created.get("schedule")))
check("scoped to all businesses", created.get("entity_slug") is None, str(created.get("entity_slug")))

fake_repo.list_tasks = lambda _x: (_ for _ in ()).throw(RuntimeError("db down"))
check("DB failure -> None, no raise", n.ensure_digest_task() is None)

print("\n" + "=" * 60)
print(f"sends attempted against the FAKE transport: {len(SENT)} (real Gmail never touched)")
print(f"RESULT: {len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
