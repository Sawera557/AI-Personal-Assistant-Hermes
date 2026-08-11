"""HTTP routes for generated files (the Library tab's Files sub-list).

Files the agent makes with create_file are indexed in Postgres (see
hermes/db/generated_files.py); the durable pointer is the s3_uri. This router
lists them and, on demand, mints a FRESH presigned download link from that uri
(so a file stays reachable long after the original 24h link expired) via a 302
redirect. Mirrors hermes/reviews_api.py / hermes/jobs_api.py — no import back
into app.py, to avoid a circular import.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from . import storage
from .businesses import to_entity_slug
from .db import generated_files as gen_files_db

router = APIRouter(prefix="/api/generated-files", tags=["generated-files"])

# Fresh link lifetime when re-presigning on demand.
_LINK_EXPIRES = 6 * 60 * 60


def _entity_scope(entity_id: str | None) -> str | None:
    if not entity_id or entity_id == "all":
        return None
    return to_entity_slug(entity_id) or entity_id


@router.get("")
async def list_generated_files(entity_id: str | None = None):
    """List the owner's generated files, scoped to a business (or all)."""
    scope = _entity_scope(entity_id)
    files = await run_in_threadpool(gen_files_db.list_files, scope)
    return {"files": files, "entity_id": entity_id}


@router.get("/{file_id}/download")
async def download_generated_file(file_id: str):
    """Redirect to a freshly-presigned S3 URL for the file (link never stale)."""
    rec = await run_in_threadpool(gen_files_db.get_file, file_id)
    if not rec:
        return JSONResponse({"error": "File not found."}, status_code=404)
    url = await run_in_threadpool(storage.presign, rec["s3_uri"], _LINK_EXPIRES)
    if not url:
        return JSONResponse(
            {"error": "Could not generate a download link (S3 not configured?)."},
            status_code=502,
        )
    return RedirectResponse(url, status_code=302)


@router.delete("/{file_id}")
async def delete_generated_file(file_id: str):
    """Remove the file: delete the S3 object (best-effort) then the index row."""
    rec = await run_in_threadpool(gen_files_db.get_file, file_id)
    if not rec:
        return JSONResponse({"error": "File not found."}, status_code=404)
    try:
        await run_in_threadpool(storage.delete, rec["s3_uri"])
    except Exception:  # noqa: BLE001 — orphaned S3 object is acceptable
        pass
    ok = await run_in_threadpool(gen_files_db.delete_file, file_id)
    if not ok:
        return JSONResponse({"error": "File not found."}, status_code=404)
    return {"deleted": file_id}
