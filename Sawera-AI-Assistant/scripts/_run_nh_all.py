"""One-off: run the FULL NH WhatsApp backfill (Jun 17 -> Jul 17), all 31 days.

Drives each day directly (manual-style) so it's fully isolated from the running
old-code :5050 poller: the task stays DISABLED (next_run_at NULL) the whole time,
so no poller can race it, and we set the per-run day ourselves. Each day is
recorded as a run in history AND consolidated into a results file. Read-only.
Safe to delete after.
"""
import os, sys, re
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from hermes.db import scheduled_tasks as db
from hermes import scheduler as sch

TASK_ID = "9d797bbe-5104-400f-93ea-7355acbd3c9a"
START, END = date(2026, 6, 17), date(2026, 7, 17)
OUT = os.path.join(os.path.dirname(__file__), "nh_backfill_results.md")

task = db.get_task(TASK_ID)
lines = ["# Nour Hammam WhatsApp backfill — full results (Jun 17 → Jul 17)\n"]

def first_para(txt):
    for ln in (txt or "").splitlines():
        s = ln.strip()
        if s:
            return s
    return "(empty reply)"

day = START
n = 0
while day <= END:
    n += 1
    iso = day.isoformat()
    task["_window_day"] = iso
    print(f"[{n}/31] {iso} ... running", flush=True)
    run_id = db.start_run(TASK_ID, trigger="manual")
    try:
        res = sch.run_task(task)
        status = "error" if res.get("stop_reason") == "refusal" else "success"
        reply = res.get("reply") or ""
        db.finish_run(run_id, TASK_ID, status=status, reply=reply,
                      tool_calls=res.get("tool_calls") or [], set_next=False)
    except Exception as exc:  # noqa: BLE001
        status, reply = "error", f"EXCEPTION: {exc}"
        db.finish_run(run_id, TASK_ID, status="error", error=str(exc), set_next=False)
    link = ""
    m = re.search(r"https?://\S+?\.txt\S*", reply)
    if m:
        link = m.group(0)
    head = first_para(reply).encode("ascii", "replace").decode()
    print(f"[{n}/31] {iso} -> {status}: {head[:140]}", flush=True)
    lines.append(f"## {iso} — {status}\n\n{head}\n" + (f"\n[report file]({link})\n" if link else ""))
    day += timedelta(days=1)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\nDONE. Consolidated results written to {OUT}", flush=True)
print(f"Task left disabled (dormant); cursor untouched at {db.get_task(TASK_ID)['window_cursor']}", flush=True)
