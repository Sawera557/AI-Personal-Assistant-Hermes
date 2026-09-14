"""Download the Agent Skills named in skills/MANIFEST.json into skills/<scope>/.

Each skill is fetched verbatim from its upstream repo and written with a short
provenance header prepended, so anyone reading it in the Context page's Skills
tab can see where it came from and under which licence. Re-run it to refresh.

    python scripts/fetch_business_skills.py           # fetch everything
    python scripts/fetch_business_skills.py gak nh    # only these scopes

Nothing here touches the database — see scripts/seed_business_skills.py for that.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
MANIFEST = SKILLS_DIR / "MANIFEST.json"


def header(skill: dict, source: dict, url: str) -> str:
    """A provenance block prepended to every downloaded skill. It survives into
    the ingested document, so a citation in chat can be traced back upstream."""
    return (
        f"<!-- Fetched by scripts/fetch_business_skills.py on {date.today().isoformat()}. "
        f"Do not hand-edit; edit skills/MANIFEST.json and re-run. -->\n\n"
        f"> **Skill:** {skill['title']}  \n"
        f"> **Scope:** {skill['business']} (`{skill['scope']}`)  \n"
        f"> **Source:** [{source['repo']}]({url}) — {source['license']}  \n"
        f"> **Why this business:** {skill['why']}\n\n"
        f"---\n\n"
    )


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-skills-fetch"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def main(argv: list[str]) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources = manifest["sources"]
    wanted = set(argv) or None

    ok, failed = 0, []
    for skill in manifest["skills"]:
        if wanted and skill["scope"] not in wanted:
            continue
        source = sources[skill["source"]]
        url = source["raw"].format(path=skill["path"])
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001 — one bad URL shouldn't stop the rest
            failed.append((skill["file"], f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL {skill['scope']}/{skill['file']}: {exc}")
            continue

        out = SKILLS_DIR / skill["scope"] / skill["file"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(header(skill, source, source["url"]) + body, encoding="utf-8")
        ok += 1
        print(f"  ok   {skill['scope']}/{skill['file']}  ({len(body):,} bytes)")

    print(f"\n{ok} fetched, {len(failed)} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
