"""Context packs — the read/write DB layer for always-present business context
(Plan task B1). Table created in migration 0017_context_packs_playbooks.sql.

One row per slug ('global' plus one per business). The agent reads these through
the cached loader in hermes/context_packs.py and injects them into the system
prompt every turn — the durable standing-facts block Hermes otherwise lacks.

Every function degrades to a safe empty result on error: a context-pack problem
must never break a chat turn."""
from __future__ import annotations

import logging
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.context_packs")


def get_pack(slug: str) -> dict[str, Any] | None:
    """The pack row for a slug, or None (missing / error)."""
    s = (slug or "").strip().lower()
    if not s:
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT slug, content_md, version FROM context_packs WHERE slug = %s;",
                (s,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — degrade to "no pack"
        log.warning("get_pack(%s) failed: %s", s, exc)
        return None
    if not row:
        return None
    return {"slug": row[0], "content_md": row[1], "version": row[2]}


def upsert_pack(slug: str, content_md: str) -> dict[str, Any] | None:
    """Insert or replace a pack by slug; bumps version. Returns {slug, version}
    or None on failure."""
    s = (slug or "").strip().lower()
    if not s:
        return None
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO context_packs (slug, content_md, version, updated_at)
                       VALUES (%s, %s, 1, now())
                   ON CONFLICT (slug) DO UPDATE
                       SET content_md = EXCLUDED.content_md,
                           version = context_packs.version + 1,
                           updated_at = now()
                   RETURNING slug, version;""",
                (s, content_md or ""),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("upsert_pack(%s) failed: %s", s, exc)
        return None
    return {"slug": row[0], "version": row[1]} if row else None


def list_packs() -> list[dict[str, Any]]:
    """All packs (slug + version + updated_at), for an admin/listing view."""
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT slug, version, updated_at FROM context_packs ORDER BY slug;"
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_packs failed: %s", exc)
        return []
    return [
        {"slug": r[0], "version": r[1], "updated_at": r[2].isoformat() if r[2] else None}
        for r in rows
    ]
