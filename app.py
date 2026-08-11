"""FastAPI entrypoint for the Hermes AI Agent — web chat UI + JSON API.

Serves a single-page web chat at `/` (with a conversation-history sidebar) and
the full JSON API under `/api/*`. The API is self-contained, so you can also
ignore the UI and point any frontend, mobile app, or service at the backend.

Endpoints:
    GET  /                       — web chat UI (conversation history sidebar)
    GET  /api/health             — liveness + config check
    POST /api/chat               — run a turn through the Claude + Maton agent
    GET  /api/conversations      — list past conversations (history sidebar)
    GET  /api/conversations/{id} — replay one conversation's transcript
    DELETE /api/conversations/{id} — delete a conversation from history
    POST /api/attachments        — upload files, get attachment ids for /api/chat
    GET  /api/attachments/{id}   — attachment metadata
    DELETE /api/attachments/{id} — delete an attachment
    POST /api/voice/transcribe   — speech -> text (multipart audio upload)
    POST /api/voice/speak        — text -> speech (returns audio bytes)
    GET  /api/voice/voices       — list available TTS voices + current default
    GET/POST /api/connections    — list / create Maton app connections
    GET/POST /api/settings/model — read/switch the agent's active model (Claude/OpenAI)
    POST /api/reset              — clear a conversation's history
    GET/POST /api/scheduled-tasks           — list / create recurring agent tasks
    GET/PATCH/DELETE /api/scheduled-tasks/{id} — read / edit / delete one task
    POST /api/scheduled-tasks/{id}/run      — run a task now (manual)
    GET  /api/scheduled-tasks/{id}/runs     — a task's run history
    */POST /api/scheduled-tasks/run-due     — cron trigger: fire all due tasks

Interactive docs are auto-generated at /docs (Swagger) and /redoc.

Every agent/voice endpoint accepts an optional `entity_id` so a single backend
can serve many tenants/users with isolated conversation memory and history.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("hermes.app")

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
# Starlette's form parser yields starlette.UploadFile, which is the BASE class of
# fastapi.UploadFile — so isinstance checks on parsed form files must use this.
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import UploadFile as FormUploadFile

from hermes.agent import run_turn, stream_turn
from hermes.attachments import ATTACHMENT_TEXT_PREFIX, store as attachment_store
from hermes.bills import check_paid_bills, extract_bill, fetch_gmail_body, guess_business, scan_drive, scan_gmail
from hermes.businesses import to_business_slug, to_entity_slug
from hermes import calendar as hermes_calendar
from hermes.config import Config
from hermes.fathom import find_recording_link as find_fathom_link
from hermes import storage as s3
from hermes import files_tree
from hermes.db.bills import (
    _BILL_NOT_FOUND,
    cleanup_old_drive_bills,
    compute_insights,
    delete_bill,
    get_bill,
    insert_bill,
    list_bills,
    list_businesses,
    update_bill,
    upsert_business,
)
from hermes.db import documents as docs_repo
from hermes.db import memories as mem_repo
from hermes.db import meetings as meetings_db
from hermes.db import usage as usage_repo
from hermes.ingest import ingest_document, update_document
from hermes.maton import MatonClient, MatonError
from hermes.meeting_copilot import generate_ai_notes, get_guidance, get_guidance_comprehensive, prepare_brief, summarize_meeting
from hermes.memory import store
from hermes.memory_extract import extract_async
from hermes.conversation_write import record_turn_async
from hermes import models as hermes_models
from hermes import guardrails as hermes_guardrails
from hermes import scheduler as hermes_scheduler
from hermes.db import scheduled_tasks as tasks_db
from hermes import job_runner as hermes_jobs
from hermes.jobs_api import router as jobs_router
from hermes.search import semantic_search
from hermes.voice import VoiceError, speakable, synthesize, transcribe

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """Warm the DB connection (DNS + IAM token) in the background at boot, so the
    first real request — e.g. loading the history sidebar when the page opens —
    isn't the slow cold one that could otherwise come back empty."""
    def _go() -> None:
        try:
            store.conversations(None)  # resolves owner + opens a connection
        except Exception:  # noqa: BLE001 — best-effort; never block startup
            pass
        try:
            removed = cleanup_old_drive_bills()
            if removed:
                log.info("Startup: removed %d pre-June-2026 Drive bill stubs", removed)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Warm the World Bank data cache so the agent's first data question
            # hits the cache instead of the 8-25s cold API path mid-conversation.
            from hermes import worldbank
            worldbank.prewarm()
        except Exception:  # noqa: BLE001
            pass
        try:
            # Ensure the once-a-day "attention report" scheduled task exists. Done
            # here (not gated on the in-process poller) so it's present on Vercel
            # too, where the external cron fires it. Idempotent — a no-op if set up.
            from hermes import notify
            notify.ensure_digest_task()
        except Exception:  # noqa: BLE001 — never block startup on the digest setup
            log.warning("Could not ensure the daily attention-report task", exc_info=True)

    threading.Thread(target=_go, daemon=True).start()

    # Fire scheduled tasks automatically on any always-on process (local dev, a
    # VM, Railway/Render) with no external cron needed. Auto-disabled on Vercel,
    # where the vercel.json cron hits /api/scheduled-tasks/run-due instead — so a
    # task created in the UI "just runs" in every deployment, and never twice.
    if Config.scheduler_inprocess():
        try:
            hermes_scheduler.start_background_poller(Config.SCHEDULER_POLL_SECONDS)
        except Exception:  # noqa: BLE001 — never block startup on the poller
            log.warning("Could not start in-process scheduler", exc_info=True)
        # Same deal for code-driven Jobs (the Jobs tab): fire due jobs on any
        # always-on process; on Vercel the vercel.json cron hits /api/jobs/run-due.
        try:
            hermes_jobs.start_background_poller(Config.SCHEDULER_POLL_SECONDS)
        except Exception:  # noqa: BLE001
            log.warning("Could not start in-process jobs poller", exc_info=True)

    yield


app = FastAPI(title="Hermes AI Agent API", version="0.1.0", lifespan=_lifespan)

# Jobs (the Jobs tab) — /api/jobs/* routes live in hermes/jobs_api.py.
app.include_router(jobs_router)
# Review intelligence reports — /api/reviews/* routes live in hermes/reviews_api.py.
from hermes.reviews_api import router as reviews_router
app.include_router(reviews_router)
# Artefacts (the Library tab) — /api/artefacts/* routes live in hermes/artefacts_api.py.
from hermes.artefacts_api import router as artefacts_router
app.include_router(artefacts_router)
# Generated files (the Library tab's Files list) — hermes/generated_files_api.py.
from hermes.generated_files_api import router as generated_files_router
app.include_router(generated_files_router)

# ---- Web UI (single-page chat with conversation history) ------------------
_BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

# Allow a separately-deployed frontend / client to call this API. Lock down the
# origins in production via CORS_ORIGINS (comma-separated) — defaults to "*".
_origins = Config.cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if isinstance(_origins, list) else ["*"],
    allow_credentials=isinstance(_origins, list),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Entity-Id"],
)

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_MAX_CHARS = 200_000
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB upload cap


@app.middleware("http")
async def _limit_upload_size(request: Request, call_next):
    """Reject oversized request bodies before we buffer them into memory."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "Request body too large (max 25 MB)."}, status_code=413)
    return await call_next(request)


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_uuid(value: str) -> bool:
    import uuid as _uuid_mod

    try:
        _uuid_mod.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _session_key(entity_id: str | None, session_id: str) -> str:
    """Namespace a session by entity so tenants never share conversation memory."""
    eid = (entity_id or "").strip()
    return f"{eid}:{session_id}" if eid else session_id


def _block_for(name: str, mime: str, raw: bytes) -> tuple[dict, str]:
    """Turn one uploaded file into a Claude content block + its kind.

    Returns (block, kind) where kind is "document", "image", or "text"."""
    mime = (mime or "").lower()

    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(raw).decode("utf-8"),
            },
            "title": name,
        }
        return block, "document"

    if mime in _IMAGE_TYPES:
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.standard_b64encode(raw).decode("utf-8"),
            },
        }
        return block, "image"

    # Treat anything else as text (csv, json, md, code, ...).
    try:
        text = raw.decode("utf-8", errors="replace")[:_TEXT_MAX_CHARS]
    except Exception:  # noqa: BLE001
        text = "(could not decode file as text)"
    return {"type": "text", "text": f"{ATTACHMENT_TEXT_PREFIX}{name}':\n\n{text}"}, "text"


def _build_attachments(files: list[tuple[str, str, bytes]]) -> list[dict]:
    """Turn uploaded files (name, mimetype, raw bytes) into Claude content blocks."""
    return [_block_for(name, mime, raw)[0] for name, mime, raw in files if name]


def _asset_version() -> int:
    """A version stamp that changes whenever a static asset changes, so browsers
    fetch fresh JS/CSS after a deploy instead of serving a stale cached copy."""
    latest = 0.0
    for name in ("app.js", "style.css", "voice.js", "meeting.js", "bills.js", "business.js", "tasks.js", "context.js", "model.js", "files.js", "jobs.js", "artefacts.js", "library.js"):
        try:
            latest = max(latest, (_BASE_DIR / "static" / name).stat().st_mtime)
        except OSError:
            pass
    return int(latest)


@app.get("/")
def index(request: Request):
    """Render the web chat UI. Config drives what the sidebar shows."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "apps": Config.apps(),
            "model_options": hermes_models.options(),
            "active_model": hermes_models.active_model(),
            "active_effort": Config.EFFORT,
            "web_search": Config.WEB_SEARCH,
            "voice_enabled": Config.voice_enabled(),
            "voices": Config.voice_voices(),
            "voice": Config.voice_tts_voice(),
            "v": _asset_version(),
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "problems": Config.validate()}


