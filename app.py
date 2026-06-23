"""Flask entrypoint for the Hermes AI Agent — web chat UI + JSON API."""
from __future__ import annotations

import base64
import uuid

from flask import Flask, jsonify, render_template, request

from hermes.agent import run_turn
from hermes.config import Config
from hermes.maton import MatonClient, MatonError
from hermes.memory import store

app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload cap

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_MAX_CHARS = 200_000


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
        voice_ws_port=Config.VOICE_WS_PORT,
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
    else:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id") or str(uuid.uuid4())
        message = (data.get("message") or "").strip()
        attachments = []

    if not message and not attachments:
        return jsonify({"error": "Provide a message or at least one file."}), 400

    content: list[dict] = list(attachments)
    if message:
        content.append({"type": "text", "text": message})

    try:
        result = run_turn(session_id, content)
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
    print(f"[Hermes] Starting on http://127.0.0.1:{Config.PORT}  (model: {Config.MODEL})")
    app.run(host="0.0.0.0", port=Config.PORT, debug=True)
