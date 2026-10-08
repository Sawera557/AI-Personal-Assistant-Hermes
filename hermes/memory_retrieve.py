"""Memory retrieval — the read half of persistent memory.

Fetched once per turn, before the tool-iteration loop in hermes/agent.py, and
formatted into a compact block injected into the system prompt. Two kinds of
memory per scope: "always" (standing rules/preferences, cheap indexed lookup,
no embedding) and "semantic" (relevance-ranked to the current message).

Concurrent-fetch pattern mirrors hermes/meeting_copilot.py::get_guidance() —
the slowest source sets the latency, not the sum of all of them.

Project memories have no "active project" selector in the UI (see the plan):
retrieval searches across ALL projects by scope_type alone and relies on
semantic relevance to surface the right one, rather than filtering to one
specific project_id.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .businesses import to_entity_slug
from .config import Config, int_env
from .db import documents as docs_repo
from .db import memories as mem_repo
from .db.connection import cursor
from .vector_store import embed

import os  # B4: env-driven memory-block cap (default raised 2000 -> 8000)
_MAX_BLOCK_CHARS = int_env("MEMORY_BLOCK_MAX_CHARS", 8000)


def _resolve_entity_id(active_business: str | None) -> str | None:
    """Business slug (e.g. "afk") -> entities.entity_id, or None for "all"/
    unset/unresolvable. `active_business` is a BUSINESS slug, not an entities
    slug — same conversion hermes.search/meeting_copilot require."""
    if not active_business or active_business == "all":
        return None
    entity_slug = to_entity_slug(active_business)
    if not entity_slug:
        return None
    with cursor(commit=False) as cur:
        resolved = docs_repo.resolve_entity(cur, entity_slug)
    return resolved[0] if resolved else None


def _always(scope_type: str, *, entity_id: str | None = None, project_id: str | None = None) -> list[dict]:
    try:
        return mem_repo.always_inject_memories(
            scope_type=scope_type, entity_id=entity_id, project_id=project_id,
            limit=Config.MEMORY_ALWAYS_INJECT_MAX,
        )
    except Exception:  # noqa: BLE001 — degrade to "no memory" rather than fail the turn
        return []


def _semantic(vector: list[float], scope_type: str, *, entity_id: str | None = None, project_id: str | None = None) -> list[dict]:
    try:
        return mem_repo.semantic_search_memories(
            vector, scope_type=scope_type, entity_id=entity_id, project_id=project_id,
            top_k=Config.MEMORY_SEMANTIC_TOP_K, max_sensitivity=Config.MEMORY_MAX_SENSITIVITY,
        )
    except Exception:  # noqa: BLE001
        return []


def build_memory_block(*, active_business: str | None, latest_user_text: str) -> str:
    """Owner memory is always considered; business/project memory only kicks
    in once a specific business is resolved ("all businesses" mode fetches
    owner-scope only — a deliberate v1 simplification, see the plan)."""
    if not Config.MEMORY_EXTRACT_ENABLED:
        return ""

    entity_id = _resolve_entity_id(active_business)
    text = (latest_user_text or "").strip()

    tasks = {"owner_always": lambda: _always("owner")}
    if entity_id:
        tasks["business_always"] = lambda: _always("business", entity_id=entity_id)
        tasks["project_always"] = lambda: _always("project")

    vector: list[float] | None = None
    if text:
        try:
            vector = embed([text], task="query")[0]
        except Exception:  # noqa: BLE001 — no relevance search this turn, always-inject still runs
            vector = None
    if vector is not None:
        tasks["owner_semantic"] = lambda: _semantic(vector, "owner")
        if entity_id:
            tasks["business_semantic"] = lambda: _semantic(vector, "business", entity_id=entity_id)
            tasks["project_semantic"] = lambda: _semantic(vector, "project")

    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {name: pool.submit(fn) for name, fn in tasks.items()}
        for name, fut in futures.items():
            results[name] = fut.result()
    return _format_block(results)


def _db_off() -> bool:
    """HERMES_DISABLE_DB set, or no database configured. Memories live only in
    Postgres, so there is nothing to read — and trying would only stall the
    turn on connection retries before failing open to "no memory" anyway."""
    if os.getenv("HERMES_DISABLE_DB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return not os.getenv("DBRDS_HOST", "").strip()


def build_always_block(*, active_business: str | None) -> str:
    """Only the ALWAYS tier: the owner's and the locked business's standing
    rules/preferences (always_inject rows). Query-independent, so no embedding
    and no relevance search — the same two cheap indexed lookups
    build_memory_block starts with.

    Used by the standing-rules core (TURN_COST_PLAN step 0.3) on a turn that
    offers tools while Business context is off: the semantic tier, RAG and
    recall are dropped with the context, but a standing rule the owner saved
    ("never contact tenants by text", "CC me on every invoice email") has to
    keep travelling with the tools that could break it.

    Project-scope always rows are deliberately left out: they are fetched with
    no business filter (TURN_COST_PLAN B15), so in a locked turn they could
    carry another business's rules in."""
    if not Config.MEMORY_EXTRACT_ENABLED or _db_off():
        return ""
    entity_id = _resolve_entity_id(active_business)
    results: dict[str, list[dict]] = {"owner_always": _always("owner")}
    if entity_id:
        results["business_always"] = _always("business", entity_id=entity_id)
    return _format_block(results)


def _format_block(results: dict[str, list[dict]]) -> str:
    """Render fetched memory rows (keyed `<scope>_always` / `<scope>_semantic`)
    as the grouped, de-duplicated, length-capped block the prompt carries.
    Shared by the full block and the always-only block so the two can't drift
    into two different shapes for the same rows."""
    seen: set[str] = set()
    groups: dict[str, list[str]] = {"Standing rules & preferences": [], "Business": [], "Project": []}
    for key, always_label, semantic_label in (
        ("owner", "Standing rules & preferences", "Standing rules & preferences"),
        ("business", "Business", "Business"),
        ("project", "Project", "Project"),
    ):
        for suffix, label in ((f"{key}_always", always_label), (f"{key}_semantic", semantic_label)):
            for item in results.get(suffix, []):
                mid = item["memory_id"]
                if mid in seen:
                    continue
                seen.add(mid)
                groups[label].append(item["text"])

    lines: list[str] = []
    for label, items in groups.items():
        if items:
            lines.append(f"{label}:")
            lines.extend(f"  - {t}" for t in items)
    block = "\n".join(lines).strip()
    if len(block) > _MAX_BLOCK_CHARS:
        cut = block[:_MAX_BLOCK_CHARS]
        nl = cut.rfind("\n")
        block = (cut[:nl] if nl > 0 else cut).rstrip() + "\n  ...(truncated)"
    return block