@app.get("/api/settings/model")
def get_model_setting():
    """Current agent model + the switchable options, for the UI's model picker."""
    return {"active": hermes_models.active_model(), "options": hermes_models.options()}


class ModelUpdate(BaseModel):
    model: str


@app.post("/api/settings/model")
def set_model_setting(body: ModelUpdate):
    """Switch the agent's active model platform-wide (Claude <-> OpenAI, cheap
    <-> expensive tier). Takes effect on the next turn of every conversation."""
    try:
        hermes_models.set_active_model(body.model)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"active": hermes_models.active_model()}


@app.get("/api/settings/guardrails")
def get_guardrail_settings():
    """Every user-facing guardrail with its current global on/off state, for the
    Settings tab. Data-driven from hermes.guardrails.GUARDRAILS_META."""
    return {"guardrails": hermes_guardrails.list_guardrails()}


class GuardrailUpdate(BaseModel):
    guardrail: str = "confirm_writes"
    enabled: bool
    business: str | None = None


@app.post("/api/settings/guardrails")
def set_guardrail_settings(body: GuardrailUpdate):
    """Turn a guardrail on/off, globally (business=None) or for one business.
    Takes effect on the next turn of every conversation."""
    try:
        hermes_guardrails.set_guardrail_enabled(body.guardrail, body.enabled, body.business)
    except KeyError:
        return JSONResponse(
            {"error": f"Unknown guardrail: {body.guardrail!r}"}, status_code=400
        )
    return {
        "guardrail": body.guardrail,
        "enabled": body.enabled,
        "scope": body.business or "global",
    }


@app.post("/api/admin/migrate")
async def admin_migrate():
    """Apply pending SQL migrations (hermes/db/migrations/*.sql).

    Exists because on some deployments (e.g. serverless platforms like
    Vercel) the database is only reachable from inside the platform's own
    network — the same place this endpoint runs — so this is the working
    path to apply a new migration, not a local `scripts/init_db.py` run from
    somewhere that may have no route to the DB at all. Idempotent: every
    migration uses CREATE TABLE IF NOT EXISTS / DO $$ ... EXCEPTION
    duplicate_object, and applied filenames are tracked in
    schema_migrations so pending work is computed correctly on every call.
    """
    from pathlib import Path

    from hermes.db import cursor

    def _run() -> dict:
        migrations_dir = Path(__file__).resolve().parent / "hermes" / "db" / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))
        if not files:
            return {"error": f"No .sql files found under {migrations_dir}"}
        with cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"
            )
            cur.execute("SELECT filename FROM schema_migrations;")
            done = {row[0] for row in cur.fetchall()}
            applied = []
            for f in files:
                if f.name in done:
                    continue
                cur.execute(f.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s);", (f.name,)
                )
                applied.append(f.name)
        return {"applied": applied, "already_done": sorted(done)}

    try:
        result = await run_in_threadpool(_run)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    if "error" in result:
        return JSONResponse(result, status_code=500)
    return result


@app.post("/api/chat")
async def chat(request: Request):
    """Run one user turn through the Claude + Maton agent.

    Accepts JSON or multipart/form-data (multipart carries file attachments)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/"):
        form = await request.form()
        session_id = form.get("session_id") or str(uuid.uuid4())
        entity_id = form.get("entity_id")
        message = (form.get("message") or "").strip()
        voice = _truthy(form.get("voice"))
        attachment_ids = [a for a in form.getlist("attachment_ids") if isinstance(a, str)]
        effort = form.get("effort")
        files: list[tuple[str, str, bytes]] = []
        for upload in form.getlist("files"):
            if isinstance(upload, FormUploadFile) and upload.filename:
                files.append((upload.filename, upload.content_type or "", await upload.read()))
        attachments = _build_attachments(files)
    else:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — tolerate empty/invalid bodies
            data = {}
        data = data or {}
        session_id = data.get("session_id") or str(uuid.uuid4())
        entity_id = data.get("entity_id")
        message = (data.get("message") or "").strip()
        voice = _truthy(data.get("voice"))
        attachment_ids = data.get("attachment_ids") or []
        effort = data.get("effort")
        attachments = []

    if not message and not attachments and not attachment_ids:
        return JSONResponse(
            {"error": "Provide a message, a file, or an attachment_id."}, status_code=400
        )

    # Pull any previously-uploaded attachments (scoped to this entity) first.
    content: list[dict] = []
    for aid in attachment_ids:
        block = attachment_store.block(str(aid), entity_id)
        if block is None:
            return JSONResponse(
                {"error": f"Unknown attachment_id: {aid}"}, status_code=400
            )
        content.append(block)
    content.extend(attachments)
    if message:
        content.append({"type": "text", "text": message})

    key = _session_key(entity_id, session_id)

    # Business scoping: a conversation's business is locked at creation and
    # never changes mid-thread, even if the dropdown is flipped while it's
    # open — enforced here, not just by frontend convention. `entity_id` from
    # the request is a business slug (e.g. "afk") or the sentinel "all".
    prior_messages = await run_in_threadpool(store.get, key)
    if prior_messages:
        locked = await run_in_threadpool(store.locked_business_slug, key)
        active_business = locked or "all"
    else:
        active_business = (entity_id or "").strip() or "all"

    try:
        result = await run_in_threadpool(
            run_turn, key, content, voice, active_business, None, effort
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the client
        return JSONResponse({"error": f"Agent error: {exc}"}, status_code=500)

    # Record this exchange so the conversation-history sidebar can list/replay it.
    file_names = [name for name, _, _ in files] if content_type.startswith("multipart/") else []
    n_files = len(file_names) + len(attachment_ids)
    user_text = message
    if n_files:
        names = ", ".join(file_names) if file_names else f"{n_files} file(s)"
        note = f"📎 {names}"
        user_text = f"{message}\n{note}" if message else note
    store_entity = None if active_business == "all" else to_entity_slug(active_business)
    store.record_turn(key, store_entity, session_id, user_text, result)
    extract_async(
        session_id=session_id, entity_slug=store_entity,
        user_text=user_text, reply=result.get("reply") or "",
    )
    record_turn_async(
        session_id=session_id, active_business=active_business,
        user_text=user_text, reply=result.get("reply") or "",
    )

    result["session_id"] = session_id
    result["entity_id"] = active_business  # authoritative, locked value — not just an echo
    return result


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    """Same agent turn as /api/chat, streamed as Server-Sent Events so the
    typed chat UI can render the reply as it's generated instead of waiting
    for the whole turn — including every tool round-trip — to finish first.

    Voice mode keeps using plain /api/chat unchanged: it needs the complete
    reply text before it can call text-to-speech, so streaming wouldn't help
    it and this endpoint was kept additive rather than touching that path.

    Same request shape as /api/chat (JSON or multipart). Response is
    text/event-stream: one `data: <json>\\n\\n` line per event —
    {"type": "thinking", "text": "<delta>"} — Claude's REAL extended-thinking
    text, streamed live, only on models/turns where it chose to think out
    loud (Haiku never produces this; Opus/Sonnet only produce it for turns
    complex enough to warrant it — a simple one-tool-call question often has
    none at all, which is expected, not a bug),
    {"type": "text", "text": "<delta>"} as the reply is generated,
    {"type": "tool_call", "tool": "...", "input": {...}, "label": "..."} when a
    tool is invoked — `label` is a short, friendly "what I'm doing right now"
    string (e.g. "📅 Peeking at your calendar…") meant to be shown as-is in the
    UI instead of the raw tool name; `tool_calls` entries in the final `done`
    event below carry the same `label` too, so history replay shows it,
    then a final {"type": "done", "session_id", "entity_id", "reply",
    "thinking", "tool_calls", "stop_reason"} — the same fields /api/chat
    returns as JSON, so the frontend's finishing logic (business lock,
    history refresh) needs no new fields to handle.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/"):
        form = await request.form()
        session_id = form.get("session_id") or str(uuid.uuid4())
        entity_id = form.get("entity_id")
        message = (form.get("message") or "").strip()
        voice = _truthy(form.get("voice"))
        attachment_ids = [a for a in form.getlist("attachment_ids") if isinstance(a, str)]
        effort = form.get("effort")
        files: list[tuple[str, str, bytes]] = []
        for upload in form.getlist("files"):
            if isinstance(upload, FormUploadFile) and upload.filename:
                files.append((upload.filename, upload.content_type or "", await upload.read()))
        attachments = _build_attachments(files)
    else:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 — tolerate empty/invalid bodies
            data = {}
        data = data or {}
        session_id = data.get("session_id") or str(uuid.uuid4())
        entity_id = data.get("entity_id")
        message = (data.get("message") or "").strip()
        voice = _truthy(data.get("voice"))
        attachment_ids = data.get("attachment_ids") or []
        effort = data.get("effort")
        attachments = []
        files = []

    if not message and not attachments and not attachment_ids:
        return JSONResponse(
            {"error": "Provide a message, a file, or an attachment_id."}, status_code=400
        )

    content: list[dict] = []
    for aid in attachment_ids:
        block = attachment_store.block(str(aid), entity_id)
        if block is None:
            return JSONResponse({"error": f"Unknown attachment_id: {aid}"}, status_code=400)
        content.append(block)
    content.extend(attachments)
    if message:
        content.append({"type": "text", "text": message})

    key = _session_key(entity_id, session_id)

    # Same business-locking rule as /api/chat.
    prior_messages = await run_in_threadpool(store.get, key)
    if prior_messages:
        locked = await run_in_threadpool(store.locked_business_slug, key)
        active_business = locked or "all"
    else:
        active_business = (entity_id or "").strip() or "all"

    file_names = [name for name, _, _ in files]
    n_files = len(file_names) + len(attachment_ids)
    user_text = message
    if n_files:
        names = ", ".join(file_names) if file_names else f"{n_files} file(s)"
        note = f"📎 {names}"
        user_text = f"{message}\n{note}" if message else note
    store_entity = None if active_business == "all" else to_entity_slug(active_business)

    async def event_source():
        result: dict = {}
        try:
            async for event in iterate_in_threadpool(
                stream_turn(key, content, voice, active_business, None, effort)
            ):
                if event["type"] == "done":
                    result = event["result"]
                    payload = {
                        "type": "done",
                        "session_id": session_id,
                        "entity_id": active_business,
                        **result,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 — surface a clean error to the client
            yield f"data: {json.dumps({'type': 'error', 'error': f'Agent error: {exc}'})}\n\n"
            return

        # Same history recording as /api/chat, after the stream completes.
        store.record_turn(key, store_entity, session_id, user_text, result)
        extract_async(
            session_id=session_id, entity_slug=store_entity,
            user_text=user_text, reply=result.get("reply") or "",
        )
        record_turn_async(
            session_id=session_id, active_business=active_business,
            user_text=user_text, reply=result.get("reply") or "",
        )

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/api/conversations")
def list_conversations(entity_id: str | None = None, q: str | None = None):
    """List this business's past conversations (newest first) for the history
    sidebar. Each item has session_id, title, timestamps, message_count, and
    `business` (the slug it's locked to, or None for an "All Businesses" chat).

    `entity_id` here is a business slug (e.g. "afk") or the sentinel "all" —
    "all" (or omitted) shows every conversation; a specific slug shows only
    conversations locked to that business.

    Pass `q` to search chats by title or message content; matching items also
    carry a `snippet` of the first matching message."""
    norm = None if not entity_id or entity_id == "all" else to_entity_slug(entity_id)
    return {
        "conversations": store.conversations(norm, q),
        "entity_id": entity_id,
        "q": q,
    }


@app.get("/api/usage")
def usage_report(
    entity_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
):
    """Token-usage / cost rollup for the Usage tab: grand totals plus a
    per-chat-session breakdown. `entity_id` is a business slug (or "all"/omitted
    for every session). `start`/`end` are optional ISO-8601 timestamps that
    bound the report to a date range (half-open: start <= created_at < end)."""
    norm = None if not entity_id or entity_id == "all" else to_entity_slug(entity_id)
    report = usage_repo.session_usage(norm, start or None, end or None)
    return {"entity_id": entity_id, "start": start, "end": end, **report}


@app.get("/api/conversations/{session_id}")
def get_conversation(session_id: str, entity_id: str | None = None):
    """Replay one conversation: returns its display transcript (user/assistant
    turns with thinking + tool traces) so the UI can re-render it, plus the
    business slug it's actually locked to (authoritative — for the frontend to
    reseed its session lock on reopen, regardless of what `entity_id` was
    passed in the query string)."""
    key = _session_key(entity_id, session_id)
    transcript = store.transcript(key)
    if not transcript:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)
    locked = store.locked_business_slug(key)
    return {"session_id": session_id, "entity_id": locked, "transcript": transcript}


