"""Memory Extractor — the silent backend step that turns a finished chat turn
into durable owner/business/project memory.

Not a tool the model calls (see hermes/tools.py) — this is the app-driven
pipeline: user talks -> Hermes answers -> this reviews the exchange -> useful
memories are saved to Postgres with a pgvector embedding -> the next turn's
hermes/memory_retrieve.py recalls them before answering.

Triggered from app.py right after each `store.record_turn(...)` call — that's
where a turn is actually persisted (see hermes/db/conversations.py), not
inside hermes/agent.py. `extract_async()` must add zero latency to the
user-facing reply, so it does nothing but start a background thread; the
thread body is wrapped in a broad try/except so a bad extraction never affects
the conversation itself.
"""
from __future__ import annotations

import logging
import threading

from .config import Config
from .db import documents as docs_repo
from .db import memories as mem_repo
from .llm_json import call_claude_json
from .vector_store import EMBED_MODEL, embed

log = logging.getLogger("hermes.memory_extract")

_MEMORY_TYPES = ("preference", "style", "rule", "fact", "decision", "project_update")
_SCOPE_TYPES = ("owner", "business", "project")
_SENSITIVITY_LEVELS = ("public", "internal", "confidential", "restricted")

_SYSTEM_TEMPLATE = """You are Hermes's memory extractor — a silent step that runs after every \
chat turn, never seen by the user. Your only job: decide whether anything in this exchange is \
worth remembering long-term, and if so, classify it precisely.

Remember things like: a personal preference or writing-style habit ("prefers direct, source-\
backed answers"), an approval rule ("QuickBooks writes need approval first"), a business-\
specific fact or rule, a standing decision, or a project update. Do NOT remember one-off \
operational requests ("send this email", "what's on my calendar today") — those are noise, \
not memory.

KNOWN BUSINESSES (use as entity_hint when scope_type is "business"):
{entities}

KNOWN PROJECTS (use as project_hint when scope_type is "project" — NEVER invent a new project \
slug; if nothing here fits, fall back to scope_type "business" or "owner" instead):
{projects}
{locked_hint}
Sensitivity: public < internal < confidential < restricted. A viewer may see their level and \
everything below it. Default to "confidential" for anything touching Comfort Services (health/\
PHI-adjacent) or Nour Hammam (women-only service details) — never "internal" for those unless \
the content is clearly generic. Default to "internal" otherwise.

Respond with ONLY a single JSON object, no other text, in exactly this shape:
{{
  "memories": [
    {{
      "scope_type": "owner" | "business" | "project",
      "entity_hint": "<business slug from the list above, or null>",
      "project_hint": "<project slug from the list above, or null>",
      "memory_type": "preference" | "style" | "rule" | "fact" | "decision" | "project_update",
      "text": "<the fact itself, written as a standalone statement — no pronouns needing prior context>",
      "sensitivity_level": "public" | "internal" | "confidential" | "restricted",
      "always_inject": true | false
    }}
  ]
}}

Rules:
- scope_type/entity_hint/project_hint reflect what the TEXT is actually about, not necessarily \
which business the conversation happened to be locked to.
- always_inject = true ONLY for firm, general "always/never/must/required" standing rules or \
preferences you're confident about — most memories should be false (surfaced only when relevant).
- If there is nothing worth remembering this turn, return {{"memories": []}}. This is the common \
case — most turns are pure task execution, not new durable information. Do not force an entry \
just to have something to say.
"""


def _entities_block(entities: list[dict]) -> str:
    if not entities:
        return "(none)"
    return "\n".join(f"  - {e['slug']}: {e['name']}" for e in entities)


def _projects_block(projects: list[dict]) -> str:
    if not projects:
        return "(none yet)"
    return "\n".join(
        f"  - {p['slug']}: {p['name']} ({p['kind']})" for p in projects
    )


def _build_system(entity_slug: str | None, entities: list[dict], projects: list[dict]) -> str:
    locked_hint = ""
    if entity_slug and entity_slug != "all":
        name = next((e["name"] for e in entities if e["slug"] == entity_slug), entity_slug)
        locked_hint = (
            f"\nThis conversation is currently locked to business '{name}' ({entity_slug}) — "
            "treat that as a hint only, not a forced classification.\n"
        )
    return _SYSTEM_TEMPLATE.format(
        entities=_entities_block(entities),
        projects=_projects_block(projects),
        locked_hint=locked_hint,
    )


def extract_async(
    *, session_id: str, entity_slug: str | None, user_text: str, reply: str,
) -> None:
    """Fire-and-forget: spawn the background extraction thread and return
    immediately. Must never add latency to the caller."""
    if not Config.MEMORY_EXTRACT_ENABLED:
        return
    if not (user_text or "").strip():
        return
    threading.Thread(
        target=_safe_extract_and_store,
        args=(session_id, entity_slug, user_text, reply),
        daemon=True,
    ).start()


def _safe_extract_and_store(
    session_id: str, entity_slug: str | None, user_text: str, reply: str,
) -> None:
    try:
        _extract_and_store(session_id, entity_slug, user_text, reply)
    except Exception:  # noqa: BLE001 — never let a bad extraction surface anywhere
        log.warning("memory extraction failed for session %s", session_id, exc_info=True)


def _extract_and_store(
    session_id: str, entity_slug: str | None, user_text: str, reply: str,
) -> None:
    entities = docs_repo.list_entities()
    projects = mem_repo.known_projects()

    system = _build_system(entity_slug, entities, projects)
    user_message = f"User said: {user_text}\n\nHermes replied: {reply}"

    parsed = call_claude_json(
        system=system, user_message=user_message, max_tokens=800,
        fallback_key="_raw", model=Config.GUIDANCE_MODEL,
    )
    candidates = parsed.get("memories")
    if not isinstance(candidates, list) or not candidates:
        return

    entity_by_slug = {e["slug"]: e["entity_id"] for e in entities}
    project_by_slug = {p["slug"]: p["project_id"] for p in projects}

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        text = (cand.get("text") or "").strip()
        scope_type = cand.get("scope_type")
        memory_type = cand.get("memory_type")
        if not text or scope_type not in _SCOPE_TYPES or memory_type not in _MEMORY_TYPES:
            continue

        entity_id = None
        project_id = None
        if scope_type == "business":
            entity_id = entity_by_slug.get(cand.get("entity_hint"))
            if not entity_id:
                continue  # can't file a business memory against an unresolvable business
        elif scope_type == "project":
            project_id = project_by_slug.get(cand.get("project_hint"))
            if not project_id:
                continue  # extractor is instructed never to invent project slugs

        sensitivity = cand.get("sensitivity_level")
        if sensitivity not in _SENSITIVITY_LEVELS:
            sensitivity = "internal"

        vector = embed([text])[0]
        result = mem_repo.write_memory(
            scope_type=scope_type,
            entity_id=entity_id,
            project_id=project_id,
            memory_type=memory_type,
            text=text,
            embedding=vector,
            embed_model=EMBED_MODEL,
            sensitivity_level=sensitivity,
            always_inject=bool(cand.get("always_inject")),
            source_session_id=session_id,
            dup_threshold=Config.MEMORY_DUP_THRESHOLD,
            update_threshold=Config.MEMORY_UPDATE_THRESHOLD,
        )
        log.info(
            "memory %s scope=%s type=%s memory_id=%s",
            result["action"], scope_type, memory_type, result["memory_id"],
        )
