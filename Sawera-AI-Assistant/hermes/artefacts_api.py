"""HTTP routes for Artefacts (the Artefacts tab) — a self-contained APIRouter.

Split out of app.py like hermes/jobs_api.py / hermes/reviews_api.py: app.py just
does `from hermes.artefacts_api import router` + `app.include_router(router)`.

An artefact is a persisted, re-openable HTML canvas bound to a live data source.
The `/{id}/view` route is the canvas: it triggers a deterministic re-fetch +
re-render (hermes/artefacts.sync_and_render) — NO LLM call — and returns HTML.
List is scoped by entity_id so one business's tab never shows another's; the
per-artefact routes re-derive the business from the stored business_slug, so a
leaked id can only ever render that artefact's own bound source.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from . import artefacts as artefacts_engine
from .businesses import to_entity_slug
from .db import artefacts as artefacts_db

router = APIRouter(prefix="/api/artefacts", tags=["artefacts"])


def _entity_scope(entity_id: str | None) -> str | None:
    """Map the frontend's business slug (or "all") to an entities.slug filter."""
    if not entity_id or entity_id == "all":
        return None
    # The dropdown sends a businesses.slug (afk/gak/…); the store filters on the
    # entities.slug, so translate. If it's already an entity slug / uuid the
    # store's own _resolve_entity_id handles it, so fall back to the raw value.
    return to_entity_slug(entity_id) or entity_id


@router.get("")
async def list_artefacts(entity_id: str | None = None):
    """List the owner's artefacts, scoped to a business (or all). Meta only."""
    scope = _entity_scope(entity_id)
    items = await run_in_threadpool(artefacts_db.list_artefacts, scope)
    return {"artefacts": items, "entity_id": entity_id}


@router.get("/{artefact_id}")
async def get_artefact_meta(artefact_id: str):
    """One artefact's metadata (no rendered HTML) — for the detail header."""
    art = await run_in_threadpool(artefacts_db.get_artefact, artefact_id)
    if not art:
        return JSONResponse({"error": "Artefact not found."}, status_code=404)
    return {"artefact": art}


@router.get("/{artefact_id}/view", response_class=HTMLResponse)
async def view_artefact(artefact_id: str, refresh: int = 0):
    """THE canvas. Serves the cached HTML instantly when present and refresh=0;
    otherwise re-fetches the live source and re-renders. Always returns HTML —
    a fetch failure renders a friendly error canvas, never a raw 500."""
    if not refresh:
        cached, _ = await run_in_threadpool(artefacts_db.get_cached_html, artefact_id)
        if cached:
            return HTMLResponse(cached)
    html = await run_in_threadpool(artefacts_engine.sync_and_render, artefact_id)
    if html is None:
        return HTMLResponse(
            artefacts_engine.render_error_canvas("Artefact", "This artefact no longer exists."),
            status_code=404,
        )
    return HTMLResponse(html)


@router.post("/{artefact_id}/sync")
async def sync_artefact(artefact_id: str):
    """Force a re-fetch + re-render now. Returns the fresh sync status."""
    html = await run_in_threadpool(artefacts_engine.sync_and_render, artefact_id)
    if html is None:
        return JSONResponse({"error": "Artefact not found."}, status_code=404)
    art = await run_in_threadpool(artefacts_db.get_artefact, artefact_id)
    return {
        "ok": bool(art and not art.get("last_error")),
        "last_synced_at": art.get("last_synced_at") if art else None,
        "last_error": art.get("last_error") if art else None,
    }


@router.delete("/{artefact_id}")
async def delete_artefact(artefact_id: str):
    ok = await run_in_threadpool(artefacts_db.delete_artefact, artefact_id)
    if not ok:
        return JSONResponse({"error": "Artefact not found."}, status_code=404)
    return {"deleted": artefact_id}
