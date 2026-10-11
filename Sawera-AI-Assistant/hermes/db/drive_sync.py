"""Postgres-backed sync ledger for the Drive → S3 sync Job (hermes/drive_sync.py).

drive_sync_files — one row per (business_slug, Drive file). The runner checks
get_synced() before copying a file and calls upsert_synced() after, so a re-run
never duplicates a file or files it into two folders. See migration 0019.

Pure storage, house style: raw SQL over the pooled cursor(), tuples mapped to
dicts by hand, jsonb written with json.dumps, never raises (logs and degrades).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.drive_sync")

_COLS = (
    "id, business_slug, drive_file_id, drive_name, mime_type, "
    "drive_modified_time, drive_md5, category, s3_key, s3_uri, size_bytes, "
    "exported_pdf, status, error, metadata, synced_at, updated_at"
)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _row_to_dict(row: tuple) -> dict[str, Any]:
    (id_, business_slug, drive_file_id, drive_name, mime_type,
     drive_modified_time, drive_md5, category, s3_key, s3_uri, size_bytes,
     exported_pdf, status, error, metadata, synced_at, updated_at) = row
    return {
        "id": str(id_),
        "business_slug": business_slug,
        "drive_file_id": drive_file_id,
        "drive_name": drive_name,
        "mime_type": mime_type,
        "drive_modified_time": _iso(drive_modified_time),
        "drive_md5": drive_md5,
        "category": category,
        "s3_key": s3_key,
        "s3_uri": s3_uri,
        "size_bytes": size_bytes,
        "exported_pdf": bool(exported_pdf),
        "status": status,
        "error": error,
        "metadata": metadata or {},
        "synced_at": _iso(synced_at),
        "updated_at": _iso(updated_at),
    }


def get_synced(business_slug: str, drive_file_id: str) -> dict[str, Any] | None:
    """The ledger row for one Drive file in one business, or None if never synced."""
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {_COLS} FROM drive_sync_files
                    WHERE business_slug = %s AND drive_file_id = %s;""",
                (business_slug, drive_file_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_synced(%s, %s) failed: %s", business_slug, drive_file_id, exc)
        return None
    return _row_to_dict(row) if row else None


def upsert_synced(
    *,
    business_slug: str,
    drive_file_id: str,
    drive_name: str | None = None,
    mime_type: str | None = None,
    drive_modified_time: str | None = None,
    drive_md5: str | None = None,
    category: str | None = None,
    s3_key: str | None = None,
    s3_uri: str | None = None,
    size_bytes: int | None = None,
    exported_pdf: bool = False,
    status: str = "synced",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Insert or update the ledger row for a Drive file (the single write path).

    Targets the ux_drive_sync_file_ident unique index so re-syncing a file updates
    its row in place (and stamps updated_at) rather than creating a duplicate."""
    try:
        with cursor() as cur:
            cur.execute(
                """INSERT INTO drive_sync_files
                       (business_slug, drive_file_id, drive_name, mime_type,
                        drive_modified_time, drive_md5, category, s3_key, s3_uri,
                        size_bytes, exported_pdf, status, error, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (business_slug, drive_file_id) DO UPDATE SET
                       drive_name          = EXCLUDED.drive_name,
                       mime_type           = EXCLUDED.mime_type,
                       drive_modified_time = EXCLUDED.drive_modified_time,
                       drive_md5           = EXCLUDED.drive_md5,
                       category            = EXCLUDED.category,
                       s3_key              = EXCLUDED.s3_key,
                       s3_uri              = EXCLUDED.s3_uri,
                       size_bytes          = EXCLUDED.size_bytes,
                       exported_pdf        = EXCLUDED.exported_pdf,
                       status              = EXCLUDED.status,
                       error               = EXCLUDED.error,
                       metadata            = EXCLUDED.metadata,
                       updated_at          = now();""",
                (
                    business_slug, drive_file_id, drive_name, mime_type,
                    drive_modified_time, drive_md5, category, s3_key, s3_uri,
                    size_bytes, exported_pdf, status, error,
                    json.dumps(metadata or {}),
                ),
            )
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("upsert_synced(%s, %s) failed: %s", business_slug, drive_file_id, exc)
        return False


def synced_map(business_slug: str) -> dict[str, dict[str, Any]]:
    """All ledger rows for a business as
    {drive_file_id: {drive_modified_time, drive_md5, status, s3_key}}.

    One query the runner preloads so its per-file change check (and its S3-presence
    check, which needs the stored s3_key) is an in-memory lookup, not a DB round-trip
    for every file scanned in Drive."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                """SELECT drive_file_id, drive_modified_time, drive_md5, status, s3_key
                   FROM drive_sync_files WHERE business_slug = %s;""",
                (business_slug,),
            )
            for drive_file_id, modified_time, md5, status, s3_key in cur.fetchall():
                out[drive_file_id] = {
                    "drive_modified_time": _iso(modified_time),
                    "drive_md5": md5,
                    "status": status,
                    "s3_key": s3_key,
                }
    except Exception as exc:  # noqa: BLE001
        log.warning("synced_map(%s) failed: %s", business_slug, exc)
    return out


def recent(business_slug: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Recently-synced files (newest first), optionally scoped to one business."""
    try:
        with cursor(commit=False) as cur:
            if business_slug:
                cur.execute(
                    f"""SELECT {_COLS} FROM drive_sync_files
                        WHERE business_slug = %s
                        ORDER BY synced_at DESC LIMIT %s;""",
                    (business_slug, limit),
                )
            else:
                cur.execute(
                    f"""SELECT {_COLS} FROM drive_sync_files
                        ORDER BY synced_at DESC LIMIT %s;""",
                    (limit,),
                )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("recent(%s) failed: %s", business_slug, exc)
        return []
    return [_row_to_dict(r) for r in rows]
