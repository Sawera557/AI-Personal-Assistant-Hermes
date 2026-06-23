"""Groq voice layer for Hermes — speech-to-text and text-to-speech.

This module is ONLY the voice I/O. The agent "brain" stays Claude (`run_turn`
in hermes.agent), so the voice agent has the exact same multi-app functionality
as the typed chat. We just:

    mic audio  --(Groq Whisper)-->  transcript  --(run_turn)-->  reply text
    reply text --(Groq PlayAI TTS)-->  spoken audio

Calls go through Groq's OpenAI-compatible REST API with plain `requests`, so
there is no extra SDK dependency and nothing Groq-specific leaks into the
browser (the API key lives server-side only).
"""
from __future__ import annotations

import re
from typing import Any

import requests

from .config import Config

_STT_TIMEOUT = 60
_TTS_TIMEOUT = 60

# Audio MIME -> a filename extension Groq's transcription endpoint accepts.
# Whisper supports: flac, mp3, mp4, mpeg, mpga, m4a, ogg, opus, wav, webm.
_EXT_BY_MIME = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/flac": "flac",
}


class VoiceError(RuntimeError):
    """Raised when a Groq voice call fails."""


def _ext_for(mimetype: str | None) -> str:
    if not mimetype:
        return "webm"
    base = mimetype.split(";", 1)[0].strip().lower()  # drop ";codecs=opus"
    return _EXT_BY_MIME.get(base, "webm")


def transcribe(audio: bytes, mimetype: str | None = None) -> str:
    """Speech -> text via Groq Whisper. Returns the recognized transcript."""
    if not Config.GROQ_API_KEY:
        raise VoiceError("GROQ_API_KEY is not set — voice mode is unavailable.")
    if not audio:
        raise VoiceError("No audio was received.")

    ext = _ext_for(mimetype)
    url = f"{Config.GROQ_BASE_URL}/audio/transcriptions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {Config.GROQ_API_KEY}"},
            files={"file": (f"speech.{ext}", audio, mimetype or "audio/webm")},
            data={
                "model": Config.GROQ_STT_MODEL,
                "response_format": "json",
                "temperature": "0",
            },
            timeout=_STT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VoiceError(f"Network error during transcription: {exc}") from exc

    if not resp.ok:
        raise VoiceError(_groq_error(resp, "transcription"))

    try:
        return (resp.json().get("text") or "").strip()
    except ValueError as exc:
        raise VoiceError("Transcription returned an unreadable response.") from exc


def resolve_voice(voice: str | None) -> str:
    """Return a valid Orpheus voice, falling back to the configured default."""
    v = (voice or "").strip().lower()
    return v if v in Config.TTS_VOICES else Config.GROQ_TTS_VOICE


def synthesize(text: str, voice: str | None = None) -> tuple[bytes, str]:
    """Text -> speech via Groq Orpheus TTS. Returns (audio_bytes, mimetype)."""
    if not Config.GROQ_API_KEY:
        raise VoiceError("GROQ_API_KEY is not set — voice mode is unavailable.")
    text = (text or "").strip()
    if not text:
        raise VoiceError("Nothing to speak.")

    url = f"{Config.GROQ_BASE_URL}/audio/speech"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {Config.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": Config.GROQ_TTS_MODEL,
                "voice": resolve_voice(voice),
                "input": text[:9_000],  # TTS caps input length
                "response_format": "wav",
            },
            timeout=_TTS_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VoiceError(f"Network error during speech synthesis: {exc}") from exc

    if not resp.ok:
        raise VoiceError(_groq_error(resp, "speech"))

    return resp.content, "audio/wav"


def _groq_error(resp: requests.Response, what: str) -> str:
    """Turn a non-2xx Groq response into a helpful message.

    Groq's own messages already include the exact fix (e.g. the model-specific
    terms-acceptance URL), so we surface them verbatim.
    """
    detail: Any = resp.text
    try:
        body = resp.json()
        detail = body.get("error", {}).get("message") or body
    except ValueError:
        pass
    return f"Groq {what} failed ({resp.status_code}): {detail}"


# --- Make agent replies sound natural when spoken --------------------------

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_LIST_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")


def speakable(text: str, limit: int = 1_500) -> str:
    """Strip markdown/URLs/code so the reply reads cleanly aloud."""
    if not text:
        return ""
    out = _CODE_FENCE.sub(" (code omitted) ", text)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _MD_LINK.sub(r"\1", out)            # keep the link text, drop the URL
    out = _BARE_URL.sub("the link", out)
    out = _HEADING.sub("", out)
    out = _LIST_BULLET.sub("", out)
    out = _EMPHASIS.sub("", out)
    out = _MULTISPACE.sub(" ", out)
    out = _MULTINEWLINE.sub("\n\n", out).strip()
    if len(out) > limit:
        # Trim to the last sentence end before the cap so speech doesn't cut mid-word.
        cut = out[:limit]
        dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        out = (cut[: dot + 1] if dot > 200 else cut).rstrip() + " …"
    return out
