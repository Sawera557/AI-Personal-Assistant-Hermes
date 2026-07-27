"""Load playbooks from HERMES_PLAYBOOKS.md into the playbooks table (Plan tasks
D1/D3). Direct-to-DB.

Usage (from the repo root, with .env configured for Postgres):
    python load_playbooks.py [path/to/HERMES_PLAYBOOKS.md]

Parses every
    <!-- PLAYBOOK: slug | Name | one-line description -->
    ...body...
    <!-- /PLAYBOOK: slug -->
block and upserts it (idempotent).

To make a scheduled task follow one, link it by task_id:
    python -c "from hermes.db import playbooks as p; print(p.assign_playbook('<task_id>', 'friday-brief'))"
Find task_ids with:
    python -c "from hermes.db import scheduled_tasks as t; [print(x['task_id'], x['name']) for x in t.list_tasks()]"
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes.config import Config  # noqa: E402  (loads .env)
from hermes.db import playbooks as repo  # noqa: E402

_PB_RE = re.compile(
    r"<!-- PLAYBOOK: ([\w-]+) \| (.*?) \| (.*?) -->\n(.*?)<!-- /PLAYBOOK: \1 -->", re.S
)


def main() -> int:
    _ = Config
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("HERMES_PLAYBOOKS.md")
    if not path.exists():
        print(f"File not found: {path}")
        return 1
    text = path.read_text(encoding="utf-8")
    pbs = _PB_RE.findall(text)
    if not pbs:
        print("No <!-- PLAYBOOK: slug | name | desc --> blocks found.")
        return 1
    ok = 0
    for slug, name, desc, body in pbs:
        res = repo.upsert_playbook(
            slug=slug, name=name.strip(), description=desc.strip(),
            content_md=body.strip(),
        )
        if res:
            print(f"  [x] {slug:22s} ({len(body.strip())} chars)")
            ok += 1
        else:
            print(f"  [ ] {slug:22s} FAILED (check DB config / logs)")
    print(f"Loaded {ok}/{len(pbs)} playbooks.")
    return 0 if ok == len(pbs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
