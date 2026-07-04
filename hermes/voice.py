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

from .config import APP_LABELS, Config

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


def _stt_prompt(context: str = "chat") -> str:
    """A light context prompt so STT spells the user's real app/brand names right.

    `context="meeting"` swaps in a different prompt for ambient meeting audio
    (see below) instead of the default one-off voice-command framing. The
    provider's transcribe model (gpt-4o-transcribe) is generative, not purely
    acoustic — it leans on this prompt as a bias for *what kind of thing* is
    being said, so a prompt describing "you're talking to an AI assistant
    about your apps" makes it hallucinate plausible app/business chatter on
    any stretch of unclear or noisy meeting audio, rather than transcribing
    only what it can actually make out.
    """
    if context == "meeting":
        return (
            "This is a live business meeting recording picked up by a room "
            "microphone — multiple people may be speaking, with pauses, "
            "silence, and background noise. Transcribe only what is actually "
            "said, verbatim, word for word. Never invent, guess, or complete "
            "a word or sentence to sound plausible; if a stretch of audio is "
            "unclear, silent, or just noise, output nothing for it rather "
            "than fabricating text."
        )
    names = ["Hermes", "Maton"]
    names += [APP_LABELS.get(slug, slug) for slug in Config.apps()]
    if Config.VOICE_VOCAB:
        names += [t.strip() for t in Config.VOICE_VOCAB.split(",") if t.strip()]
    return (
        "The user is talking to an AI work assistant called Hermes about their "
        "connected apps (" + ", ".join(names) + ") and the web. "
        "Transcribe exactly what they say, word for word; do not substitute or "
        "add app or brand names that were not actually spoken."
    )


_NO_SPEECH_PROB_MAX = 0.6  # drop a segment the model itself flags as probably non-speech
_AVG_LOGPROB_MIN = -1.0    # drop a segment it was very unsure about, even if not flagged as silence


def transcribe(audio: bytes, mimetype: str | None = None, context: str = "chat") -> str:
    """Speech -> text. Returns the recognized transcript.

    `context` picks the priming prompt: "chat" (default, one-off voice
    commands spoken directly to Hermes) or "meeting" (ambient room audio —
    see `_stt_prompt`).

    For "meeting", this also switches to a classic Whisper model (OpenAI's
    "whisper-1", or whichever Whisper model Groq is configured with) instead
    of the default gpt-4o-transcribe. gpt-4o-transcribe is generative — tested
    against literal silence it still confidently invents a full plausible
    sentence, even at temperature=0, since it's trained to always produce
    fluent text rather than admit uncertainty. Classic Whisper models expose
    per-segment `no_speech_prob`/`avg_logprob` via `verbose_json`, so segments
    the model itself wasn't confident about can be dropped instead of shipped
    as if they were real speech — essential for a mic that's on continuously
    and picking up plenty of silence and room noise between utterances.
    """
    if not Config.voice_api_key():
        raise VoiceError("Voice API key is not set — voice mode is unavailable.")
    if not audio:
        raise VoiceError("No audio was received.")

    is_meeting = context == "meeting"
    model = "whisper-1" if (is_meeting and not Config._use_groq()) else Config.voice_stt_model()
    response_format = "verbose_json" if is_meeting else "json"

    ext = _ext_for(mimetype)
    url = f"{Config.voice_base_url()}/audio/transcriptions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {Config.voice_api_key()}"},
            files={"file": (f"speech.{ext}", audio, mimetype or "audio/webm")},
            data={
                "model": model,
                "response_format": response_format,
                # Lock recognition to one language so it can't drift to another.
                "language": Config.VOICE_LANGUAGE,
                # Bias toward known brand/product names (accents + jargon) —
                # or, for meetings, toward literal transcription over invention.
                "prompt": _stt_prompt(context),
                # Deterministic decoding — the generative transcribe models
                # get more prone to "creative" filler as temperature rises.
                "temperature": 0,
            },
            timeout=_STT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VoiceError(f"Network error during transcription: {exc}") from exc

    if not resp.ok:
        raise VoiceError(_voice_error(resp, "transcription"))

    try:
        data = resp.json()
    except ValueError as exc:
        raise VoiceError("Transcription returned an unreadable response.") from exc

    if not is_meeting:
        return (data.get("text") or "").strip()

    segments = data.get("segments") or []
    if not segments:
        return (data.get("text") or "").strip()
    kept = [
        seg.get("text", "").strip()
        for seg in segments
        if seg.get("no_speech_prob", 0) <= _NO_SPEECH_PROB_MAX
        and seg.get("avg_logprob", 0) >= _AVG_LOGPROB_MIN
    ]
    return " ".join(t for t in kept if t).strip()


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
