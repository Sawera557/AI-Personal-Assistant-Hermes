"""Postgres rows for Hermes's persistent memory: `projects` + `memories`.

Five memory layers as two dimensions (see hermes/db/migrations/0009_memory_schema.sql):
`scope_type` (owner/business/project) is WHOSE memory it is; `memory_type`
(preference/style/rule/fact/decision/project_update) is WHAT KIND of content.

`write_memory()` is the one function that both writes and consolidates: it
takes a per-scope Postgres advisory lock so two concurrent extractions for the
same business/project/owner can't both see "no similar memory" and insert
near-duplicates — writes for *different* scopes still run fully in parallel.
"""
from __future__ import annotations

from typing import Any

from pgvector import Vector

from .connection import cursor
from .documents import _is_uuid

# Sensitivity ladder (low -> high) — same ordering as vector_store.py's, kept
# local since this is a small, stable list and hermes/db shouldn't depend on
# hermes/vector_store.
_SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]


def _allowed_sensitivities(max_level: str) -> list[str]:
    if max_level not in _SENSITIVITY_ORDER:
        return ["public"]
    return _SENSITIVITY_ORDER[: _SENSITIVITY_ORDER.index(max_level) + 1]


def _scope_clause(
    scope_type: str, entity_id: str | None, project_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Build the WHERE fragment for one scope. `scope_type` alone disambiguates
    owner (no entity_id/project_id) from business (entity_id required) from
    project — entity_id/project_id are added as exact-match filters only when
    given, so passing project_id=None with scope_type="project" means "any
    project" (there's no "active project" selector in the UI, so retrieval
    across all projects relies on scope_type + semantic relevance instead of a
    specific id). The scope_type + data-invariant (see the CHECK constraint in
    0009_memory_schema.sql) is what keeps owner/business/project rows from
    ever mixing, not blanket null-matching."""
    clauses = ["scope_type = %(scope_type)s"]
    params: dict[str, Any] = {"scope_type": scope_type}
    if entity_id is not None:
        clauses.append("entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if project_id is not None:
        clauses.append("project_id = %(project_id)s")
        params["project_id"] = project_id
    return " AND ".join(clauses), params


# ---- Projects ---------------------------------------------------------------
def known_projects() -> list[dict[str, Any]]:
    """Active projects, for the memory extractor's classification prompt and
    for scoping project-level retrieval. Small table — fetched fresh each call."""
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT project_id, slug, name, entity_id, kind, description "
            "FROM projects WHERE status = 'active' ORDER BY name;"
        )
        rows = cur.fetchall()
    return [
        {
            "project_id": str(r[0]), "slug": r[1], "name": r[2],
            "entity_id": str(r[3]) if r[3] else None, "kind": r[4], "description": r[5],
        }
        for r in rows
    ]


def create_project(
    *, slug: str, name: str, entity_id: str | None = None,
    kind: str = "business", description: str | None = None,
) -> str:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO projects (slug, name, entity_id, kind, description)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING project_id;
            """,
            (slug, name, entity_id, kind, description),
        )
        return str(cur.fetchone()[0])


def resolve_project(cur, project_ref: str | None) -> str | None:
    """A project_id (uuid) or slug -> its uuid, or None if unknown/blank."""
    if not project_ref:
        return None
    ref = str(project_ref).strip()
    if not ref:
        return None
    col = "project_id" if _is_uuid(ref) else "slug"
    cur.execute(f"SELECT project_id FROM projects WHERE {col} = %s;", (ref,))
    row = cur.fetchone()
    return str(row[0]) if row else None


