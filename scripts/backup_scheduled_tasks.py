"""Export every scheduled task and its full run history to a JSON file.

    python scripts/backup_scheduled_tasks.py [--out path.json]

`scheduled_task_runs.task_id` is `REFERENCES scheduled_tasks (task_id) ON DELETE
CASCADE`, so deleting a task silently destroys every run it ever had — including
`reply`, which holds the generated report text, and `tool_calls`, which holds the
trace. There is no undo. Run this before anything that deletes a task;
curate_scheduled_tasks.py --apply now calls it automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from hermes.db.connection import cursor  # noqa: E402


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def export(out_path: str | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = out_path or f"scheduled_tasks_backup_{stamp}.json"

    with cursor(commit=False) as cur:
        cur.execute("SELECT * FROM scheduled_tasks ORDER BY created_at")
        cols = [d[0] for d in cur.description]
        tasks = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute("SELECT * FROM scheduled_task_runs ORDER BY started_at")
        cols = [d[0] for d in cur.description]
        runs = [dict(zip(cols, r)) for r in cur.fetchall()]

    by_task: dict[str, list] = {}
    for r in runs:
        by_task.setdefault(str(r["task_id"]), []).append(r)
    for t in tasks:
        t["runs"] = by_task.get(str(t["task_id"]), [])

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "task_count": len(tasks),
        "run_count": len(runs),
        "tasks": tasks,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, default=_jsonable, ensure_ascii=False)

    reports = sum(1 for r in runs if (r.get("reply") or "").strip())
    print(f"Backed up {len(tasks)} tasks and {len(runs)} runs "
          f"({reports} with report text) -> {out_path}")
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="output path (default: timestamped in cwd)")
    export(ap.parse_args().out)
