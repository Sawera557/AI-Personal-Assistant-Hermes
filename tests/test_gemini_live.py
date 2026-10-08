"""Gemini Live voice (Home orb): the server-side token minting.

What matters here is the security boundary: the browser gets a single-use token
with the whole session setup locked in, never the real key, and Gemini is told
to route every fact about the user's data through Hermes.

Offline — the Gemini call is stubbed. Run directly or under pytest:

    pytest tests/test_gemini_live.py -q
"""
from __future__ import annotations

import os
import sys

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes import gemini_live  # noqa: E402
from hermes.config import Config  # noqa: E402


class _Resp:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"name": "auth_tokens/test123"}


def test_setup_routes_facts_through_hermes():
    setup = gemini_live.build_setup("afk")
    assert setup["model"] == f"models/{Config.GEMINI_LIVE_MODEL}"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    tools = setup["tools"][0]["functionDeclarations"]
    assert [t["name"] for t in tools] == ["ask_hermes"]
    prompt = setup["systemInstruction"]["parts"][0]["text"]
    assert "ask_hermes" in prompt and "EVERY" in prompt and "never guess" in prompt
    assert "Never answer from your own knowledge" in prompt
    assert "afk" in prompt
    assert "all of the user's businesses" in gemini_live.build_setup("all")["systemInstruction"]["parts"][0]["text"]


def test_mint_locks_setup_and_hides_key(monkeypatch):
    monkeypatch.setattr(Config, "GEMINI_API_KEY", "real-secret-key")
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, body=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(gemini_live.requests, "post", fake_post)
    out = gemini_live.mint_session("all")

    assert out["token"] == "auth_tokens/test123"
    assert "real-secret-key" not in repr(out)  # the key never goes to the browser
    assert sent["headers"]["x-goog-api-key"] == "real-secret-key"
    body = sent["body"]
    assert body["uses"] == 1
    # Every setup field is locked, so a leaked token can't swap prompt/tools/model.
    assert set(body["fieldMask"].split(",")) == set(out["setup"])
    assert body["bidiGenerateContentSetup"] == out["setup"]
    assert out["ws_url"].endswith("BidiGenerateContentConstrained")


def test_mint_without_key_fails_cleanly(monkeypatch):
    monkeypatch.setattr(Config, "GEMINI_API_KEY", "")
    try:
        gemini_live.mint_session("all")
    except gemini_live.LiveError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected LiveError")
