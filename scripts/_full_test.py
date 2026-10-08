"""Full backend verification for the scheduled-task feature. Deterministic, runs
against the real DB. Creates tasks tagged [FULLTEST] and deletes them in finally.
"""
import os, sys, json, urllib.request
from datetime import datetime, date, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from hermes.db import scheduled_tasks as db
from hermes import scheduler as sch
from hermes import tools as T
import app as APP  # for _enrich_run + _validate_window

P = {"pass": 0, "fail": 0}
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label, flush=True)
    P["pass" if cond else "fail"] += 1

created = []
try:
    # ---- B1. Chat-agent tool: create_scheduled_task (recurring) --------------
    print("\n[B1] create_scheduled_task chat tool (recurring)")
    r = T._create_scheduled_task({
        "name": "[FULLTEST] chat recurring", "prompt": "Say hi.",
        "frequency": "weekdays", "times": ["07:30"], "business": "nh",
        "autonomy": "readonly",
    }, active_business="nh")
    check("tool returns ok", r.get("ok") is True)
    check("has task_id", bool(r.get("task_id")))
    check("schedule described", "Weekdays" in (r.get("schedule") or ""))
    if r.get("task_id"):
        created.append(r["task_id"])
        t = db.get_task(r["task_id"])
        check("stored, business=nh", t and t["business"] == "nh")
        check("no window on chat-made task", t and not t.get("window_pace"))

    # ---- B2. Chat tool validation: weekly without days -----------------------
    print("\n[B2] chat tool validation")
    bad = T._create_scheduled_task({"name": "x", "prompt": "y", "frequency": "weekly"}, "nh")
    check("weekly w/o days rejected", bad.get("ok") is False)

    # ---- B3. Windowed backfill task via db + claim drain ---------------------
    print("\n[B3] windowed backfill: create + claim drain")
    now = datetime.now(timezone.utc)
    tid = db.create_task(
        entity_slug="nour_hammam", name="[FULLTEST] window", prompt="x",
        schedule={"frequency": "daily", "times": ["06:00"], "days_of_week": []},
        timezone_name="America/Chicago", allowed_apps=["wati"], autonomy="readonly",
        enabled=True, next_run_at=now - timedelta(minutes=1),
        window_start="2026-07-15", window_end="2026-07-17", window_step=1, window_pace="catchup")
    created.append(tid)
    t = db.get_task(tid)
    check("window stored, cursor=start", t["window_cursor"] == "2026-07-15")
    check("window_summary rendered", "Backfill" in (sch.describe_window(t) or ""))
    days, cur = [], now
    for _ in range(5):
        claimed = db.claim_due_window(tid, now + timedelta(days=1), now=cur)
        if not claimed:
            break
        days.append(str(claimed))
        tt = db.get_task(tid)
        if not tt["next_run_at"]:
            break
        cur = datetime.fromisoformat(tt["next_run_at"]) + timedelta(seconds=1)
    check("drained exactly 3 days inclusive", days == ["2026-07-15", "2026-07-16", "2026-07-17"])
    check("stops after end (next claim None)", db.claim_due_window(tid, now, now=cur + timedelta(minutes=5)) is None)

    # ---- B4. API validation of window (app._validate_window) -----------------
    print("\n[B4] window validation")
    check("end<start rejected", APP._validate_window({"pace": "catchup", "start": "2026-07-17", "end": "2026-07-01"}) is not None)
    check("catchup w/o end rejected", APP._validate_window({"pace": "catchup", "start": "2026-07-01", "end": None}) is not None)
    check("bad pace rejected", APP._validate_window({"pace": "nope", "start": "2026-07-01", "end": "2026-07-02"}) is not None)
    check("valid window accepted", APP._validate_window({"pace": "catchup", "start": "2026-07-01", "end": "2026-07-02", "step_days": 1}) is None)

    # ---- B5. Run a file-creating task -> files captured + enriched + link -----
    print("\n[B5] run -> create_file captured -> enriched download link")
    ftid = db.create_task(
        entity_slug="nour_hammam", name="[FULLTEST] file", enabled=False, autonomy="readonly",
        prompt="Use create_file to save a Markdown file named fulltest.md whose content is exactly: # Full Test\\nIt works. Then reply done.",
        schedule={"frequency": "daily", "times": ["06:00"], "days_of_week": []},
        timezone_name="America/Chicago")
    created.append(ftid)
    task = db.get_task(ftid)
    summary = sch.execute_and_record(task, trigger="manual")
    check("run success", summary.get("status") == "success")
    runs = db.list_runs(ftid, 1)
    tc = runs[0]["tool_calls"] if runs else []
    has_file_meta = any(c.get("file", {}).get("s3_uri") for c in tc)
    check("run trace captured file s3_uri", has_file_meta)
    enriched = APP._enrich_run(dict(runs[0]))
    files = enriched.get("files") or []
    check("enriched run exposes files", len(files) == 1)
    url = files[0].get("download_url") if files else None
    check("fresh download_url present", bool(url))
    if url:
        try:
            body = urllib.request.urlopen(url, timeout=15).read().decode("utf-8", "replace")
        except Exception as e:
            body = f"ERR {e}"
        check("download serves the .md content", "It works" in body)
        check("filename is .md", (files[0].get("filename") or "").endswith(".md"))

    # ---- B6. update (pause) + list + delete tools ----------------------------
    print("\n[B6] update/list/delete chat tools")
    up = T._update_scheduled_task({"task_id": created[0], "enabled": False}, "nh")
    check("pause via update tool ok", up.get("ok") is True)
    check("paused -> next_run cleared", db.get_task(created[0])["next_run_at"] is None)
    lst = T._list_scheduled_tasks({"business": "nh"}, "nh")
    names = json.dumps(lst)
    check("list tool returns our task", "[FULLTEST] chat recurring" in names)
    dl = T._delete_scheduled_task({"task_id": created[0]}, "nh")
    check("delete tool ok", dl.get("ok") is True)
    check("deleted -> gone", db.get_task(created[0]) is None)
    created.remove(created[0])

finally:
    print("\n[cleanup] deleting test tasks")
    for tid in created:
        db.delete_task(tid)
    print(f"  removed {len(created)} task(s)")

print(f"\n==== RESULT: {P['pass']} passed, {P['fail']} failed ====")
sys.exit(1 if P["fail"] else 0)
