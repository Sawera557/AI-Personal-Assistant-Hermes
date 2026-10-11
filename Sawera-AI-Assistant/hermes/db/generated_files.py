"""Postgres-backed index of agent-generated files — mirrors hermes/db/artefacts.py.

generated_files — one row per create_file output (PDF/DOCX/XLSX/HTML/text on S3),
                  owned by the single owner user, scoped to one business
                  (entity_id) or NULL for the unscoped 'general' case. Stores the
                  durable s3_uri; the API re-presigns a fresh download link on
                  demand rather than persisting an expiring one.

Pure storage — no S3 or presigning here (that stays in storage.py / the API).
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.generated_files")

_COLS = ("f.file_id, f.filename, f.content_type, f.s3_uri, f.size_bytes, "
         "f.business_slug, f.created_at, e.slug")


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _owner(cur) -> str:
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


def _row_to_dict(row: tuple) -> dict[str, Any]:
    from ..businesses import to_business_slug  # local import: avoid a cycle
    (file_id, filename, content_type, s3_uri, size_bytes, business_slug, created, entity_slug) = row
    return {
        "file_id": str(file_id),
        "filename": filename,
        "content_type": content_type,
        "s3_uri": s3_uri,
        "size_bytes": size_bytes,
        "business": business_slug or to_business_slug(entity_slug),
        "created_at": _iso(created),
    }


def record_file(
    *,
    entity_slug: str | None,
    business_slug: str | None,
    filename: str,
    content_type: str | None,
    s3_uri: str,
    size_bytes: int | None,
    created_at: datetime | str | None = None,
) -> str | None:
    """Insert a generated-file row and return its file_id (or None on failure).
    Best-effort: the caller must never let a failure here break file creation.
    `created_at` defaults to now(); the storage backfill passes the S3 object's
    LastModified so old files sort where they belong."""
    try:
        with cursor() as cur:
            owner = _owner(cur)
            ent = _resolve_entity_id(cur, entity_slug)
            cur.execute(
                """INSERT INTO generated_files
                       (user_id, entity_id, business_slug, filename, content_type, s3_uri,
                        size_bytes, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
                   RETURNING file_id;""",
                (owner, ent, business_slug, filename, content_type, s3_uri, size_bytes, created_at),
            )
            return str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("record_file failed: %s", exc)
        return None


def indexed_s3_uris() -> set[str] | None:
    """Every s3_uri already indexed (any owner), or None if the query failed —
    callers must treat None as "unknown" and not backfill on it."""
    try:
        with cursor(commit=False) as cur:
            cur.execute("SELECT s3_uri FROM generated_files;")
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        log.warning("indexed_s3_uris failed: %s", exc)
        return None


def get_file(file_id: str) -> dict[str, Any] | None:
    if not _is_uuid(file_id):
        return None
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_COLS} FROM generated_files f
                    LEFT JOIN entities e ON e.entity_id = f.entity_id
                    WHERE f.file_id = %s;""",
                (file_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_file(%s) failed: %s", file_id, exc)
        return None
    return _row_to_dict(row) if row else None


def list_files(entity_slug: str | None = None) -> list[dict[str, Any]]:
    """The owner's generated files (newest first). Pass a business slug to scope."""
    try:
        with cursor(commit=False) as cur:
            owner = _owner(cur)
            where = ["f.user_id = %s"]
            params: list[Any] = [owner]
            if entity_slug and entity_slug != "all":
                ent = _resolve_entity_id(cur, entity_slug)
                where.append("f.entity_id = %s")
                params.append(ent)
            cur.execute(
                f"""SELECT {_COLS} FROM generated_files f
                    LEFT JOIN entities e ON e.entity_id = f.entity_id
                    WHERE {' AND '.join(where)}
                    ORDER BY f.created_at DESC
                    LIMIT 300;""",
                params,
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_files failed: %s", exc)
        return []
    return [_row_to_dict(r) for r in rows]


def delete_file(file_id: str) -> bool:
    if not _is_uuid(file_id):
        return False
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM generated_files WHERE file_id = %s;", (file_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_file(%s) failed: %s", file_id, exc)
        return False
