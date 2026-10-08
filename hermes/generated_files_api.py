"""HTTP routes for generated files (the Library tab's Files sub-list).

Files the agent makes with create_file are indexed in Postgres (see
hermes/db/generated_files.py); the durable pointer is the s3_uri. This router
lists them and, on demand, mints a FRESH presigned download link from that uri
(so a file stays reachable long after the original 24h link expired) via a 302
redirect. Mirrors hermes/reviews_api.py / hermes/jobs_api.py — no import back
into app.py, to avoid a circular import.
"""
from __future__ import annotations

import logging
import mimetypes
import threading

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from . import storage
from .businesses import to_entity_slug
from .db import generated_files as gen_files_db

log = logging.getLogger("hermes.generated_files_api")

router = APIRouter(prefix="/api/generated-files", tags=["generated-files"])

# Fresh link lifetime when re-presigning on demand.
_LINK_EXPIRES = 6 * 60 * 60


def _entity_scope(entity_id: str | None) -> str | None:
    if not entity_id or entity_id == "all":
        return None
    return to_entity_slug(entity_id) or entity_id


# Once-per-process S3 -> index reconciliation (see _reconcile_with_storage).
_reconcile_lock = threading.Lock()
_reconciled = False


def _reconcile_with_storage() -> int:
    """Index every create_file output on S3 that has no generated_files row.

    The index only started with migration 0020 (2026-08-14), and create_file
    records rows best-effort, so earlier files — and any later write whose
    insert failed — exist on S3 but never appeared in the Library. Keys are
    `generated/{business|general}/{hex}/{filename}`. Runs once per process;
    retried next request if the index couldn't be read. Returns rows added.
    """
    global _reconciled
    with _reconcile_lock:
        if _reconciled:
            return 0
        known = gen_files_db.indexed_s3_uris()
        if known is None:
            return 0
        try:
            objects = storage.list_objects(storage.PUBLIC_PREFIX)
        except Exception as exc:  # noqa: BLE001 — listing is best-effort
            log.warning("generated-files reconcile: S3 listing failed: %s", exc)
            return 0
        known_keys = {storage.key_of(u) for u in known}
        added = 0
        for obj in objects:
            key = obj["key"]
            parts = key.split("/", 3)
            if key in known_keys or len(parts) != 4 or not parts[3]:
                continue
            scope, filename = parts[1], parts[3]
            business_slug = scope if scope != "general" else None
            if gen_files_db.record_file(
                entity_slug=to_entity_slug(business_slug) if business_slug else None,
                business_slug=business_slug, filename=filename,
                content_type=mimetypes.guess_type(filename)[0],
                s3_uri=f"s3://{storage.bucket()}/{key}", size_bytes=obj.get("size"),
                created_at=obj.get("last_modified"),
            ):
                added += 1
        if added:
            log.info("generated-files reconcile: indexed %d untracked S3 file(s)", added)
        _reconciled = True
        return added


@router.get("")
async def list_generated_files(entity_id: str | None = None):
    """List the owner's generated files, scoped to a business (or all).

    Each file carries `public_url` — a permanent, no-login link — when public
    serving is configured, so files made before that was turned on are shareable
    too. It's None otherwise; the UI should fall back to the download route.
    """
    scope = _entity_scope(entity_id)
    if not _reconciled:
        await run_in_threadpool(_reconcile_with_storage)
    files = await run_in_threadpool(gen_files_db.list_files, scope)
    for f in files:
        f["public_url"] = storage.public_url(f.get("s3_uri") or "")
    return {"files": files, "entity_id": entity_id}


@router.get("/{file_id}/download")
async def download_generated_file(file_id: str):
    """Redirect to the file: its permanent public URL if public serving is on,
    otherwise a freshly-presigned S3 URL (so the link is never stale either way)."""
    rec = await run_in_threadpool(gen_files_db.get_file, file_id)
    if not rec:
        return JSONResponse({"error": "File not found."}, status_code=404)
    url = storage.public_url(rec["s3_uri"])
    if not url:
        url = await run_in_threadpool(storage.presign, rec["s3_uri"], _LINK_EXPIRES)
    if not url:
        return JSONResponse(
            {"error": "Could not generate a download link (S3 not configured?)."},
            status_code=502,
        )
    return RedirectResponse(url, status_code=302)


@router.delete("/{file_id}")
async def delete_generated_file(file_id: str):
    """Remove the file: delete the S3 object, then the index row. The row is
    kept if the S3 delete fails — otherwise the storage reconcile would quietly
    re-index the surviving object and the file would reappear."""
    rec = await run_in_threadpool(gen_files_db.get_file, file_id)
    if not rec:
        return JSONResponse({"error": "File not found."}, status_code=404)
    if not await run_in_threadpool(storage.delete, rec["s3_uri"]):
        return JSONResponse({"error": "Could not delete the stored file; try again."}, status_code=502)
    ok = await run_in_threadpool(gen_files_db.delete_file, file_id)
    if not ok:
        return JSONResponse({"error": "File not found."}, status_code=404)
    return {"deleted": file_id}
