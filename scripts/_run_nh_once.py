"""One-off TEST: fire a single manual run of the NH WhatsApp backfill task.

Manual trigger => processes the current cursor day (Jun 17) WITHOUT advancing the
window cursor or moving the schedule (task stays disabled/dormant afterward). It
runs the REAL agent read-only against WhatsApp and saves one batch report. Safe to
delete after. Prints the run summary + reply + which tools were called.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from hermes.db import scheduled_tasks as db
from hermes import scheduler as sch

TASK_ID = "9d797bbe-5104-400f-93ea-7355acbd3c9a"
task = db.get_task(TASK_ID)
print("=== RUNNING (manual, single day) ===", flush=True)
print("task:", task["name"], "| cursor day:", task["window_cursor"], flush=True)
print("Building single-day prompt preview...", flush=True)
task["_window_day"] = task["window_cursor"]
preview = sch.build_run_prompt(task)
print("--- prompt injected (first 600 chars) ---", flush=True)
print(preview[:600], flush=True)
print("--- firing agent (this can take a few minutes) ---", flush=True)

summary = sch.execute_and_record(task, trigger="manual")
print("\n=== RESULT ===", flush=True)
print("status:", summary.get("status"), flush=True)
if summary.get("error"):
    print("error:", summary["error"], flush=True)
reply = (summary.get("reply") or "")
print("reply length:", len(reply), "chars", flush=True)
print("--- reply (first 1500 chars) ---", flush=True)
print(reply[:1500].encode("ascii", "replace").decode(), flush=True)

# Show which tools the run used
runs = db.list_runs(TASK_ID, 1)
if runs:
    tools = [ (tc.get("name") or tc.get("tool") or "?") for tc in (runs[0].get("tool_calls") or []) ]
    print("\ntools called:", tools, flush=True)
    print("run_id:", runs[0]["run_id"], "| trigger:", runs[0]["trigger"], "| status:", runs[0]["status"], flush=True)

# Confirm the cursor did NOT advance (manual must not consume a day)
after = db.get_task(TASK_ID)
print("\ncursor after manual run:", after["window_cursor"], "(should still be 2026-06-17)", flush=True)
print("enabled after:", after["enabled"], "(should still be False)", flush=True)
