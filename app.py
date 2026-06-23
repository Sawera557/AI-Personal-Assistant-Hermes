"""Flask entrypoint for the Hermes AI Agent — web chat UI + JSON API."""
from __future__ import annotations

import base64
import uuid

from flask import Flask, Response, jsonify, render_template, request

from hermes.agent import run_turn
from hermes.config import Config
from hermes.maton import MatonClient, MatonError
from hermes.memory import store
from hermes.voice import VoiceError, speakable, synthesize, transcribe

app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload cap

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_MAX_CHARS = 200_000


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_attachments(files) -> list[dict]:
    """Turn uploaded files into Claude content blocks."""
    blocks: list[dict] = []
    for f in files:
        if not f or not f.filename:
            continue
        raw = f.read()
        mime = (f.mimetype or "").lower()
        name = f.filename

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


@app.route("/")
def index():
    return render_template(
        "index.html",
        apps=Config.apps(),
        model=Config.MODEL,
        web_search=Config.WEB_SEARCH,
        voice_enabled=Config.voice_enabled(),
        voices=Config.TTS_VOICES,
        voice=Config.GROQ_TTS_VOICE,
    )


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "problems": Config.validate()})


@app.route("/api/chat", methods=["POST"])
def chat():
    # Accept JSON or multipart (multipart carries file attachments).
    if request.content_type and request.content_type.startswith("multipart/"):
        session_id = request.form.get("session_id") or str(uuid.uuid4())
        message = (request.form.get("message") or "").strip()
        attachments = _build_attachments(request.files.getlist("files"))
        voice = _truthy(request.form.get("voice"))
    else:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id") or str(uuid.uuid4())
        message = (data.get("message") or "").strip()
        attachments = []
        voice = _truthy(data.get("voice"))

    if not message and not attachments:
        return jsonify({"error": "Provide a message or at least one file."}), 400

    content: list[dict] = list(attachments)
    if message:
        content.append({"type": "text", "text": message})

    try:
        result = run_turn(session_id, content, voice=voice)
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the client
        return jsonify({"error": f"Agent error: {exc}"}), 500

    result["session_id"] = session_id
    return jsonify(result)


@app.route("/api/connections", methods=["GET", "POST"])
def connections():
    client = MatonClient()
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            app_slug = data.get("app")
            if not app_slug:
                return jsonify({"error": "Missing 'app'."}), 400
            return jsonify(client.create_connection(app_slug))
        return jsonify(
            client.list_connections(
                app=request.args.get("app"), status=request.args.get("status")
            )
        )
    except MatonError as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/voice/transcribe", methods=["POST"])
def voice_transcribe():
    """Speech -> text. The browser posts a recorded audio clip; Groq Whisper
    returns the transcript, which the client then sends through /api/chat so the
    voice agent runs the exact same Claude + Maton agent as the typed chat."""
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "No audio uploaded."}), 400
    try:
        text = transcribe(f.read(), f.mimetype)
    except VoiceError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"text": text})


@app.route("/api/voice/speak", methods=["POST"])
def voice_speak():
    """Text -> speech. Returns Groq PlayAI TTS audio for the agent's reply."""
    data = request.get_json(silent=True) or {}
    text = speakable(data.get("text") or "")
    if not text:
        return jsonify({"error": "Nothing to speak."}), 400
    try:
        audio, mimetype = synthesize(text, voice=data.get("voice"))
    except VoiceError as exc:
        return jsonify({"error": str(exc)}), 502
    return Response(audio, mimetype=mimetype)


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id:
        store.reset(session_id)
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    problems = Config.validate()
    if problems:
        print("[Hermes] Configuration warnings:")
        for p in problems:
            print(f"  - {p}")
    voice = "on" if Config.voice_enabled() else "off (set GROQ_API_KEY)"
    print(
        f"[Hermes] Starting on http://127.0.0.1:{Config.PORT}  "
        f"(model: {Config.MODEL}, voice: {voice})"
    )
    app.run(host="0.0.0.0", port=Config.PORT, debug=True)


#AI Stack, All platforms connectors apis mcps cost
# Questions related to AI Dashboard
# API cost
# Which AI you need and you dont need
# Agentic features