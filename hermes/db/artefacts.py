"""Postgres-backed store for Artefacts — mirrors hermes/db/scheduled_tasks.py.

artefacts — one row per artefact, owned by the single owner user, scoped to one
            business (entity_id) or NULL for the cross-business 'reviews' wrapper.
            Carries the title, the source_type, the source_binding (how to fetch
            the live source), the render_recipe (how to render it), the captured
            business_slug (so an open-time re-sync can rebuild the right Maton
            client with no chat turn in scope), and a disposable rendered_html /
            last_synced_at / last_error cache.

Nothing here fetches or renders — that stays in hermes/artefacts.py, so this
module is pure storage.
"""
from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.artefacts")

# rendered_html is deliberately omitted from the list/meta column set to keep
# those payloads light (same reasoning as list_documents omitting body_text).
_META_COLS = (
    "a.artefact_id, a.title, a.source_type, a.source_binding, a.render_recipe, "
    "a.business_slug, a.last_synced_at, a.last_error, a.created_at, a.updated_at, e.slug"
)


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _owner(cur) -> str:
    """Same owner-first resolution as conversations.py / scheduled_tasks.py."""
    email = (os.getenv("OWNER_EMAIL") or "owner@hermes.local").strip().lower()
    name = (os.getenv("OWNER_NAME") or "Owner").strip()
    cur.execute("SELECT user_id FROM users WHERE lower(email) = %s;", (email,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING user_id;",
            (name, email),
        )
        row = cur.fetchone()
    return str(row[0])


def _resolve_entity_id(cur, entity_slug: str | None) -> str | None:
    """entities.slug (or a uuid) -> entities.entity_id, or None for unscoped."""
    eid = str(entity_slug or "").strip()
    if not eid:
        return None
    col = "entity_id" if _is_uuid(eid) else "slug"
    cur.execute(f"SELECT entity_id FROM entities WHERE {col} = %s;", (eid,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _jsonb(value: Any) -> dict[str, Any]:
    """psycopg returns jsonb already-decoded; guard against str just in case."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return {}
    return value or {}


def _meta_row_to_dict(row: tuple) -> dict[str, Any]:
    from ..businesses import to_business_slug  # local import: avoid a cycle

    (artefact_id, title, source_type, source_binding, render_recipe,
     business_slug, last_synced_at, last_error, created, updated, entity_slug) = row
    return {
        "artefact_id": str(artefact_id),
        "title": title,
        "source_type": source_type,
        "source_binding": _jsonb(source_binding),
        "render_recipe": _jsonb(render_recipe),
        # business_slug is stored directly (load-bearing for re-sync); fall back to
        # deriving it from the entity slug for older/edge rows.
        "business": business_slug or to_business_slug(entity_slug),
        "business_slug": business_slug,
        "last_synced_at": _iso(last_synced_at),
        "last_error": last_error,
        "created_at": _iso(created),
        "updated_at": _iso(updated),
    }


def create_artefact(
    *,
    entity_slug: str | None,
    business_slug: str | None,
    title: str,
    source_type: str,
    source_binding: dict[str, Any],
    render_recipe: dict[str, Any],
) -> str | None:
    """Insert an artefact and return its artefact_id (or None on failure).

    `entity_slug` may be None for the cross-business 'reviews' wrapper.
    `business_slug` is the businesses.slug captured now so a later open-time
    re-sync can rebuild the correct per-business Maton client."""
    try:
        with cursor() as cur:
            owner = _owner(cur)
            ent = _resolve_entity_id(cur, entity_slug)  # None valid (reviews)
            cur.execute(
                """INSERT INTO artefacts
                       (user_id, entity_id, business_slug, title, source_type,
                        source_binding, render_recipe)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING artefact_id;""",
                (
                    owner, ent, business_slug, title, source_type,
                    json.dumps(source_binding or {}),
                    json.dumps(render_recipe or {}),
                ),
            )
            return str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("create_artefact failed: %s", exc)
        return None


def get_artefact(artefact_id: str) -> dict[str, Any] | None:
    """Metadata only (no rendered_html) — for the detail route."""
    if not _is_uuid(artefact_id):
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_META_COLS} FROM artefacts a
                    LEFT JOIN entities e ON e.entity_id = a.entity_id
                    WHERE a.artefact_id = %s;""",
                (artefact_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_artefact(%s) failed: %s", artefact_id, exc)
        return None
    return _meta_row_to_dict(row) if row else None


# get_render_inputs is an alias for get_artefact — it returns exactly the fields
# sync_and_render needs (source_type, source_binding, render_recipe,
# business_slug). Kept as a named entry point so the render pipeline reads clearly.
get_render_inputs = get_artefact


def list_artefacts(entity_slug: str | None = None) -> list[dict[str, Any]]:
    """All of the owner's artefacts (newest-updated first). Pass a business slug
    to scope to one business; omit (or "all") for every artefact."""
    try:
        with cursor(commit=False) as cur:
            owner = _owner(cur)
            where = ["a.user_id = %s"]
            params: list[Any] = [owner]
            if entity_slug and entity_slug != "all":
                ent = _resolve_entity_id(cur, entity_slug)
                where.append("a.entity_id = %s")
                params.append(ent)
            cur.execute(
                f"""SELECT {_META_COLS} FROM artefacts a
                    LEFT JOIN entities e ON e.entity_id = a.entity_id
                    WHERE {' AND '.join(where)}
                    ORDER BY a.updated_at DESC
                    LIMIT 200;""",
                params,
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_artefacts failed: %s", exc)
        return []
    return [_meta_row_to_dict(r) for r in rows]


def get_cached_html(artefact_id: str) -> tuple[str | None, str | None]:
    """(rendered_html, last_synced_at_iso) for the instant-paint path."""
    if not _is_uuid(artefact_id):
        return (None, None)
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT rendered_html, last_synced_at FROM artefacts WHERE artefact_id = %s;",
                (artefact_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_cached_html(%s) failed: %s", artefact_id, exc)
        return (None, None)
    if not row:
        return (None, None)
    return (row[0], _iso(row[1]))


def save_render(artefact_id: str, html: str) -> None:
    """Cache a successful render: set html + last_synced_at=now(), clear error."""
    if not _is_uuid(artefact_id):
        return
    try:
        with cursor() as cur:
            cur.execute(
                """UPDATE artefacts
                       SET rendered_html = %s, last_synced_at = now(), last_error = NULL
                     WHERE artefact_id = %s;""",
                (html, artefact_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("save_render(%s) failed: %s", artefact_id, exc)


def save_error(artefact_id: str, message: str) -> None:
    """Record a sync failure without touching the (possibly stale) cached html."""
    if not _is_uuid(artefact_id):
        return
    try:
        with cursor() as cur:
            cur.execute(
                "UPDATE artefacts SET last_error = %s WHERE artefact_id = %s;",
                ((message or "")[:2000], artefact_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("save_error(%s) failed: %s", artefact_id, exc)


def update_artefact(
    artefact_id: str,
    *,
    title: str | None = None,
    source_binding: dict[str, Any] | None = None,
    render_recipe: dict[str, Any] | None = None,
) -> bool:
    """Patch an artefact. Only the fields passed (non-None) are written."""
    if not _is_uuid(artefact_id):
        return False
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = %s"); params.append(title)
    if source_binding is not None:
        sets.append("source_binding = %s"); params.append(json.dumps(source_binding))
    if render_recipe is not None:
        sets.append("render_recipe = %s"); params.append(json.dumps(render_recipe))
    if not sets:
        return False
    sets.append("updated_at = now()")
    params.append(artefact_id)
    try:
        with cursor() as cur:
            cur.execute(
                f"UPDATE artefacts SET {', '.join(sets)} WHERE artefact_id = %s;",
                params,
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("update_artefact(%s) failed: %s", artefact_id, exc)
        return False


def delete_artefact(artefact_id: str) -> bool:
    """Hard-delete an artefact."""
    if not _is_uuid(artefact_id):
        return False
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM artefacts WHERE artefact_id = %s;", (artefact_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_artefact(%s) failed: %s", artefact_id, exc)
        return False
