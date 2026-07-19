"""End-to-end API tests for the Hermes FastAPI backend.

Self-contained and offline: the network-bound calls (Claude `run_turn`, voice
`transcribe`/`synthesize`) are monkeypatched, so no API keys or connectivity are
needed. Run directly:

    python tests/test_api.py

or under pytest if installed:

    pytest tests/test_api.py -q
"""
from __future__ import annotations

import os
import sys

# Force the in-memory store BEFORE importing app, so tests never touch the real
# database (even when DBRDS_HOST is configured in .env).
os.environ["HERMES_DISABLE_DB"] = "1"

# Make the project root importable when run as `python tests/test_api.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from hermes.attachments import store as attachment_store  # noqa: E402

client = TestClient(app_module.app)

# Capture what the agent receives so we can assert on attachment wiring.
_last_turn: dict = {}


def _fake_run_turn(session_id, content, voice=False, active_business=None, allowed_apps=None):
    _last_turn.clear()
    _last_turn.update(
        session_id=session_id, content=content, voice=voice,
        active_business=active_business, allowed_apps=allowed_apps,
    )
    return {"reply": "ok", "thinking": None, "tool_calls": [], "stop_reason": "end_turn"}


def _fake_transcribe(audio: bytes, mimetype=None, context="chat"):
    return "hello world"


def _fake_synthesize(text: str, voice=None):
    return b"RIFFfakewav", "audio/wav"


# Patch the network boundaries (chat/voice look these names up in app's globals).
app_module.run_turn = _fake_run_turn
app_module.transcribe = _fake_transcribe
app_module.synthesize = _fake_synthesize


# --- helpers ---------------------------------------------------------------

def _kinds(content):
    return [b.get("type") for b in content]


# --- health / voices -------------------------------------------------------

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


def test_voices():
    r = client.get("/api/voice/voices")
    body = r.json()
    assert r.status_code == 200
    assert isinstance(body["voices"], list) and body["voices"]
    assert body["default"] in body["voices"]


# --- chat ------------------------------------------------------------------

def test_chat_requires_input():
    r = client.post("/api/chat", json={})
    assert r.status_code == 400
    assert "error" in r.json()


def test_chat_json_happy_path():
    r = client.post(
        "/api/chat",
        json={"message": "hi", "session_id": "s1", "entity_id": "e1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "ok"
    assert body["session_id"] == "s1"
    assert body["entity_id"] == "e1"
    # entity_id must namespace the conversation key.
    assert _last_turn["session_id"] == "e1:s1"
    assert _kinds(_last_turn["content"]) == ["text"]


def test_chat_multipart_inline_file():
    r = client.post(
        "/api/chat",
        data={"message": "summarize", "entity_id": "e1"},
        files={"files": ("notes.txt", b"some text", "text/plain")},
    )
    assert r.status_code == 200, r.text
    # inline text file -> text block, then the message text block.
    assert _kinds(_last_turn["content"]) == ["text", "text"]


# --- attachments -----------------------------------------------------------

def test_attachments_upload_no_files():
    r = client.post("/api/attachments", data={"entity_id": "e1"})
    assert r.status_code == 400


def test_attachment_lifecycle_and_chat_reference():
    # Upload a text file + a PNG in one request.
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    r = client.post(
        "/api/attachments",
        data={"entity_id": "e1"},
        files=[
            ("files", ("a.txt", b"alpha", "text/plain")),
            ("files", ("b.png", png, "image/png")),
        ],
    )
    assert r.status_code == 200, r.text
    items = r.json()["attachments"]
    assert len(items) == 2
    assert {i["type"] for i in items} == {"text", "image"}
    assert items[0]["bytes"] == len(b"alpha")
    txt_id = items[0]["attachment_id"]
    png_id = items[1]["attachment_id"]

    # GET metadata for the right entity.
    r = client.get(f"/api/attachments/{txt_id}", params={"entity_id": "e1"})
    assert r.status_code == 200
    assert r.json()["filename"] == "a.txt"

    # Wrong entity -> 404 (scoping).
    assert client.get(f"/api/attachments/{txt_id}", params={"entity_id": "other"}).status_code == 404
    # Unknown id -> 404.
    assert client.get("/api/attachments/deadbeef", params={"entity_id": "e1"}).status_code == 404

    # Reference both attachments from chat -> blocks reach the agent, in order.
    r = client.post(
        "/api/chat",
        json={"message": "describe", "entity_id": "e1", "attachment_ids": [txt_id, png_id]},
    )
    assert r.status_code == 200, r.text
    assert _kinds(_last_turn["content"]) == ["text", "image", "text"]

    # Unknown attachment id -> 400.
    r = client.post("/api/chat", json={"entity_id": "e1", "attachment_ids": ["nope"]})
    assert r.status_code == 400
    assert "Unknown attachment_id" in r.json()["error"]

    # Right id but wrong entity -> 400 (cannot cross tenants).
    r = client.post("/api/chat", json={"entity_id": "other", "attachment_ids": [txt_id]})
    assert r.status_code == 400

    # attachment_id alone (no message) is enough to run a turn.
    r = client.post("/api/chat", json={"entity_id": "e1", "attachment_ids": [txt_id]})
    assert r.status_code == 200
    assert _kinds(_last_turn["content"]) == ["text"]

    # Delete, then it's gone and chat can't reference it.
    assert client.delete(f"/api/attachments/{txt_id}", params={"entity_id": "e1"}).status_code == 200
    assert client.get(f"/api/attachments/{txt_id}", params={"entity_id": "e1"}).status_code == 404
    r = client.post("/api/chat", json={"entity_id": "e1", "attachment_ids": [txt_id]})
    assert r.status_code == 400
    # Deleting again -> 404.
    assert client.delete(f"/api/attachments/{txt_id}", params={"entity_id": "e1"}).status_code == 404


# --- voice -----------------------------------------------------------------

def test_transcribe_requires_audio():
    r = client.post("/api/voice/transcribe", data={"entity_id": "e1"})
    assert r.status_code == 400


def test_transcribe_happy_path():
    r = client.post(
        "/api/voice/transcribe",
        data={"entity_id": "e1"},
        files={"audio": ("clip.webm", b"fakeaudio", "audio/webm")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "hello world"
    assert body["entity_id"] == "e1"


def test_speak_requires_text():
    r = client.post("/api/voice/speak", json={"text": ""})
    assert r.status_code == 400


def test_speak_happy_path():
    r = client.post(
        "/api/voice/speak",
        json={"text": "hello there", "voice": "alloy", "entity_id": "e1"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers.get("x-entity-id") == "e1"
    assert r.content  # bytes returned


# --- misc ------------------------------------------------------------------

def test_reset():
    r = client.post("/api/reset", json={"session_id": "s1", "entity_id": "e1"})
    assert r.status_code == 200
    assert r.json()["status"] == "reset"


def test_connections_post_requires_app():
    r = client.post("/api/connections", json={})
    assert r.status_code == 400


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
