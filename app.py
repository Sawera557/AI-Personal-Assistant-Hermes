"""FastAPI entrypoint for the Hermes AI Agent — JSON API backend (no UI).

This is the headless, deploy-anywhere API for Hermes. There is no web UI here:
every route returns JSON (or audio bytes), so you can host this backend on its
own and point any frontend, mobile app, or service at it.

Endpoints:
    GET  /api/health            — liveness + config check
    POST /api/chat              — run a turn through the Claude + Maton agent
    POST /api/voice/transcribe  — speech -> text (multipart audio upload)
    POST /api/voice/speak       — text -> speech (returns audio bytes)
    GET  /api/voice/voices      — list available TTS voices + current default
    GET/POST /api/connections   — list / create Maton app connections
    POST /api/reset             — clear a conversation's history

Interactive docs are auto-generated at /docs (Swagger) and /redoc.

Every agent/voice endpoint accepts an optional `entity_id` so a single backend
can serve many tenants/users with isolated conversation memory.
"""
from __future__ import annotations

import base64
import uuid

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from hermes.agent import run_turn
from hermes.config import Config
from hermes.maton import MatonClient, MatonError
from hermes.memory import store
from hermes.voice import VoiceError, speakable, synthesize, transcribe

app = FastAPI(title="Hermes AI Agent API", version="0.1.0")

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


def _build_attachments(files: list[tuple[str, str, bytes]]) -> list[dict]:
    """Turn uploaded files (name, mimetype, raw bytes) into Claude content blocks."""
    blocks: list[dict] = []
    for name, mime, raw in files:
        if not name:
            continue
        mime = (mime or "").lower()

        if mime == "application/pdf" or name.lower().endswith(".pdf"):
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(raw).decode("utf-8"),
                    },
                    "title": name,
                }
            )
        elif mime in _IMAGE_TYPES:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.standard_b64encode(raw).decode("utf-8"),
                    },
                }
            )
        else:
            # Treat anything else as text (csv, json, md, code, ...).
            try:
                text = raw.decode("utf-8", errors="replace")[:_TEXT_MAX_CHARS]
            except Exception:  # noqa: BLE001
                text = "(could not decode file as text)"
            blocks.append(
                {"type": "text", "text": f"Attached file '{name}':\n\n{text}"}
            )
    return blocks


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
        files: list[tuple[str, str, bytes]] = []
        for upload in form.getlist("files"):
            if isinstance(upload, UploadFile) and upload.filename:
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
        attachments = []

    if not message and not attachments:
        return JSONResponse(
            {"error": "Provide a message or at least one file."}, status_code=400
        )

    content: list[dict] = list(attachments)
    if message:
        content.append({"type": "text", "text": message})

    try:
        result = await run_in_threadpool(
            run_turn, _session_key(entity_id, session_id), content, voice
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the client
        return JSONResponse({"error": f"Agent error: {exc}"}, status_code=500)

    result["session_id"] = session_id
    result["entity_id"] = entity_id
    return result


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