# ---- Similarity lookup (used by both write-time consolidation and retrieval) -
def _find_similar(
    cur, embedding: list[float], *, scope_type: str,
    entity_id: str | None, project_id: str | None, top_k: int,
) -> list[dict[str, Any]]:
    scope_sql, params = _scope_clause(scope_type, entity_id, project_id)
    params.update({"vec": Vector(embedding), "top_k": top_k})
    cur.execute(
        f"""
        SELECT memory_id, memory_type, text, 1 - (embedding <=> %(vec)s) AS score
        FROM memories
        WHERE status = 'active' AND {scope_sql} AND embedding IS NOT NULL
        ORDER BY embedding <=> %(vec)s
        LIMIT %(top_k)s;
        """,
        params,
    )
    return [
        {"memory_id": str(r[0]), "memory_type": r[1], "text": r[2], "score": float(r[3])}
        for r in cur.fetchall()
    ]


def find_similar(
    embedding: list[float], *, scope_type: str,
    entity_id: str | None = None, project_id: str | None = None, top_k: int = 3,
) -> list[dict[str, Any]]:
    """Public read-only variant of `_find_similar`, own connection."""
    with cursor(commit=False) as cur:
        return _find_similar(
            cur, embedding, scope_type=scope_type,
            entity_id=entity_id, project_id=project_id, top_k=top_k,
        )


# ---- Write + consolidate ----------------------------------------------------
# Types considered "evolving" — a close-enough new statement supersedes the
# old one rather than sitting alongside it as a second near-identical fact.
_SUPERSEDABLE_TYPES = {"preference", "rule", "decision"}


def write_memory(
    *,
    scope_type: str,
    entity_id: str | None,
    project_id: str | None,
    memory_type: str,
    text: str,
    embedding: list[float],
    embed_model: str,
    sensitivity_level: str = "internal",
    region: str = "us-east-1",
    always_inject: bool = False,
    source_session_id: str | None = None,
    source_message_id: str | None = None,
    dup_threshold: float = 0.92,
    update_threshold: float = 0.75,
) -> dict[str, Any]:
    """Consolidate-and-write one candidate memory.

    Takes a per-scope advisory lock (hashed from entity_id/project_id/"owner")
    so two concurrent extractions touching the same scope can't both see "no
    similar memory" and insert near-duplicates — different scopes still write
    fully in parallel. Within the lock: near-duplicate (>= dup_threshold, same
    memory_type) -> skip; close match on an evolving type (>= update_threshold)
    -> insert new row + mark the old one superseded; otherwise -> plain insert.

    Returns {"action": "skip"|"update"|"add", "memory_id": ..., "superseded": <id or None>}.
    """
    scope_key = str(entity_id or project_id or "owner")
    with cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (scope_key,))

        similar = _find_similar(
            cur, embedding, scope_type=scope_type,
            entity_id=entity_id, project_id=project_id, top_k=3,
        )
        for cand in similar:
            if cand["memory_type"] == memory_type and cand["score"] >= dup_threshold:
                return {"action": "skip", "memory_id": cand["memory_id"], "superseded": None}

        supersede_id = None
        if memory_type in _SUPERSEDABLE_TYPES:
            for cand in similar:
                if cand["memory_type"] == memory_type and cand["score"] >= update_threshold:
                    supersede_id = cand["memory_id"]
                    break

        cur.execute(
            """
            INSERT INTO memories
                (scope_type, entity_id, project_id, memory_type, text, embedding,
                 embed_model, sensitivity_level, region, always_inject,
                 source_session_id, source_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING memory_id;
            """,
            (
                scope_type, entity_id, project_id, memory_type, text, Vector(embedding),
                embed_model, sensitivity_level, region, always_inject,
                source_session_id, source_message_id,
            ),
        )
        new_id = str(cur.fetchone()[0])

        if supersede_id:
            cur.execute(
                "UPDATE memories SET status = 'superseded', superseded_by = %s WHERE memory_id = %s;",
                (new_id, supersede_id),
            )
            return {"action": "update", "memory_id": new_id, "superseded": supersede_id}
        return {"action": "add", "memory_id": new_id, "superseded": None}


