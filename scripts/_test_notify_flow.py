"""End-to-end flow check for the daily attention report.

Takes a REAL completed run of the "Daily attention report" task out of the
production DB (read-only) and drives it through the real
scheduler -> notify.deliver_run_result path, with ONLY the Gmail transport
replaced by a fake. Nothing is sent; the MIME that *would* go out is decoded
and shown. Scratch file — not intended to be committed.
"""
from __future__ import annotations

import base64
import email
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import notify as n              # noqa: E402
from hermes.db.connection import cursor     # noqa: E402

CAPTURED: list[dict] = []


class FakeClient:
    """Records the send instead of performing it."""

    def request(self, app, method, path, body=None, query=None):
        CAPTURED.append({"app": app, "path": path, "raw": (body or {}).get("raw", "")})
        return {"ok": True, "status": 200, "data": {}}


n._notify_client = lambda: FakeClient()      # ← the ONLY thing faked

# ── pull the real task + its most recent successful run ─────────────────────
with cursor(commit=False) as c:
    c.execute("""select task_id, name, notify_to, prompt
                 from scheduled_tasks where lower(name)='daily attention report'""")
    task_id, name, notify_to, prompt = c.fetchone()
    c.execute("""select run_id, reply, tool_calls, status, started_at
                 from scheduled_task_runs
                 where task_id=%s and status='success'
                 order by started_at desc limit 1""", (task_id,))
    run_id, reply, tool_calls, status, started = c.fetchone()

task = {"task_id": str(task_id), "name": name, "notify_to": notify_to}
result = {"reply": reply, "tool_calls": tool_calls or []}

print("=" * 70)
print("INPUT — real production data")
print("=" * 70)
print(f"  task        : {name}")
print(f"  notify_to   : {notify_to}")
print(f"  run         : {str(run_id)[:8]}  status={status}  at {started:%Y-%m-%d %H:%M}")
print(f"  reply length: {len(reply or '')} chars")
print(f"  tool_calls  : {len(tool_calls or [])}")

# ── drive the real delivery path ────────────────────────────────────────────
n.deliver_run_result(task, str(run_id), result)

print()
print("=" * 70)
print("OUTPUT — the email that WOULD have been sent")
print("=" * 70)
if not CAPTURED:
    print("  NO SEND ATTEMPTED")
    sys.exit(1)

sent = CAPTURED[0]
raw = sent["raw"]
mime = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
msg = email.message_from_string(mime)

body = msg.get_payload()
if msg.is_multipart():
    parts = msg.get_payload()
    body = parts[0].get_payload(decode=True).decode("utf-8", "replace")

print(f"  endpoint    : {sent['app']} {sent['path']}")
print(f"  To          : {msg.get('To')}")
print(f"  Subject     : {msg.get('Subject')}")
print(f"  multipart   : {msg.is_multipart()}")
print(f"  body chars  : {len(body)}")
print()
print("  --- body: first 12 lines ---")
for line in body.splitlines()[:12]:
    print("   |", line[:100])
print("  --- body: last 6 lines ---")
for line in body.splitlines()[-6:]:
    print("   |", line[:100])

# ── assertions on the flow ──────────────────────────────────────────────────
print()
print("=" * 70)
print("FLOW CHECKS")
print("=" * 70)
fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  -- ' + detail}")
    if not cond:
        fails.append(label)


headline = n._headline(reply)
check("recipient matches task notify_to", msg.get("To") == notify_to, str(msg.get("To")))
check("subject prefixed [Hermes]", (msg.get("Subject") or "").startswith("[Hermes]"))
check("subject carries task name", name in (msg.get("Subject") or ""))
check("subject carries the headline", headline[:40] in (msg.get("Subject") or ""),
      f"headline={headline[:40]!r}")
check("body truncated at the 4000-char preview cap",
      len(reply) > n._BODY_PREVIEW_CHARS and "truncated" in body,
      f"reply={len(reply)} cap={n._BODY_PREVIEW_CHARS}")
check("body carries the signature footer", f'scheduled task "{name}"' in body)
check("exactly one send", len(CAPTURED) == 1, str(len(CAPTURED)))

print()
print(f"RESULT: {len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
print("(no mail left the process — transport was faked)")
sys.exit(1 if fails else 0)