@app.delete("/api/conversations/{session_id}")
def delete_conversation(session_id: str, entity_id: str | None = None):
    """Delete a conversation (its agent memory and its history transcript)."""
    store.reset(_session_key(entity_id, session_id))
    return {"status": "deleted", "session_id": session_id}


@app.post("/api/attachments")
async def upload_attachments(request: Request):
    """Upload one or more files and get back stable attachment ids.

    Pass those ids as `attachment_ids` to /api/chat to include the files in a
    turn without re-uploading them. Attachments are scoped to `entity_id`, so
    only the same entity can reference or delete them.

    Multipart fields: `files` (one or more, required), `entity_id` (optional)."""
    form = await request.form()
    entity_id = form.get("entity_id")
    uploads = [
        u for u in form.getlist("files")
        if isinstance(u, FormUploadFile) and u.filename
    ]
    if not uploads:
        return JSONResponse(
            {"error": "No files uploaded. Use the multipart field 'files'."},
            status_code=400,
        )

    out = []
    for upload in uploads:
        raw = await upload.read()
        block, kind = _block_for(upload.filename, upload.content_type or "", raw)
        rec = attachment_store.add(entity_id, upload.filename, kind, len(raw), block)
        out.append(
            {
                "attachment_id": rec["attachment_id"],
                "filename": rec["filename"],
                "type": rec["type"],
                "bytes": rec["bytes"],
            }
        )
    return {"attachments": out, "entity_id": entity_id}


@app.get("/api/attachments/{attachment_id}")
def get_attachment(attachment_id: str, entity_id: str | None = None):
    """Return an attachment's metadata (filename, type, size). Pass the same
    `entity_id` it was uploaded with as a query parameter."""
    meta = attachment_store.meta(attachment_id, entity_id)
    if meta is None:
        return JSONResponse({"error": "Attachment not found."}, status_code=404)
    return meta


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: str, entity_id: str | None = None):
    """Delete an attachment. Pass the same `entity_id` it was uploaded with."""
    if attachment_store.remove(attachment_id, entity_id):
        return {"status": "deleted", "attachment_id": attachment_id}
    return JSONResponse({"error": "Attachment not found."}, status_code=404)


@app.get("/api/media/presign")
def presign_media(uri: str):
    """Return a short-lived HTTPS URL for an s3:// URI produced by a tool call
    (e.g. wati_get_media), so the chat UI can render it inline — both live and
    when a past conversation is reopened (presigned URLs expire, so the raw
    s3:// URI is what's persisted in the transcript, and this is called fresh
    on every render). Only presigns URIs inside our own configured bucket."""
    if not uri.startswith(f"s3://{s3.bucket()}/"):
        return JSONResponse({"error": "Unknown or disallowed URI."}, status_code=400)
    url = s3.presign(uri)
    if not url:
        return JSONResponse({"error": "Could not generate a link."}, status_code=404)
    return {"url": url}


