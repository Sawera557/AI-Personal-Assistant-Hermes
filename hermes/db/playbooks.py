"""Playbooks — the read/write DB layer for scheduled-run procedures (Plan task
D1). Tables created in migration 0017_context_packs_playbooks.sql.

A playbook is a written procedure (arrears-chase, friday-brief, ...). A scheduled
task is linked to one via the task_playbooks table; the scheduler injects the
linked playbook into the run prompt in hermes/scheduler.py::build_run_prompt.

Every function degrades safely on error — a playbook problem must never break a
scheduled run (the run just proceeds without the procedure)."""
from __future__ import annotations

import logging
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.playbooks")


def get_playbook(slug: str) -> dict[str, Any] | None:
    s = (slug or "").strip().lower()
    if not s:
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT slug, name, description, content_md, enabled "
                "FROM playbooks WHERE slug = %s;",
                (s,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_playbook(%s) failed: %s", s, exc)
        return None
    if not row:
        return None
    return {
        "slug": row[0], "name": row[1], "description": row[2],
        "content_md": row[3], "enabled": row[4],
    }


def upsert_playbook(
    *, slug: str, name: str, description: str, content_md: str, enabled: bool = True
) -> dict[str, Any] | None:
    s = (slug or "").strip().lower()
    if not s or not content_md:
        return None
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO playbooks (slug, name, description, content_md, enabled, updated_at)
                       VALUES (%s, %s, %s, %s, %s, now())
                   ON CONFLICT (slug) DO UPDATE
                       SET name = EXCLUDED.name,
                           description = EXCLUDED.description,
                           content_md = EXCLUDED.content_md,
                           enabled = EXCLUDED.enabled,
                           updated_at = now()
                   RETURNING slug;""",
                (s, name or "", description or "", content_md, bool(enabled)),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("upsert_playbook(%s) failed: %s", s, exc)
        return None
    return {"slug": row[0]} if row else None


def list_playbooks() -> list[dict[str, Any]]:
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT slug, name, description, enabled FROM playbooks ORDER BY slug;"
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_playbooks failed: %s", exc)
        return []
    return [
        {"slug": r[0], "name": r[1], "description": r[2], "enabled": r[3]}
        for r in rows
    ]


def assign_playbook(task_id: str, slug: str | None) -> bool:
    """Link a scheduled task to a playbook (slug), or unlink it (slug=None)."""
    if not task_id:
        return False
    try:
        with cursor() as cur:
            if slug:
                cur.execute(
                    """INSERT INTO task_playbooks (task_id, playbook_slug, assigned_at)
                           VALUES (%s, %s, now())
                       ON CONFLICT (task_id) DO UPDATE
                           SET playbook_slug = EXCLUDED.playbook_slug, assigned_at = now();""",
                    (task_id, slug.strip().lower()),
                )
            else:
                cur.execute("DELETE FROM task_playbooks WHERE task_id = %s;", (task_id,))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("assign_playbook(%s) failed: %s", task_id, exc)
        return False


def playbook_for_task(task_id: str | None) -> dict[str, Any] | None:
    """The ENABLED playbook linked to a task, or None. Called by the scheduler on
    every run — a missing/disabled/unlinked playbook simply returns None."""
    if not task_id:
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT p.slug, p.name, p.content_md
                       FROM task_playbooks tp
                       JOIN playbooks p ON p.slug = tp.playbook_slug
                       WHERE tp.task_id = %s AND p.enabled;""",
                (task_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("playbook_for_task(%s) failed: %s", task_id, exc)
        return None
    if not row:
        return None
    return {"slug": row[0], "name": row[1], "content_md": row[2]}
