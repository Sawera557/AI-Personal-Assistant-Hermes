"""Configuration loaded from environment variables (.env supported)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

# override=True makes values in .env win over any stale variable already set in
# the OS environment (a common cause of "invalid x-api-key").
load_dotenv(override=True)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Friendly labels for the Maton app slugs Hermes knows about. Used in the
# system prompt so Claude maps "send an email" -> "google-mail", etc.
APP_LABELS: dict[str, str] = {
    "quickbooks": "QuickBooks (accounting)",
    "pdf-co": "PDF.co (PDF parsing / OCR / conversion)",
    "google-contacts": "Google Contacts",
    "fathom": "Fathom (meeting notes & recordings)",
    "google-meet": "Google Meet",
    "google-forms": "Google Forms",
    "google-sheets": "Google Sheets",
    "google-docs": "Google Docs",
    "google-drive": "Google Drive",
    "google-calendar": "Google Calendar",
    "google-mail": "Gmail",
}


class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    MATON_API_KEY: str = os.getenv("MATON_API_KEY", "").strip()
    MATON_BASE_URL: str = os.getenv("MATON_BASE_URL", "https://api.maton.ai").rstrip("/")

    MODEL: str = os.getenv("HERMES_MODEL", "claude-opus-4-8")
    EFFORT: str = os.getenv("HERMES_EFFORT", "medium")
    MAX_TOKENS: int = int(os.getenv("HERMES_MAX_TOKENS", "8000"))
    MAX_TOOL_ITERATIONS: int = int(os.getenv("HERMES_MAX_TOOL_ITERATIONS", "12"))

    WEB_SEARCH: bool = _bool(os.getenv("HERMES_WEB_SEARCH"), True)

    FLASK_SECRET: str = os.getenv("FLASK_SECRET", "dev-secret-change-me")
    PORT: int = int(os.getenv("PORT", "5000"))

    # ---- Voice mode (Pipecat + Groq) — READ-ONLY ----
    # One Groq key covers STT + LLM + TTS. Server-side only; never sent to the
    # browser. The voice service runs as a separate process from Flask.
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_STT_MODEL: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    GROQ_LLM_MODEL: str = os.getenv("GROQ_LLM_MODEL", "llama-3.3-70b-versatile")
    GROQ_TTS_MODEL: str = os.getenv("GROQ_TTS_MODEL", "playai-tts")
    GROQ_TTS_VOICE: str = os.getenv("GROQ_TTS_VOICE", "Celeste-PlayAI")
    VOICE_WS_HOST: str = os.getenv("VOICE_WS_HOST", "0.0.0.0")
    VOICE_WS_PORT: int = int(os.getenv("VOICE_WS_PORT", "8765"))

    @classmethod
    def apps(cls) -> list[str]:
        raw = os.getenv(
            "HERMES_APPS",
            "quickbooks,pdf-co,google-contacts,fathom,google-meet,google-forms,"
            "google-sheets,google-docs,google-drive,google-calendar,google-mail",
        )
        return [a.strip() for a in raw.split(",") if a.strip()]

    @classmethod
    def app_catalog(cls) -> str:
        """Human-readable list of enabled apps for the system prompt."""
        lines = []
        for slug in cls.apps():
            label = APP_LABELS.get(slug, slug)
            lines.append(f"  - {slug}: {label}")
        return "\n".join(lines)

    @classmethod
    def validate(cls) -> list[str]:
        problems = []
        if not cls.ANTHROPIC_API_KEY:
            problems.append("ANTHROPIC_API_KEY is not set")
        if not cls.MATON_API_KEY:
            problems.append("MATON_API_KEY is not set (app integrations will fail)")
        return problems
