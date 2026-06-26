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
    POST /api/reset              — clear a conversation's history

Interactive docs are auto-generated at /docs (Swagger) and /redoc.

Every agent/voice endpoint accepts an optional `entity_id` so a single backend
can serve many tenants/users with isolated conversation memory and history.
"""
from __future__ import annotations

import base64
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
# Starlette's form parser yields starlette.UploadFile, which is the BASE class of
# fastapi.UploadFile — so isinstance checks on parsed form files must use this.
from starlette.datastructures import UploadFile as FormUploadFile

from hermes.agent import run_turn
from hermes.attachments import store as attachment_store
from hermes.config import Config
from hermes.ingest import ingest_document
from hermes.maton import MatonClient, MatonError
from hermes.memory import store
from hermes.search import semantic_search
from hermes.voice import VoiceError, speakable, synthesize, transcribe

app = FastAPI(title="Hermes AI Agent API", version="0.1.0")

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
    return {"type": "text", "text": f"Attached file '{name}':\n\n{text}"}, "text"


def _build_attachments(files: list[tuple[str, str, bytes]]) -> list[dict]:
    """Turn uploaded files (name, mimetype, raw bytes) into Claude content blocks."""
    return [_block_for(name, mime, raw)[0] for name, mime, raw in files if name]


@app.get("/")
def index(request: Request):
    """Render the web chat UI. Config drives what the sidebar shows."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "apps": Config.apps(),
            "model": Config.MODEL,
            "web_search": Config.WEB_SEARCH,
            "voice_enabled": Config.voice_enabled(),
            "voices": Config.voice_voices(),
            "voice": Config.voice_tts_voice(),
        },
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "problems": Config.validate()}


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
    try:
        result = await run_in_threadpool(run_turn, key, content, voice)
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
    store.record_turn(key, entity_id, session_id, user_text, result)

    result["session_id"] = session_id
    result["entity_id"] = entity_id
    return result


@app.get("/api/conversations")
def list_conversations(entity_id: str | None = None, q: str | None = None):
    """List this entity's past conversations (newest first) for the history
    sidebar. Each item has session_id, title, timestamps, and message_count.

    Pass `q` to search chats by title or message content; matching items also
    carry a `snippet` of the first matching message."""
    return {
        "conversations": store.conversations(entity_id, q),
        "entity_id": entity_id,
        "q": q,
    }


@app.get("/api/conversations/{session_id}")
def get_conversation(session_id: str, entity_id: str | None = None):
    """Replay one conversation: returns its display transcript (user/assistant
    turns with thinking + tool traces) so the UI can re-render it."""
    transcript = store.transcript(_session_key(entity_id, session_id))
    if not transcript:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)
    return {"session_id": session_id, "entity_id": entity_id, "transcript": transcript}


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


@app.post("/api/documents")
async def upload_documents(request: Request):
    """Ingest files into the knowledge base so they're searchable by meaning.

    Each file is stored (S3, if configured), recorded in `documents`, chunked,
    embedded, and upserted into the business's Pinecone namespace.

    Multipart fields: `files` (one or more, required), `entity_id` (required —
    which business the files belong to), `sensitivity_level` (optional, default
    'internal'), `message_id` (optional, link to a chat message)."""
    form = await request.form()
    entity_id = (form.get("entity_id") or "").strip()
    if not entity_id:
        return JSONResponse({"error": "entity_id is required."}, status_code=400)
    sensitivity = (form.get("sensitivity_level") or "internal").strip()
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
                sensitivity_level=sensitivity,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Ingest failed: {exc}"}, status_code=500)
        out.append({"filename": upload.filename, **result})
    return {"documents": out, "entity_id": entity_id}


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
):
    """Speech -> text. The client posts a recorded audio clip; the configured
    provider (OpenAI/Groq Whisper) returns the transcript, which the client then
    sends through /api/chat so the voice agent runs the exact same Claude + Maton
    agent as the typed chat.

    Multipart fields: `audio` (file, required), `entity_id` (optional)."""
    if audio is None:
        return JSONResponse({"error": "No audio uploaded."}, status_code=400)
    raw = await audio.read()
    try:
        text = await run_in_threadpool(transcribe, raw, audio.content_type)
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
        f"(model: {Config.MODEL}, voice: {voice})"
    )
    uvicorn.run("app:app", host="0.0.0.0", port=Config.PORT, reload=True)