@app.post("/api/documents")
async def upload_documents(request: Request):
    """Ingest files into the knowledge base so they're searchable by meaning.

    Each file is stored (S3, if configured), recorded in `documents`, chunked,
    embedded, and written into the business's document_chunks rows (pgvector).

    Multipart fields: `files` (one or more, required), `entity_id` (required —
    the BUSINESS slug, e.g. "afk", same convention as /api/chat, converted to
    the underlying entities.slug here; or "all" to make it a GLOBAL document
    that surfaces for every business), `sensitivity_level` (optional, default
    'internal'), `source_system` (optional, default 'upload' — the Business
    Context panel tags its uploads 'business_context'), `message_id` (optional,
    link to a chat message)."""
    form = await request.form()
    business_slug = (form.get("entity_id") or "").strip()
    if not business_slug:
        return JSONResponse({"error": "entity_id is required."}, status_code=400)
    entity_id = None if business_slug == "all" else (to_entity_slug(business_slug) or business_slug)
    sensitivity = (form.get("sensitivity_level") or "internal").strip()
    source_system = (form.get("source_system") or "upload").strip()
    message_id = form.get("message_id") or None

    uploads = [
        u for u in form.getlist("files")
        if isinstance(u, FormUploadFile) and u.filename
    ]
    if not uploads:
        return JSONResponse(
            {"error": "No files uploaded. Use the multipart field 'files'."},
            status_code=400,
        )

    out = []
    for upload in uploads:
        raw = await upload.read()
        try:
            result = await run_in_threadpool(
                ingest_document,
                entity_id,
                filename=upload.filename,
                mime=upload.content_type or "",
                raw=raw,
                message_id=message_id,
                source_system=source_system,
                sensitivity_level=sensitivity,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Ingest failed: {exc}"}, status_code=500)
        out.append({"filename": upload.filename, **result})
    return {"documents": out, "entity_id": business_slug}


def _tag_business_slug(rows: list[dict]) -> list[dict]:
    """Add a `business_slug` field (may be None for archived/unmapped
    entities like Comfort Services) alongside the raw entity_slug/entity_name
    every all-businesses row already carries — the frontend groups by this."""
    for row in rows:
        row["business_slug"] = to_business_slug(row.get("entity_slug"))
    return rows


@app.get("/api/documents")
async def list_documents_endpoint(entity_id: str, source_system: str | None = None):
    """List ingested documents (newest first), one business or every business.
    `entity_id` is the BUSINESS slug (same convention as /api/chat), or "all"
    to list across every business (each row tagged with its business). Optional
    `source_system` filter (the Context tab passes 'business_context')."""
    all_mode = entity_id == "all"
    resolved = None if all_mode else (to_entity_slug(entity_id) or entity_id)
    try:
        docs = await run_in_threadpool(docs_repo.list_documents, resolved, source_system)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if all_mode:
        docs = _tag_business_slug(docs)
    return {"entity_id": entity_id, "documents": docs}


@app.get("/api/files/tree")
async def list_files_tree(entity_id: str):
    """List raw S3 objects for the Files explorer, scoped to one business.

    `entity_id` is the BUSINESS slug (same convention as /api/chat), or "all" to
    list across every business (each file tagged with its `business_slug`, or
    null when unattributable). Each item carries `key`, `s3_uri`, `size`,
    `last_modified`, and `business_slug`; the frontend builds the folder tree and
    fetches a download link per file via /api/media/presign. Returns an empty
    list (not an error) when S3 isn't configured, so the UI degrades gracefully.
    """
    if not entity_id:
        return JSONResponse({"error": "entity_id is required."}, status_code=400)
    files = await run_in_threadpool(files_tree.list_business_files, entity_id)
    return {"entity_id": entity_id, "files": files}


@app.delete("/api/documents/{document_id}")
async def delete_document_endpoint(document_id: str):
    """Remove a document (and its chunks/embeddings, via cascade). Best-effort
    S3 cleanup if the original bytes were stored."""
    result = await run_in_threadpool(docs_repo.delete_document, document_id)
    if result is None:
        return JSONResponse({"error": "Document not found."}, status_code=404)
    if result.get("s3_uri"):
        await run_in_threadpool(s3.delete, result["s3_uri"])
    return {"deleted": document_id}


def _resolve_entity_uuid(business_slug: str) -> str | None:
    """BUSINESS slug -> entities.entity_id (uuid), or None if unresolvable."""
    from hermes.db.connection import cursor as _cursor

    entity_slug = to_entity_slug(business_slug) or business_slug
    with _cursor(commit=False) as cur:
        resolved = docs_repo.resolve_entity(cur, entity_slug)
    return resolved[0] if resolved else None


@app.get("/api/memories")
async def list_memories_endpoint(entity_id: str = "all", scope_type: str | None = None):
    """List persistent memories (owner/business/project), newest first — one
    business or every business. `entity_id` is the BUSINESS slug, or "all"
    (default) to list across every business plus owner-scope memories."""
    entity_uuid = None
    if entity_id != "all":
        entity_uuid = await run_in_threadpool(_resolve_entity_uuid, entity_id)
        if entity_uuid is None:
            return JSONResponse({"error": f"Unknown business: {entity_id!r}"}, status_code=400)
    memories = await run_in_threadpool(
        mem_repo.list_memories, entity_id=entity_uuid, scope_type=scope_type,
    )
    memories = _tag_business_slug(memories)
    return {"entity_id": entity_id, "memories": memories}


@app.delete("/api/memories/{memory_id}")
async def delete_memory_endpoint(memory_id: str):
    """Remove one memory."""
    ok = await run_in_threadpool(mem_repo.delete_memory, memory_id)
    if not ok:
        return JSONResponse({"error": "Memory not found."}, status_code=404)
    return {"deleted": memory_id}


@app.get("/api/documents/{document_id}")
async def get_document_endpoint(document_id: str):
    """Full document detail, including body_text — for loading into an editor.
    The list endpoint above deliberately omits body_text to stay light."""
    doc = await run_in_threadpool(docs_repo.get_document, document_id)
    if doc is None:
        return JSONResponse({"error": "Document not found."}, status_code=404)
    return {"document": doc}


class DocumentPatch(BaseModel):
    title: str | None = None
    sensitivity_level: str | None = None
    body_text: str | None = None


@app.patch("/api/documents/{document_id}")
async def update_document_endpoint(document_id: str, body: DocumentPatch):
    """Edit a document's title/sensitivity/content. Editing body_text discards
    and regenerates its chunks/embeddings so search reflects the edit, not the
    stale version."""
    try:
        doc = await run_in_threadpool(
            update_document, document_id,
            title=body.title, sensitivity_level=body.sensitivity_level, body_text=body.body_text,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return {"document": doc}


@app.get("/api/search")
async def search(
    q: str,
    entity_id: str,
    max_sensitivity: str = "internal",
    top_k: int = 8,
):
    """Meaning search within one business. The backend is the guard: it scopes
    the search to `entity_id`'s namespace and filters by sensitivity + region."""
    try:
        results = await run_in_threadpool(
            semantic_search, entity_id, q,
            max_sensitivity=max_sensitivity, top_k=top_k,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Search failed: {exc}"}, status_code=502)
    return {"entity_id": entity_id, "query": q, "results": results}


# ════════════════════════════════════════════════════════════════════════════
#  Meeting Copilot — private, real-time guidance during a live meeting
# ════════════════════════════════════════════════════════════════════════════
# Grounded ONLY in what the ingestion pipeline already read ahead of time
# (pgvector + Postgres) — never scans Gmail/Drive live, never speaks in the
# meeting, never sends anything. One semantic search + one Claude call, so it
# stays fast enough to use while actively talking.

class MeetingCopilotRequest(BaseModel):
    business: str                    # business slug, e.g. "nh" — required, always scoped
    latest_text: str = ""            # what was just said (typed or transcribed)
    notes_so_far: str = ""           # running transcript/notes for extra context
    top_k: int = 6
    session_id: str | None = None    # continue an existing meeting; omit to start one
    comprehensive: bool = False      # if true, use enhanced guidance pulling from all business data
    meeting_title: str = ""          # optional: meeting title for context
    attendees: list[dict] | None = None  # optional: attendee info for context


@app.post("/api/meetings/copilot")
async def meeting_copilot(body: MeetingCopilotRequest):
    """Return short, private, bullet-point guidance for the live meeting, and
    persist the turn (heard text + bullets + sources) to history — same
    pattern as chat's conversation history, so past meetings are retrievable.

    JSON body: business (required, a specific business — "all" isn't allowed
    here), latest_text (what was just said), notes_so_far (running transcript),
    top_k (retrieval depth), session_id (optional — omit to auto-create one on
    the first turn; the response echoes the authoritative id either way).
    Response: {business, session_id, bullets: [...], sources: [...]}.
    """
    business = (body.business or "").strip()
    if not business or business == "all":
        return JSONResponse(
            {"error": "A specific business is required — the copilot can't run in 'All Businesses' mode."},
            status_code=400,
        )
    entity_slug = to_entity_slug(business)
    if not entity_slug:
        return JSONResponse({"error": f"Unknown or unmapped business '{business}'."}, status_code=400)
    if not (body.latest_text or body.notes_so_far):
        return JSONResponse({"error": "Provide latest_text or notes_so_far."}, status_code=400)

    businesses = await run_in_threadpool(list_businesses)
    biz = next((b for b in businesses if b.get("slug") == business), None)
    business_name = (biz or {}).get("name") or business

    # Choose guidance mode: standard or comprehensive (with all business data)
    guidance_fn = get_guidance_comprehensive if body.comprehensive else get_guidance
    guidance_kwargs = {
        "entity_id": entity_slug,
        "business_name": business_name,
        "latest_text": body.latest_text,
        "notes_so_far": body.notes_so_far,
        "session_id": body.session_id,
        "top_k": body.top_k,
    }
    if body.comprehensive:
        guidance_kwargs["meeting_title"] = body.meeting_title
        guidance_kwargs["attendees"] = body.attendees

    try:
        result = await run_in_threadpool(guidance_fn, **guidance_kwargs)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Copilot error: {exc}"}, status_code=500)

    # Persist to history. A DB hiccup here must never fail the live turn —
    # the guidance above already succeeded and is what the user is waiting on.
    session_id = body.session_id
    if not session_id or not _is_uuid(session_id):
        session_id = await run_in_threadpool(
            meetings_db.create_session, entity_slug, (body.latest_text or "").strip()[:60] or None,
        )
    if session_id:
        await run_in_threadpool(
            meetings_db.add_turn,
            session_id,
            heard_text=body.latest_text or None,
            bullets=result.get("bullets") or [],
            sources=result.get("sources") or [],
        )

    return {"business": business, "session_id": session_id, **result}


# ── Meeting history (list / reopen / delete meetings) ──────────────────────

@app.get("/api/meetings/sessions")
async def list_meeting_sessions(business: str | None = None, statuses: str | None = None):
    """List meeting sessions (newest-first) for the history sidebar.

    Pass `business` to scope to one business; omit for every business.
    Pass `statuses` as a comma-separated list (e.g. "scheduled,prepared") to
    scope to a lifecycle stage — the Upcoming view uses this; the plain
    history sidebar omits it and gets everything except deleted.
    """
    entity_slug = to_entity_slug(business) if business else None
    status_list = [s.strip() for s in statuses.split(",")] if statuses else None
    result = await run_in_threadpool(
        meetings_db.sessions, entity_slug, statuses=status_list,
    )
    return {"sessions": result, "business": business}


@app.get("/api/meetings/upcoming")
async def list_upcoming_meetings(business: str, days: int = 14):
    """Upcoming events on `business`'s calendar, each annotated with its
    meeting-copilot status (none / scheduled / prepared / ...) if one exists,
    so the UI can show "Prepare" vs "Prepared — view brief" per event."""
    business = (business or "").strip()
    if not business or business == "all":
        return JSONResponse(
            {"error": "A specific business is required."}, status_code=400,
        )
    try:
        events = await run_in_threadpool(hermes_calendar.list_upcoming, business, days)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    entity_slug = to_entity_slug(business)
    existing = await run_in_threadpool(
        meetings_db.sessions, entity_slug,
        statuses=["scheduled", "prepared", "active", "ended"], newest_first=False,
    )
    by_event_id = {s.get("calendar_event_id"): s for s in existing if s.get("calendar_event_id")}
    for e in events:
        matched = by_event_id.get(e["event_id"])
        e["session_id"] = matched["session_id"] if matched else None
        e["session_status"] = matched["status"] if matched else None
        # Content itself (brief/ai_notes) is intentionally NOT sent here — the
        # Upcoming list only needs status to pick Prepare vs. Open in Live.
        # Full content loads once, when a specific meeting opens in Live view
        # (GET /api/meetings/sessions/{id}) — avoids shipping every event's
        # (potentially large) AI notes on every list load.

    return {"business": business, "events": events}


class MeetingPrepareRequest(BaseModel):
    business: str
    event_id: str
    title: str
    start: str | None = None    # ISO timestamp from the calendar event
    description: str | None = None
    attendees: list[dict] = []


@app.post("/api/meetings/prepare")
async def prepare_meeting(body: MeetingPrepareRequest):
    """Generate an AI pre-meeting brief for a calendar event: background,
    must-ask questions, risks, and links to relevant already-indexed files.
    Creates (or reuses) the session for this event, idempotently."""
    business = (body.business or "").strip()
    if not business or business == "all":
        return JSONResponse({"error": "A specific business is required."}, status_code=400)
    entity_slug = to_entity_slug(business)
    if not entity_slug:
        return JSONResponse({"error": f"Unknown or unmapped business '{business}'."}, status_code=400)

    session_id = await run_in_threadpool(
        meetings_db.upsert_scheduled,
        entity_slug,
        calendar_event_id=body.event_id,
        title=body.title,
        scheduled_at=body.start,
        attendees=body.attendees,
    )
    if not session_id:
        return JSONResponse({"error": "Could not create a session for this meeting."}, status_code=500)

    businesses = await run_in_threadpool(list_businesses)
    biz = next((b for b in businesses if b.get("slug") == business), None)
    business_name = (biz or {}).get("name") or business

    try:
        result = await run_in_threadpool(
            prepare_brief,
            entity_id=entity_slug,
            business_name=business_name,
            meeting_title=body.title,
            attendees=body.attendees,
            description=body.description,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Brief generation failed: {exc}"}, status_code=500)

    await run_in_threadpool(
        meetings_db.save_brief, session_id, brief=result["brief"], sources=result["sources"],
    )
    full = await run_in_threadpool(meetings_db.transcript, session_id)
    return full or {"session_id": session_id, **result}


class MeetingAiNotesRequest(BaseModel):
    business: str
    meeting_title: str


@app.post("/api/meetings/sessions/{session_id}/ai-notes")
async def generate_meeting_ai_notes(session_id: str, body: MeetingAiNotesRequest):
    """Build (or rebuild) detailed pre-meeting notes via the full Hermes agent:
    live Gmail search, Drive file search, read the contents, write clean
    bullet-point notes ending with links to the relevant Drive files.

    Slower than /prepare's brief (real tool calls against Gmail/Drive, not
    just a vector-search lookup) — call this on explicit request, and it's safe to
    call again any time to regenerate with fresh results.
    """
    business = (body.business or "").strip()
    if not business or business == "all":
        return JSONResponse({"error": "A specific business is required."}, status_code=400)

    businesses = await run_in_threadpool(list_businesses)
    biz = next((b for b in businesses if b.get("slug") == business), None)
    business_name = (biz or {}).get("name") or business

    try:
        result = await run_in_threadpool(
            generate_ai_notes,
            business_slug=business,
            business_name=business_name,
            meeting_title=body.meeting_title,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"AI notes generation failed: {exc}"}, status_code=500)

    await run_in_threadpool(meetings_db.save_ai_notes, session_id, result["notes"])
    full = await run_in_threadpool(meetings_db.transcript, session_id)
    return full or {"session_id": session_id, **result}


@app.get("/api/meetings/sessions/{session_id}")
async def get_meeting_session(session_id: str):
    """Full replay of one meeting: its metadata (brief/summary/fathom link/
    attendees) + every turn, in order."""
    result = await run_in_threadpool(meetings_db.transcript, session_id)
    if result is None:
        return JSONResponse({"error": "Meeting not found."}, status_code=404)
    return result


@app.post("/api/meetings/sessions/{session_id}/end")
async def end_meeting_session(session_id: str):
    """End a meeting: generate the AI post-meeting summary from its captured
    turns, and best-effort attach a Fathom recording link if one can be found.
    Both degrade gracefully — ending always succeeds even if either fails."""
    existing = await run_in_threadpool(meetings_db.transcript, session_id)
    if existing is None:
        return JSONResponse({"error": "Meeting not found."}, status_code=404)

    business_slug = existing.get("business")
    business_name = existing.get("title") or "this business"
    if business_slug:
        businesses = await run_in_threadpool(list_businesses)
        biz = next((b for b in businesses if b.get("slug") == business_slug), None)
        if biz and biz.get("name"):
            business_name = biz["name"]

    try:
        summary = await run_in_threadpool(
            summarize_meeting, business_name=business_name, turns=existing.get("turns") or [],
        )
        await run_in_threadpool(meetings_db.save_summary, session_id, summary)
    except Exception as exc:  # noqa: BLE001
        log.warning("end_meeting_session: summarize failed for %s: %s", session_id, exc)
        await run_in_threadpool(meetings_db.end_session, session_id)  # still mark ended
        summary = None

    fathom_url = existing.get("fathom_url")
    if not fathom_url and business_slug:
        try:
            fathom_url = await run_in_threadpool(
                find_fathom_link, business_slug, title_hint=existing.get("title"),
            )
            if fathom_url:
                await run_in_threadpool(meetings_db.save_fathom_url, session_id, fathom_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("end_meeting_session: fathom lookup failed for %s: %s", session_id, exc)

    full = await run_in_threadpool(meetings_db.transcript, session_id)
    return full or {"session_id": session_id, "status": "ended", "summary": summary, "fathom_url": fathom_url}


class FathomLinkRequest(BaseModel):
    url: str


@app.post("/api/meetings/sessions/{session_id}/fathom")
async def set_meeting_fathom_link(session_id: str, body: FathomLinkRequest):
    """Manually attach a Fathom recording link — the fallback when auto-fetch
    (attempted in the /end endpoint) doesn't find one, or to correct it."""
    ok = await run_in_threadpool(meetings_db.save_fathom_url, session_id, body.url)
    if not ok:
        return JSONResponse({"error": "Meeting not found or URL was empty."}, status_code=404)
    return {"session_id": session_id, "fathom_url": body.url.strip()}


@app.delete("/api/meetings/sessions/{session_id}/turns")
async def clear_meeting_turns(session_id: str):
    """Wipe notes/transcript + guidance for a meeting without deleting it —
    for clearing out test content while iterating on a live session."""
    ok = await run_in_threadpool(meetings_db.clear_turns, session_id)
    if not ok:
        return JSONResponse({"error": "Meeting not found."}, status_code=404)
    return {"session_id": session_id, "turns": []}


@app.delete("/api/meetings/sessions/{session_id}")
async def delete_meeting_session(session_id: str):
    ok = await run_in_threadpool(meetings_db.delete_session, session_id)
    if not ok:
        return JSONResponse({"error": "Meeting not found."}, status_code=404)
    return {"session_id": session_id, "status": "deleted"}


class ConnectionRequest(BaseModel):
    app: str | None = None


@app.get("/api/connections")
def list_connections(app: str | None = None, status: str | None = None):
    try:
        return MatonClient().list_connections(app=app, status=status)
    except MatonError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/connections")
def create_connection(body: ConnectionRequest):
    if not body.app:
        return JSONResponse({"error": "Missing 'app'."}, status_code=400)
    try:
        return MatonClient().create_connection(body.app)
    except MatonError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/voice/transcribe")
async def voice_transcribe(
    audio: UploadFile | None = File(default=None),
    entity_id: str | None = Form(default=None),
    context: str | None = Form(default=None),
):
    """Speech -> text. The client posts a recorded audio clip; the configured
    provider (OpenAI/Groq Whisper) returns the transcript, which the client then
    sends through /api/chat so the voice agent runs the exact same Claude + Maton
    agent as the typed chat.

    Multipart fields: `audio` (file, required), `entity_id` (optional),
    `context` (optional — "chat" default, or "meeting" for ambient room audio,
    see hermes.voice._stt_prompt)."""
    if audio is None:
        return JSONResponse({"error": "No audio uploaded."}, status_code=400)
    raw = await audio.read()
    try:
        text = await run_in_threadpool(transcribe, raw, audio.content_type, context or "chat")
    except VoiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return {"text": text, "entity_id": entity_id}


class SpeakRequest(BaseModel):
    text: str | None = None
    voice: str | None = None
    entity_id: str | None = None


@app.post("/api/voice/speak")
async def voice_speak(body: SpeakRequest):
    """Text -> speech. Returns TTS audio for the agent's reply.

    JSON body: `text` (required), `voice` (optional override), `entity_id`
    (optional — echoed back in the X-Entity-Id response header)."""
    text = speakable(body.text or "")
    if not text:
        return JSONResponse({"error": "Nothing to speak."}, status_code=400)
    try:
        audio, mimetype = await run_in_threadpool(synthesize, text, body.voice)
    except VoiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    headers = {"X-Entity-Id": str(body.entity_id)} if body.entity_id else None
    return Response(content=audio, media_type=mimetype, headers=headers)


@app.get("/api/voice/voices")
def voice_voices():
    """List the TTS voices available for the active provider, plus the current
    default. A client uses this to let the user change voice; pass the chosen
    name as `voice` to /api/voice/speak."""
    return {
        "provider": Config.VOICE_PROVIDER,
        "default": Config.voice_tts_voice(),
        "voices": list(Config.voice_voices()),
        "enabled": Config.voice_enabled(),
    }


class ResetRequest(BaseModel):
    session_id: str | None = None
    entity_id: str | None = None


@app.post("/api/reset")
def reset(body: ResetRequest):
    if body.session_id:
        store.reset(_session_key(body.entity_id, body.session_id))
    return {"status": "reset"}


# ════════════════════════════════════════════════════════════════════════════
#  Billing & Invoices API
# ════════════════════════════════════════════════════════════════════════════

# ── Businesses ──────────────────────────────────────────────────────────────

@app.get("/api/businesses")
def api_list_businesses():
    businesses = list_businesses()
    for b in businesses:
        b["connected"] = Config.maton_key_for_slug(b.get("slug")) is not None
    return {"businesses": businesses}


class BusinessBody(BaseModel):
    name: str
    slug: str
    emoji: str = "🏢"
    color: str = "#9aa3b2"
    industry: str | None = None
    business_id: str | None = None


@app.post("/api/businesses")
def api_upsert_business(body: BusinessBody):
    try:
        biz = upsert_business(
            name=body.name, slug=body.slug, emoji=body.emoji,
            color=body.color, industry=body.industry, business_id=body.business_id,
        )
        return {"business": biz}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Bills ───────────────────────────────────────────────────────────────────

@app.get("/api/bills")
def api_list_bills(
    business_id: str | None = None,
    status: str | None = None,
    source: str | None = None,
):
    return {
        "bills": list_bills(business_id=business_id, status=status, source=source),
        "insights": compute_insights(),
    }


@app.post("/api/bills/upload")
async def api_upload_bills(request: Request):
    """Upload bill files → NuExtract → store in DB.

    Multipart fields: `files` (one or more), optional `business_id`.
    Returns a list of newly created bill records.
    """
    form = await request.form()
    manual_biz_id = (form.get("business_id") or "").strip() or None
    businesses = list_businesses()

    uploads = [
        u for u in form.getlist("files")
        if isinstance(u, FormUploadFile) and u.filename
    ]
    if not uploads:
        return JSONResponse({"error": "No files provided."}, status_code=400)

    results = []
    for upload in uploads:
        raw = await upload.read()
        filename = upload.filename or "upload"
        mime = upload.content_type or "application/octet-stream"

        try:
            extracted = await run_in_threadpool(extract_bill, filename, raw, mime)
        except Exception as exc:
            results.append({"filename": filename, "error": str(exc)})
            continue

        if manual_biz_id:
            biz_id, confidence = manual_biz_id, 1.0
        else:
            biz_id, confidence = guess_business(
                extracted["vendor"], extracted.get("category"), businesses
            )

        # Store original file in S3 (no-op if S3 not configured)
        _ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        _s3_key = f"bills/{biz_id or 'unsorted'}/{uuid.uuid4()}.{_ext}"
        _file_url = await run_in_threadpool(s3.put, _s3_key, raw, mime) or None

        bill_id = await run_in_threadpool(
            insert_bill,
            extracted["vendor"],
            extracted.get("amount"),
            extracted.get("invoice_number"),
            extracted.get("due_date"),
            extracted.get("issue_date"),
            extracted.get("currency", "USD"),
            "pending",
            extracted.get("category"),
            biz_id,
            confidence,
            "upload",
            None,
            _file_url,
            extracted.get("raw_extracted"),
            extracted.get("line_items") or [],
        )
        results.append({
            "bill_id": bill_id,
            "filename": filename,
            "vendor": extracted["vendor"],
            "amount": extracted.get("amount"),
            "due_date": extracted.get("due_date"),
            "confidence": confidence,
            "needs_confirmation": confidence < 0.75,
        })

    return {"created": results}


class BillPatch(BaseModel):
    business_id: str | None = None
    status: str | None = None
    vendor: str | None = None
    amount: float | None = None
    invoice_number: str | None = None
    due_date: str | None = None
    category: str | None = None
    notes: str | None = None


@app.patch("/api/bills/{bill_id}")
def api_patch_bill(bill_id: str, body: BillPatch):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if body.business_id:
        updates["confidence"] = 1.0   # user manually assigned = full confidence
    if not update_bill(bill_id, **updates):
        return JSONResponse({"error": "Bill not found."}, status_code=404)
    return {"bill_id": bill_id, "updated": updates}


@app.delete("/api/bills/{bill_id}")
async def api_delete_bill(bill_id: str):
    """Permanently delete a bill record and its line items.

    Also removes the original file from S3 if one was stored.
    Returns 404 if the bill doesn't exist.
    """
    try:
        file_url = await run_in_threadpool(delete_bill, bill_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    if file_url is _BILL_NOT_FOUND:
        return JSONResponse({"error": "Bill not found."}, status_code=404)

    # Best-effort S3 cleanup — don't fail the response if it errors
    if file_url and file_url.startswith("s3://"):
        await run_in_threadpool(s3.delete, file_url)

    return {"deleted": True, "bill_id": bill_id}


@app.get("/api/bills/insights")
def api_bill_insights():
    return compute_insights()


@app.get("/api/bills/{bill_id}/view")
def api_bill_view(bill_id: str):
    """Return a short-lived URL to view the original bill document.

    For S3-stored files returns a presigned GET URL (15 min).
    For Drive files returns the webViewLink directly (opens in browser).
    """
    bill = get_bill(bill_id)
    if not bill:
        return JSONResponse({"error": "Bill not found."}, status_code=404)

    file_url = bill.get("file_url")

    # Gmail bills with no attachment: return a deep link + signal to fetch email body
    if not file_url:
        if bill.get("source") == "gmail" and bill.get("source_ref"):
            msg_id = bill["source_ref"]
            gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
            return {"url": gmail_url, "type": "gmail", "message_id": msg_id}
        return JSONResponse(
            {"error": "No document for this bill.", "source": bill.get("source")},
            status_code=404,
        )

    if file_url.startswith("s3://"):
        url = s3.presign(file_url, expires=900)
        if not url:
            return JSONResponse({"error": "Could not generate document URL."}, status_code=500)
        return {"url": url, "type": "pdf", "expires_in": 900}

    # Drive webViewLink or other web URL
    return {"url": file_url, "type": "web"}


@app.get("/api/bills/{bill_id}/email-body")
async def api_bill_email_body(bill_id: str):
    """Return the decoded HTML or plain-text body of a Gmail bill.

    Fetches the original Gmail message on demand via Maton — nothing stored in DB.
    Returns {"type": "html"|"text", "content": "..."} or 404/502 on failure.
    """
    bill = get_bill(bill_id)
    if not bill:
        return JSONResponse({"error": "Bill not found."}, status_code=404)
    if bill.get("source") != "gmail":
        return JSONResponse({"error": "Not a Gmail bill."}, status_code=400)
    source_ref = bill.get("source_ref")
    if not source_ref:
        return JSONResponse({"error": "No message ID for this bill."}, status_code=404)

    try:
        body = await run_in_threadpool(fetch_gmail_body, source_ref)
    except Exception as exc:
        log.warning("email-body fetch error for bill %s: %s", bill_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    if not body:
        return JSONResponse({"error": "Could not retrieve email body."}, status_code=404)
    return body


# ── Connected-app scan ───────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    business: str | None = None   # business slug, e.g. "afk"; None = legacy default ("per")
    sources: list[str] | None = None
    days: int = 45


@app.post("/api/bills/scan")
async def api_scan_bills(body: ScanRequest):
    """Scan ONE business's own Gmail and/or Google Drive for new bills.

    Body (JSON): {"business": "afk", "sources": ["gmail", "google-drive"], "days": 45}
    days=45 for initial backfill, days=7 for recurring auto-scans.

    Scans exactly one business per call — for "All Businesses", the frontend
    calls this once per connected business itself, since fanning out across
    every business's Gmail + Drive (with attachment downloads) inside a single
    request risks the platform's function timeout.
    """
    sources = body.sources or ["gmail", "google-drive"]
    days = max(1, min(body.days, 365))
    slug = (body.business or "per").strip()

    businesses = await run_in_threadpool(list_businesses)
    business = next((b for b in businesses if b.get("slug") == slug), None)
    if not business:
        return JSONResponse({"error": f"Unknown business '{slug}'."}, status_code=400)
    if not Config.maton_key_for_slug(slug):
        return JSONResponse(
            {"error": f"'{slug}' has no Maton connection configured yet.", "connected": False},
            status_code=400,
        )

    created_gmail, created_drive = [], []

    if "gmail" in sources:
        try:
            created_gmail = await run_in_threadpool(scan_gmail, business, businesses, days)
        except Exception as exc:
            log.warning("Gmail scan error (%s): %s", slug, exc)

    if "google-drive" in sources:
        try:
            created_drive = await run_in_threadpool(scan_drive, business, businesses, days)
        except Exception as exc:
            log.warning("Drive scan error (%s): %s", slug, exc)

    total = created_gmail + created_drive
    return {
        "business": slug,
        "scanned": len(total),
        "gmail": len(created_gmail),
        "google_drive": len(created_drive),
        "bills": total,
    }


# ── Auto-pay detection ───────────────────────────────────────────────────────

@app.post("/api/bills/check-payments")
async def api_check_payments():
    """Search Gmail for payment confirmation emails and auto-mark matching bills paid.

    Returns list of bill_ids that were updated to paid status.
    """
    bills = await run_in_threadpool(list_bills)
    businesses = await run_in_threadpool(list_businesses)
    try:
        paid_ids = await run_in_threadpool(check_paid_bills, bills, businesses)
    except Exception as exc:
        log.warning("check_paid_bills error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    return {"marked_paid": paid_ids, "count": len(paid_ids)}


# ════════════════════════════════════════════════════════════════════════════
#  Scheduled Tasks — recurring, unattended agent runs (Claude-style)
# ════════════════════════════════════════════════════════════════════════════
# Each task is the same Hermes agent turn as chat, but automated: a locked
# business, an allowed-apps toolset, an autonomy level, and a schedule that
# decides when it fires. Firing is pull-based — a cron hits /run-due (below),
# which runs every task whose next_run_at has passed. See hermes/scheduler.py.

def _validate_schedule(schedule: dict) -> str | None:
    """Return an error string if the schedule is unusable, else None."""
    freq = (schedule or {}).get("frequency")
    if freq not in hermes_scheduler.VALID_FREQUENCIES:
        return f"frequency must be one of {sorted(hermes_scheduler.VALID_FREQUENCIES)}."
    if freq == "weekly" and not (schedule.get("days_of_week") or []):
        return "A weekly schedule needs at least one day in days_of_week (0=Mon … 6=Sun)."
    if freq != "hourly" and not (schedule.get("times") or []):
        return "Provide at least one time (HH:MM) for this schedule."
    return None


def _validate_window(window: dict | None) -> str | None:
    """Return an error string if the optional date window is unusable, else None.

    A window turns a task into a one-day-per-run backfill over [start … end]."""
    if not window:
        return None
    from datetime import date as _date
    pace = (window.get("pace") or "").strip()
    if pace not in hermes_scheduler.VALID_WINDOW_PACES:
        return f"window.pace must be one of {sorted(hermes_scheduler.VALID_WINDOW_PACES)}."
    try:
        start = _date.fromisoformat((window.get("start") or "").strip())
    except ValueError:
        return "window.start must be a date (YYYY-MM-DD)."
    end_raw = (window.get("end") or "").strip()
    end = None
    if end_raw:
        try:
            end = _date.fromisoformat(end_raw)
        except ValueError:
            return "window.end must be a date (YYYY-MM-DD) or empty."
    if end and end < start:
        return "window.end must be on or after window.start."
    if pace == "catchup" and not end:
        return "A 'catchup' backfill needs an end date (an open-ended range would fire forever)."
    step = window.get("step_days", 1)
    if not isinstance(step, int) or step < 1:
        return "window.step_days must be an integer ≥ 1."
    return None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_notify_to(raw: str | None) -> tuple[str | None, str | None]:
    """Validate the comma-separated 'notify to' recipient list.

    Returns (normalized_or_None, error). An empty/blank value normalizes to None
    (deliver nothing). Each token must look like an email address."""
    tokens = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if not tokens:
        return None, None
    bad = [t for t in tokens if not _EMAIL_RE.match(t)]
    if bad:
        return None, f"Invalid notify-to email address(es): {', '.join(bad)}."
    return ", ".join(tokens), None


def _enrich_task(task: dict) -> dict:
    """Add the human schedule + window summaries the UI shows on each card."""
    if task:
        task["schedule_summary"] = hermes_scheduler.describe_schedule(
            task.get("schedule") or {}, task.get("timezone")
        )
        task["window_summary"] = hermes_scheduler.describe_window(task)
    return task


def _enrich_run(run: dict) -> dict:
    """Attach a `files` list to a run: any files it created (via `create_file`),
    with a FRESH presigned download link. The run's tool trace stores each file's
    stable `s3_uri` (the presigned url created at run time has since expired), so
    we re-presign here every time the run is viewed."""
    if not run:
        return run
    files = []
    for call in (run.get("tool_calls") or []):
        f = call.get("file") if isinstance(call, dict) else None
        if not f or not f.get("s3_uri"):
            continue
        url = None
        try:
            url = s3.presign(f["s3_uri"], expires=24 * 3600)
        except Exception:  # noqa: BLE001 — a bad/expired uri shouldn't break the view
            url = None
        files.append({
            "filename": f.get("filename"),
            "content_type": f.get("content_type"),
            "size_bytes": f.get("size_bytes"),
            "download_url": url,
        })
    run["files"] = files
    return run


class ScheduleBody(BaseModel):
    frequency: str = "daily"           # hourly | daily | weekdays | weekly
    times: list[str] = []              # ["07:00", "13:00", ...] wall-clock
    days_of_week: list[int] = []       # 0=Mon … 6=Sun, for weekly


class WindowBody(BaseModel):
    """Optional date window: process [start … end] ONE day per run, advancing a
    cursor each fire. `pace`='catchup' drains a past range as fast as the poller
    ticks; 'scheduled' processes one day per normal scheduled fire."""
    pace: str                          # catchup | scheduled
    start: str                         # 'YYYY-MM-DD' — first day (inclusive)
    end: str | None = None             # 'YYYY-MM-DD' — last day; None = open-ended
    step_days: int = 1                 # days to advance per run


class TaskCreateBody(BaseModel):
    name: str
    prompt: str
    business: str | None = None        # business slug, "all", or None == unscoped
    allowed_apps: list[str] = []       # Maton app slugs (+ "web"); [] == all apps
    autonomy: str = "readonly"         # readonly | draft | full
    schedule: ScheduleBody
    timezone: str | None = None
    enabled: bool = True
    window: WindowBody | None = None   # optional backfill window
    notify_to: str | None = None       # comma-separated email(s) to deliver results to


@app.get("/api/scheduled-tasks")
async def list_scheduled_tasks(entity_id: str | None = None):
    """List the owner's scheduled tasks (newest-updated first). `entity_id` is a
    business slug to scope to one business, or "all"/omitted for every task."""
    # `entity_id` arrives as a business slug (afk, gak, …), matching the chat
    # convention — but the store filters on the entity slug (gak_properties, …).
    # Convert it the same way create does, or the filter matches nothing.
    # `to_entity_slug(...) or entity_id` also passes an entity slug/uuid through
    # untouched; "all"/None mean "every task".
    scope = to_entity_slug(entity_id) or entity_id if entity_id and entity_id != "all" else entity_id
    tasks = await run_in_threadpool(tasks_db.list_tasks, scope)
    return {"tasks": [_enrich_task(t) for t in tasks], "entity_id": entity_id}


@app.post("/api/scheduled-tasks")
async def create_scheduled_task(body: TaskCreateBody):
    """Create a scheduled task. Resolves the business scope, validates the
    schedule + autonomy, computes the first next_run_at, and stores it."""
    name = (body.name or "").strip()
    prompt = (body.prompt or "").strip()
    if not name:
        return JSONResponse({"error": "A task name is required."}, status_code=400)
    if not prompt:
        return JSONResponse({"error": "A task prompt is required."}, status_code=400)
    if body.autonomy not in hermes_scheduler.VALID_AUTONOMY:
        return JSONResponse(
            {"error": f"autonomy must be one of {sorted(hermes_scheduler.VALID_AUTONOMY)}."},
            status_code=400,
        )

    schedule = body.schedule.model_dump()
    if err := _validate_schedule(schedule):
        return JSONResponse({"error": err}, status_code=400)

    window = body.window.model_dump() if body.window else None
    if err := _validate_window(window):
        return JSONResponse({"error": err}, status_code=400)

    notify_to, err = _normalize_notify_to(body.notify_to)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    business = (body.business or "").strip()
    entity_slug = None
    if business and business != "all":
        entity_slug = to_entity_slug(business)
        if not entity_slug:
            return JSONResponse({"error": f"Unknown or unmapped business '{business}'."}, status_code=400)

    tz = (body.timezone or Config.SCHEDULER_DEFAULT_TZ).strip() or Config.SCHEDULER_DEFAULT_TZ
    win_pace = window["pace"] if window else None
    next_run = hermes_scheduler.initial_next_run(schedule, tz, body.enabled, win_pace)

    task_id = await run_in_threadpool(
        tasks_db.create_task,
        entity_slug=entity_slug,
        name=name,
        prompt=prompt,
        schedule=schedule,
        timezone_name=tz,
        allowed_apps=body.allowed_apps or [],
        autonomy=body.autonomy,
        enabled=body.enabled,
        next_run_at=next_run,
        window_start=(window["start"] if window else None),
        window_end=(window["end"] if window else None),
        window_step=(window["step_days"] if window else 1),
        window_pace=win_pace,
        notify_to=notify_to,
    )
    if not task_id:
        return JSONResponse({"error": "Could not create the task."}, status_code=500)
    task = await run_in_threadpool(tasks_db.get_task, task_id)
    return {"task": _enrich_task(task)}


# NOTE: this route MUST be declared before the "/{task_id}" routes below —
# FastAPI matches in definition order, and a parameterized "/{task_id}" would
# otherwise capture "run-due" as a task id (a GET would 404 as "task not found",
# silently breaking the Vercel Cron path, which fires run-due with a GET).
@app.api_route("/api/scheduled-tasks/run-due", methods=["GET", "POST"])
async def run_due_scheduled_tasks(request: Request, limit: int = 25):
    """Fire every task whose next_run_at has passed. This is the endpoint a cron
    hits on an interval — Vercel Cron (which sends a GET), cron-job.org, a GitHub
    Action, or any external scheduler.

    Auth: when SCHEDULER_SECRET (or Vercel's own CRON_SECRET) is set, the caller
    must present it as `Authorization: Bearer <secret>` or `?token=<secret>`.
    Vercel Cron sends `Authorization: Bearer $CRON_SECRET` automatically. When no
    secret is configured the endpoint is open (local dev) — set one in production.
    """
    secrets = {s for s in (Config.SCHEDULER_SECRET, os.getenv("CRON_SECRET", "").strip()) if s}
    if secrets:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = request.query_params.get("token") or bearer
        if token not in secrets:
            return JSONResponse({"error": "Unauthorized."}, status_code=401)
    result = await run_in_threadpool(hermes_scheduler.run_due, max(1, min(limit, 100)))
    return result


@app.get("/api/scheduled-tasks/{task_id}")
async def get_scheduled_task(task_id: str):
    """One task plus its recent run history (for the detail panel)."""
    task = await run_in_threadpool(tasks_db.get_task, task_id)
    if not task:
        return JSONResponse({"error": "Task not found."}, status_code=404)
    runs = await run_in_threadpool(tasks_db.list_runs, task_id, 20)
    return {"task": _enrich_task(task), "runs": [_enrich_run(r) for r in runs]}


class TaskPatchBody(BaseModel):
    name: str | None = None
    prompt: str | None = None
    business: str | None = None
    allowed_apps: list[str] | None = None
    autonomy: str | None = None
    schedule: ScheduleBody | None = None
    timezone: str | None = None
    enabled: bool | None = None
    # Send `window: {...}` to set/replace the backfill window (restarts its
    # cursor); send `window: null` to clear it back to an ordinary task.
    window: WindowBody | None = None
    # Send `notify_to: "a@x.com, b@y.com"` to set recipients; "" clears delivery.
    notify_to: str | None = None


@app.patch("/api/scheduled-tasks/{task_id}")
async def update_scheduled_task(task_id: str, body: TaskPatchBody):
    """Edit a task. Only the fields sent are changed. Any change to the schedule,
    timezone, or enabled flag recomputes next_run_at."""
    existing = await run_in_threadpool(tasks_db.get_task, task_id)
    if not existing:
        return JSONResponse({"error": "Task not found."}, status_code=404)

    sent = body.model_dump(exclude_unset=True)
    kwargs: dict = {}

    if "name" in sent:
        if not (body.name or "").strip():
            return JSONResponse({"error": "A task name is required."}, status_code=400)
        kwargs["name"] = body.name.strip()
    if "prompt" in sent:
        if not (body.prompt or "").strip():
            return JSONResponse({"error": "A task prompt is required."}, status_code=400)
        kwargs["prompt"] = body.prompt.strip()
    if "autonomy" in sent:
        if body.autonomy not in hermes_scheduler.VALID_AUTONOMY:
            return JSONResponse({"error": "Invalid autonomy."}, status_code=400)
        kwargs["autonomy"] = body.autonomy
    if "allowed_apps" in sent:
        kwargs["allowed_apps"] = body.allowed_apps or []
    if "schedule" in sent and body.schedule is not None:
        sched = body.schedule.model_dump()
        if err := _validate_schedule(sched):
            return JSONResponse({"error": err}, status_code=400)
        kwargs["schedule"] = sched
    if "timezone" in sent and body.timezone:
        kwargs["timezone_name"] = body.timezone.strip()
    if "enabled" in sent:
        kwargs["enabled"] = body.enabled
    if "notify_to" in sent:
        notify_to, err = _normalize_notify_to(body.notify_to)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        kwargs["notify_to"] = notify_to or ""   # "" clears delivery to NULL
        kwargs["notify_to_set"] = True
    if "window" in sent:
        # window: {...} sets/replaces (restarts cursor); window: null clears it.
        window = body.window.model_dump() if body.window else None
        if err := _validate_window(window):
            return JSONResponse({"error": err}, status_code=400)
        kwargs["window_set"] = True
        kwargs["window_pace"] = window["pace"] if window else None
        kwargs["window_start"] = window["start"] if window else None
        kwargs["window_end"] = window["end"] if window else None
        kwargs["window_step"] = window["step_days"] if window else 1
    if "business" in sent:
        business = (body.business or "").strip()
        if not business or business == "all":
            kwargs["entity_slug"] = None
        else:
            resolved = to_entity_slug(business)
            if not resolved:
                return JSONResponse({"error": f"Unknown business '{business}'."}, status_code=400)
            kwargs["entity_slug"] = resolved
        kwargs["entity_slug_set"] = True

    # Recompute next_run_at whenever timing-relevant fields change (schedule,
    # timezone, enabled, or the backfill window). A newly-set 'catchup' window
    # fires immediately; clearing/replacing the window re-arms from the schedule.
    schedule = kwargs.get("schedule", existing.get("schedule") or {})
    tz = kwargs.get("timezone_name", existing.get("timezone"))
    enabled = kwargs.get("enabled", existing.get("enabled"))
    win_pace = kwargs.get("window_pace", existing.get("window_pace")) \
        if ("window_set" in kwargs or existing.get("window_pace")) else None
    if {"schedule", "timezone_name", "enabled", "window_set"} & kwargs.keys():
        kwargs["next_run_at"] = hermes_scheduler.initial_next_run(schedule, tz, enabled, win_pace)
        kwargs["next_run_set"] = True

    ok = await run_in_threadpool(lambda: tasks_db.update_task(task_id, **kwargs))
    if not ok:
        return JSONResponse({"error": "Nothing to update or task not found."}, status_code=400)
    task = await run_in_threadpool(tasks_db.get_task, task_id)
    return {"task": _enrich_task(task)}


@app.delete("/api/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: str):
    ok = await run_in_threadpool(tasks_db.delete_task, task_id)
    if not ok:
        return JSONResponse({"error": "Task not found."}, status_code=404)
    return {"deleted": task_id}


@app.get("/api/scheduled-tasks/{task_id}/runs")
async def list_scheduled_task_runs(task_id: str, limit: int = 20):
    runs = await run_in_threadpool(tasks_db.list_runs, task_id, max(1, min(limit, 100)))
    return {"task_id": task_id, "runs": [_enrich_run(r) for r in runs]}


@app.post("/api/scheduled-tasks/{task_id}/run")
async def run_scheduled_task_now(task_id: str):
    """Run a task immediately (manual trigger), without disturbing its next
    scheduled fire. Blocks until the agent run completes, then returns the run
    summary — the UI refreshes the run history from it."""
    task = await run_in_threadpool(tasks_db.get_task, task_id)
    if not task:
        return JSONResponse({"error": "Task not found."}, status_code=404)
    try:
        summary = await run_in_threadpool(
            hermes_scheduler.execute_and_record, task, "manual"
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Run failed: {exc}"}, status_code=500)
    return summary


if __name__ == "__main__":
    import uvicorn

    problems = Config.validate()
    if problems:
        print("[Hermes] Configuration warnings:")
        for p in problems:
            print(f"  - {p}")
    voice = "on" if Config.voice_enabled() else "off (set OPENAI_API_KEY)"
    print(
        f"[Hermes] API backend starting on http://127.0.0.1:{Config.PORT}  "
        f"(model: {hermes_models.active_model()}, voice: {voice})"
    )
    uvicorn.run("app:app", host="0.0.0.0", port=Config.PORT, reload=True)
