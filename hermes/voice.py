"""Voice layer for Hermes — speech-to-text and text-to-speech.

This module is ONLY the voice I/O. The agent "brain" stays Claude (`run_turn`
in hermes.agent), so the voice agent has the exact same multi-app functionality
as the typed chat. We just:

    mic audio  --(STT)-->  transcript  --(run_turn)-->  reply text
    reply text --(TTS)-->  spoken audio

The provider (OpenAI by default, Groq optional) is selected in config. Both
expose the same OpenAI-style /audio/* REST endpoints, so we call them with plain
`requests` — no extra SDK dependency, and the API key stays server-side only.
"""
from __future__ import annotations

import re
from typing import Any

import requests

from .config import Config

_STT_TIMEOUT = 60
_TTS_TIMEOUT = 60

# Audio MIME -> a filename extension the transcription endpoint accepts.
# Supported: flac, mp3, mp4, mpeg, mpga, m4a, ogg, opus, wav, webm.
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
    """Raised when a voice (STT/TTS) call fails."""


def _ext_for(mimetype: str | None) -> str:
    if not mimetype:
        return "webm"
    base = mimetype.split(";", 1)[0].strip().lower()  # drop ";codecs=opus"
    return _EXT_BY_MIME.get(base, "webm")


# Brand/product names speech-to-text commonly mangles. Seeding these into the
# transcription `prompt` biases it to output the right spelling (e.g. "Vercel"
# instead of "Warsaw") — which also helps a lot with accents.
_BASE_VOCAB = (
    "Vercel, Maton, Hermes, Claude, Anthropic, OpenAI, Groq, QuickBooks, Gmail, "
    "Google Drive, Google Docs, Google Sheets, Google Calendar, Google Forms, "
    "Google Meet, Google Contacts, Fathom, PDF.co, GitHub, OAuth, API, webhook, "
    "endpoint, deploy, dashboard, invoice"
)


def _stt_prompt() -> str:
    """A short context prompt that nudges STT toward the right spellings."""
    terms = _BASE_VOCAB
    if Config.VOICE_VOCAB:
        terms += ", " + Config.VOICE_VOCAB
    return (
        "Conversation with an AI work assistant about business apps and the web. "
        "Transcribe these names with their correct spelling when you hear them: "
        + terms + "."
    )


def transcribe(audio: bytes, mimetype: str | None = None) -> str:
    """Speech -> text. Returns the recognized transcript."""
    if not Config.voice_api_key():
        raise VoiceError("Voice API key is not set — voice mode is unavailable.")
    if not audio:
        raise VoiceError("No audio was received.")

    ext = _ext_for(mimetype)
    url = f"{Config.voice_base_url()}/audio/transcriptions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {Config.voice_api_key()}"},
            files={"file": (f"speech.{ext}", audio, mimetype or "audio/webm")},
            data={
                "model": Config.voice_stt_model(),
                "response_format": "json",
                # Lock recognition to one language so it can't drift to another.
                "language": Config.VOICE_LANGUAGE,
                # Bias toward known brand/product names (accents + jargon).
                "prompt": _stt_prompt(),
            },
            timeout=_STT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VoiceError(f"Network error during transcription: {exc}") from exc

    if not resp.ok:
        raise VoiceError(_voice_error(resp, "transcription"))

    try:
        return (resp.json().get("text") or "").strip()
    except ValueError as exc:
        raise VoiceError("Transcription returned an unreadable response.") from exc


def resolve_voice(voice: str | None) -> str:
    """Return a valid voice for the active provider, else the configured default."""
    v = (voice or "").strip().lower()
    return v if v in Config.voice_voices() else Config.voice_tts_voice()


def synthesize(text: str, voice: str | None = None) -> tuple[bytes, str]:
    """Text -> speech. Returns (audio_bytes, mimetype)."""
    if not Config.voice_api_key():
        raise VoiceError("Voice API key is not set — voice mode is unavailable.")
    text = (text or "").strip()
    if not text:
        raise VoiceError("Nothing to speak.")

    url = f"{Config.voice_base_url()}/audio/speech"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {Config.voice_api_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": Config.voice_tts_model(),
                "voice": resolve_voice(voice),
                "input": text[:9_000],  # TTS caps input length
                "response_format": "wav",
            },
            timeout=_TTS_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VoiceError(f"Network error during speech synthesis: {exc}") from exc

    if not resp.ok:
        raise VoiceError(_voice_error(resp, "speech"))

    return resp.content, "audio/wav"


def _voice_error(resp: requests.Response, what: str) -> str:
    """Turn a non-2xx provider response into a helpful message.

    Provider error bodies usually include the exact fix, so we surface them.
    """
    detail: Any = resp.text
    try:
        body = resp.json()
        detail = body.get("error", {}).get("message") or body
    except ValueError:
        pass
    return f"Voice {what} failed ({resp.status_code}): {detail}"


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