# ---- Retrieval ---------------------------------------------------------------
def always_inject_memories(
    *, scope_type: str, entity_id: str | None = None, project_id: str | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    """Standing rules/preferences for one scope — cheap indexed lookup, no
    embedding needed (see the partial index in 0009_memory_schema.sql)."""
    scope_sql, params = _scope_clause(scope_type, entity_id, project_id)
    params["limit"] = limit
    with cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT memory_id, memory_type, text
            FROM memories
            WHERE status = 'active' AND always_inject AND {scope_sql}
            ORDER BY updated_at DESC
            LIMIT %(limit)s;
            """,
            params,
        )
        rows = cur.fetchall()
    return [{"memory_id": str(r[0]), "memory_type": r[1], "text": r[2]} for r in rows]


def semantic_search_memories(
    embedding: list[float],
    *,
    scope_type: str,
    entity_id: str | None = None,
    project_id: str | None = None,
    top_k: int = 5,
    max_sensitivity: str = "internal",
) -> list[dict[str, Any]]:
    """Relevance-ranked memories for one scope, respecting the sensitivity
    ladder the same way document search does."""
    scope_sql, params = _scope_clause(scope_type, entity_id, project_id)
    params.update({
        "vec": Vector(embedding), "top_k": top_k,
        "allowed": _allowed_sensitivities(max_sensitivity),
    })
    with cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT memory_id, memory_type, text, 1 - (embedding <=> %(vec)s) AS score
            FROM memories
            WHERE status = 'active' AND {scope_sql}
              AND sensitivity_level = ANY(%(allowed)s) AND embedding IS NOT NULL
            ORDER BY embedding <=> %(vec)s
            LIMIT %(top_k)s;
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {"memory_id": str(r[0]), "memory_type": r[1], "text": r[2], "score": float(r[3])}
        for r in rows
    ]


# ---- Browse (plain listing — no embedding involved) --------------------------
def list_memories(
    *, entity_id: str | None = None, scope_type: str | None = None,
) -> list[dict[str, Any]]:
    """All active memories, for the Memories view in the UI — not a similarity
    search. With `entity_id`, owner-scope memories still always show (global
    by definition) alongside that business's own business/project memories,
    same logic hermes/memory_retrieve.py uses for live retrieval, just without
    the embedding step. `entity_id=None` ("All Businesses") returns everything."""
    where = ["m.status = 'active'"]
    params: dict[str, Any] = {}
    if scope_type:
        where.append("m.scope_type = %(scope_type)s")
        params["scope_type"] = scope_type
    if entity_id:
        where.append(
            "(m.scope_type = 'owner' "
            "OR (m.scope_type = 'business' AND m.entity_id = %(entity_id)s) "
            "OR (m.scope_type = 'project' AND m.project_id IN "
            "    (SELECT project_id FROM projects WHERE entity_id = %(entity_id)s)))"
        )
        params["entity_id"] = entity_id

    with cursor(commit=False) as cur:
        cur.execute(
            f"""
            SELECT m.memory_id, m.scope_type, m.memory_type, m.text, m.sensitivity_level,
                   m.always_inject, m.created_at, m.updated_at,
                   e.slug AS entity_slug, e.name AS entity_name,
                   p.slug AS project_slug, p.name AS project_name
            FROM memories m
            LEFT JOIN entities e ON e.entity_id = m.entity_id
            LEFT JOIN projects p ON p.project_id = m.project_id
            WHERE {' AND '.join(where)}
            ORDER BY m.updated_at DESC;
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "memory_id": str(r[0]), "scope_type": r[1], "memory_type": r[2], "text": r[3],
            "sensitivity_level": r[4], "always_inject": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "updated_at": r[7].isoformat() if r[7] else None,
            "entity_slug": r[8], "entity_name": r[9],
            "project_slug": r[10], "project_name": r[11],
        }
        for r in rows
    ]


def delete_memory(memory_id: str) -> bool:
    """Hard-delete one memory. Returns True if a row was found and removed."""
    if not _is_uuid(memory_id):
        return False
    with cursor() as cur:
        cur.execute("DELETE FROM memories WHERE memory_id = %s;", (memory_id,))
        return cur.rowcount > 0
