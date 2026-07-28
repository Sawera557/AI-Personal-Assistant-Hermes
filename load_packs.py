"""Load context packs from HERMES_CONTEXT_PACKS.md into the context_packs table
(Plan tasks B1/B3). Direct-to-DB — no HTTP endpoint required.

Usage (from the repo root, with .env configured for Postgres):
    python load_packs.py [path/to/HERMES_CONTEXT_PACKS.md]

Parses every  <!-- PACK: slug --> ... <!-- /PACK: slug -->  block and upserts it.
Idempotent: re-running bumps each pack's version. A running server picks the new
packs up on the next turn after the context-pack cache TTL (~5 min) or a restart.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes.config import Config  # noqa: E402  (loads .env)
from hermes.db import context_packs as repo  # noqa: E402

_PACK_RE = re.compile(r"<!-- PACK: (\w+) -->\n(.*?)<!-- /PACK: \1 -->", re.S)


def main() -> int:
    _ = Config  # ensure .env is loaded before we touch the DB
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("HERMES_CONTEXT_PACKS.md")
    if not path.exists():
        print(f"File not found: {path}")
        return 1
    text = path.read_text(encoding="utf-8")
    packs = _PACK_RE.findall(text)
    if not packs:
        print("No <!-- PACK: slug --> blocks found.")
        return 1
    ok = 0
    for slug, body in packs:
        res = repo.upsert_pack(slug, body.strip())
        if res:
            print(f"  [x] {slug:8s} v{res['version']}  ({len(body.strip())} chars)")
            ok += 1
        else:
            print(f"  [ ] {slug:8s} FAILED (check DB config / logs)")
    print(f"Loaded {ok}/{len(packs)} packs.")
    return 0 if ok == len(packs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
