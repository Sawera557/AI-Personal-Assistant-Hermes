"""Seed the initial `projects` list so day-1 memory extraction has something
to match project_hint against (the extractor never invents new project slugs
— see hermes/memory_extract.py).

Covers both real-world business projects (tied to an entity) and internal
engineering projects for building Hermes itself (entity_id left NULL).
Idempotent — re-running just upserts the name for each slug.

Usage:
    python scripts/seed_projects.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes.config import Config  # noqa: E402  (loads .env)
from hermes.db import documents as docs_repo  # noqa: E402
from hermes.db import memories as mem_repo  # noqa: E402

# (slug, name, entity_slug or None, kind, description)
_PROJECTS = [
    ("afro_farm_expansion", "Afro Farm expansion", "afro_farm", "business",
     "Expanding Afro Farm Holding's operations — new acreage, crops, or infrastructure."),
    ("nour_hammam_design", "Nour Hammam design", "nour_hammam", "business",
     "Designing and building out the Nour Hammam women-only spa concept."),
    ("hermes_agent_build", "Hermes AI Agent build", None, "engineering",
     "Building Hermes itself — the agent loop, tools, and integrations."),
    ("daily_briefing_agent", "Daily Briefing Agent", None, "engineering",
     "The automated daily/weekly business briefing feature."),
    ("data_ingestion_api", "Data Ingestion API", None, "engineering",
     "The document/data ingestion pipeline and its API surface."),
    ("meeting_copilot", "Meeting Copilot", None, "engineering",
     "The live meeting guidance, prep-brief, and AI notes feature."),
]


def main() -> int:
    _ = Config  # ensure .env is loaded
    entity_by_slug = {e["slug"]: e["entity_id"] for e in docs_repo.list_entities()}

    for slug, name, entity_slug, kind, description in _PROJECTS:
        entity_id = entity_by_slug.get(entity_slug) if entity_slug else None
        if entity_slug and not entity_id:
            print(f"  ! skipping {slug!r} — unknown entity slug {entity_slug!r}")
            continue
        project_id = mem_repo.create_project(
            slug=slug, name=name, entity_id=entity_id, kind=kind, description=description,
        )
        print(f"  {slug} -> {project_id}")

    print(f"Seeded {len(_PROJECTS)} project(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
