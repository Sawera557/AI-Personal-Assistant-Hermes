"""Gemini Live — real-time voice for the Home orb.

The browser talks to Gemini Live directly over a WebSocket (mic PCM in, native
audio out) so there is no STT -> text -> TTS round-trip. Vercel's serverless
functions can't hold a WebSocket, so the server's only job is to mint a
short-lived, single-use *ephemeral token* with the whole session setup locked
into it. The real GEMINI_API_KEY never reaches the browser, and a leaked token
can't be re-pointed at another model, prompt or toolset.

Gemini is the voice, not the brain: every question goes through the
`ask_hermes` tool, which the browser answers by calling /api/chat — the same
Hermes agent, RAG and app connections as the typed chat. Gemini only relays
what Hermes returns.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

from .config import Config

_API = "https://generativelanguage.googleapis.com/v1alpha"
WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained"
)
_TIMEOUT = 20


class LiveError(RuntimeError):
    pass


ASK_HERMES = {
    "name": "ask_hermes",
    "description": (
        "Ask Hermes, the user's assistant. It has the user's real data — calendar, "
        "email, bills, documents, meetings, tasks, memories and connected apps for "
        "every business — and can take actions in those apps. Call it for EVERY "
        "question or request the user makes, passing the request through in full."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {
                "type": "STRING",
                "description": "The user's question or instruction, in full, with any detail they gave.",
            }
        },
        "required": ["request"],
    },
}


def _system_prompt(business: str) -> str:
    today = dt.date.today().strftime("%A %d %B %Y")
    scope = "all of the user's businesses" if business in ("", "all") else f"the business “{business}”"
    return (
        "You are Hermes, the user's personal AI executive assistant, speaking by voice. "
        "Sound like a warm, sharp, relaxed person — conversational, never robotic or "
        "announcer-like. Keep replies short: one to three sentences unless asked for more. "
        f"Today is {today}. The user is currently focused on {scope}.\n\n"
        "You are only the voice of Hermes, never the source of an answer. For EVERY "
        "question or request the user makes — their own data, actions, general knowledge, "
        "maths, advice, anything — call ask_hermes with the request in full, then speak "
        "its answer faithfully in natural spoken form. Never answer from your own knowledge, "
        "and never guess, invent, add to or round numbers, names or dates. If ask_hermes "
        "says it doesn't know or fails, say so plainly. The only things you may say on your "
        "own: a brief 'one sec, checking' while waiting, a one-line acknowledgment of a bare "
        "greeting or 'can you hear me', and asking the user to repeat something you didn't "
        "catch."
    )


def build_setup(business: str = "all") -> dict[str, Any]:
    """The BidiGenerateContentSetup the browser sends as its first message."""
    return {
        "model": f"models/{Config.GEMINI_LIVE_MODEL}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": Config.GEMINI_LIVE_VOICE}}
            },
        },
        "systemInstruction": {"parts": [{"text": _system_prompt(business)}]},
        "tools": [{"functionDeclarations": [ASK_HERMES]}],
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }


def mint_session(business: str = "all") -> dict[str, Any]:
    """Mint a single-use token with `build_setup` locked in. The token must open
    its session within 2 minutes; the session may then run for 30."""
    if not Config.GEMINI_API_KEY:
        raise LiveError("GEMINI_API_KEY is not set — voice mode is unavailable.")
    setup = build_setup(business)
    now = dt.datetime.now(dt.timezone.utc)
    body = {
        "uses": 1,
        "expireTime": (now + dt.timedelta(minutes=30)).isoformat(),
        "newSessionExpireTime": (now + dt.timedelta(minutes=2)).isoformat(),
        "bidiGenerateContentSetup": setup,
        "fieldMask": ",".join(setup),
    }
    try:
        r = requests.post(
            f"{_API}/auth_tokens",
            json=body,
            headers={"x-goog-api-key": Config.GEMINI_API_KEY},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LiveError(f"Couldn't reach Gemini: {exc}") from exc
    if not r.ok:
        raise LiveError(f"Gemini token request failed ({r.status_code}): {r.text[:300]}")
    token = (r.json() or {}).get("name")
    if not token:
        raise LiveError("Gemini returned no token.")
    return {"token": token, "ws_url": WS_URL, "model": Config.GEMINI_LIVE_MODEL, "setup": setup}
