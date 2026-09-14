"""Upload the fetched skills into Hermes so they appear on the Context page's
Skills sub-tab, scoped to the right business.

Each file goes through the ordinary /api/documents ingest path tagged
`source_system=business_skill` — that tag is the only thing separating a Skill
from a Context file, and it's what the Skills sub-tab filters on. Because they
land in `documents` like anything else, they are chunked and embedded too, so
the agent can pull one up mid-chat via `search_business_knowledge`.

    python scripts/seed_business_skills.py --dry-run          # show what would upload
    python scripts/seed_business_skills.py                    # upload everything
    python scripts/seed_business_skills.py --scope gak nh     # only these businesses
    python scripts/seed_business_skills.py --base-url https://your-hermes.app

Uploads are NOT idempotent — /api/documents creates a new row each time, so a
second run leaves duplicates in the list. --replace deletes any existing skill
with the same title in the same scope first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
MANIFEST = SKILLS_DIR / "MANIFEST.json"
SOURCE_SYSTEM = "business_skill"


def existing_skills(base_url: str, scope: str) -> dict[str, str]:
    """{title: document_id} for skills already stored under `scope`."""
    r = requests.get(
        f"{base_url}/api/documents",
        params={"entity_id": scope, "source_system": SOURCE_SYSTEM},
        timeout=60,
    )
    r.raise_for_status()
    return {d["title"]: d["document_id"] for d in r.json().get("documents", [])}


def upload(base_url: str, scope: str, title: str, body: str) -> dict:
    # The ingest path takes the filename as the document title, so send the
    # human-readable name rather than the on-disk slug.
    files = {"files": (f"{title}.md", body.encode("utf-8"), "text/markdown")}
    data = {
        "entity_id": scope,
        "source_system": SOURCE_SYSTEM,
        "sensitivity_level": "internal",
    }
    r = requests.post(f"{base_url}/api/documents", data=data, files=files, timeout=300)
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000",
                    help="Hermes base URL (default: %(default)s)")
    ap.add_argument("--scope", nargs="*", default=None,
                    help="Only these scopes (business slugs, or 'all')")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be uploaded and exit")
    ap.add_argument("--replace", action="store_true",
                    help="Delete an existing skill of the same title in the same scope first")
    args = ap.parse_args()

    base_url = args.base_url.rstrip("/")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    wanted = set(args.scope) if args.scope else None
    todo = [s for s in manifest["skills"] if not wanted or s["scope"] in wanted]

    if not todo:
        print("Nothing matched that scope filter.")
        return 1

    if args.dry_run:
        for s in todo:
            path = SKILLS_DIR / s["scope"] / s["file"]
            mark = "ok " if path.exists() else "MISSING"
            print(f"  {mark} {s['scope']:>4}  {s['title']}  <- skills/{s['scope']}/{s['file']}")
        print(f"\n{len(todo)} skill(s) would upload to {base_url}.")
        print("Run scripts/fetch_business_skills.py first if any are MISSING.")
        return 0

    # One lookup per scope, not per skill.
    prior: dict[str, dict[str, str]] = {}
    if args.replace:
        for scope in sorted({s["scope"] for s in todo}):
            prior[scope] = existing_skills(base_url, scope)

    ok, failed = 0, []
    for s in todo:
        path = SKILLS_DIR / s["scope"] / s["file"]
        if not path.exists():
            failed.append((s["title"], "not fetched — run scripts/fetch_business_skills.py"))
            print(f"  MISSING {s['scope']}/{s['file']}")
            continue
        try:
            if args.replace:
                old = prior.get(s["scope"], {}).get(f"{s['title']}.md") or \
                      prior.get(s["scope"], {}).get(s["title"])
                if old:
                    requests.delete(f"{base_url}/api/documents/{old}", timeout=60)
            result = upload(base_url, s["scope"], s["title"], path.read_text(encoding="utf-8"))
            doc = (result.get("documents") or [{}])[0]
            ok += 1
            print(f"  ok   {s['scope']:>4}  {s['title']}  "
                  f"({doc.get('chunks', '?')} chunks, {doc.get('embedded', '?')} embedded)")
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            failed.append((s["title"], str(exc)))
            print(f"  FAIL {s['scope']:>4}  {s['title']}: {exc}")

    print(f"\n{ok} uploaded, {len(failed)} failed.")
    for title, why in failed:
        print(f"  - {title}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
