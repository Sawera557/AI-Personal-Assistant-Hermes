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
    "zoom": "Zoom (your own meetings/recordings/webinars)",
    "zoom-admin": "Zoom Admin (account-wide: any user's meetings/recordings/settings)",
    "wati": "WATI (WhatsApp Business messaging)",
    "notion": "Notion (pages, databases, comments)",
    "jira": "Jira (issues/tasks, assignment)",
}


class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    MATON_API_KEY: str = os.getenv("MATON_API_KEY", "").strip()
    MATON_BASE_URL: str = os.getenv("MATON_BASE_URL", "https://api.maton.ai").rstrip("/")

    # Per-business Maton API keys. Each business (see `businesses` table / slug
    # column) can have its own key connecting its own Gmail/Drive/Calendar/etc.
    # "per" (Personal & Family) intentionally aliases the plain default key —
    # it's the original shared personal inbox, not a dedicated business account.
    MATON_BUSINESS_KEYS: dict[str, str] = {
        "afk": os.getenv("AFROFARMS_MATON_API_KEY", "").strip(),
        "gak": os.getenv("GAK_PROPERTIES_MATON_API_KEY", "").strip(),
        "gif": os.getenv("GIF_PROPERTIES_MATON_API_KEY", "").strip(),
        "gakc": os.getenv("GAK_COMMODITIES_MATON_API_KEY", "").strip(),
        "nh": os.getenv("NOUR_HAMMAM_MATON_API_KEY", "").strip(),
        "per": os.getenv("MATON_API_KEY", "").strip(),
    }

    @classmethod
    def maton_key_for_slug(cls, slug: str | None) -> str | None:
        """The Maton API key configured for a business slug, or None if unset."""
        if not slug:
            return None
        return cls.MATON_BUSINESS_KEYS.get(slug) or None

    @classmethod
    def connected_business_slugs(cls) -> list[str]:
        """Business slugs that currently have a configured Maton key."""
        return [slug for slug, key in cls.MATON_BUSINESS_KEYS.items() if key]

    MODEL: str = os.getenv("HERMES_MODEL", "claude-sonnet-5")
    EFFORT: str = os.getenv("HERMES_EFFORT", "low")
    MAX_TOKENS: int = int(os.getenv("HERMES_MAX_TOKENS", "8000"))
    MAX_TOOL_ITERATIONS: int = int(os.getenv("HERMES_MAX_TOOL_ITERATIONS", "12"))

    # Dedicated fast model for the live meeting copilot's per-utterance guidance
    # calls (hermes.meeting_copilot.get_guidance / get_guidance_comprehensive).
    # Kept separate from MODEL above: that call is 0-4 short bullets, no tool
    # use, in the middle of a live conversation — latency matters more than the
    # extra reasoning depth a bigger model would add. Chat keeps MODEL so
    # tool-use/agentic quality isn't affected by this.
    GUIDANCE_MODEL: str = os.getenv("HERMES_GUIDANCE_MODEL", "claude-haiku-4-5-20251001")

    WEB_SEARCH: bool = _bool(os.getenv("HERMES_WEB_SEARCH"), True)

    # ---- Voice layer (speech-to-text + text-to-speech) --------------------
    # The agent "brain" stays Claude (`run_turn`), so the voice agent keeps the
    # EXACT same app functionality as the typed chat. The voice provider only
    # does the listening (STT) and speaking (TTS). Both OpenAI and Groq expose
    # the same OpenAI-style /audio/* endpoints, so switching is just config.
    VOICE_PROVIDER: str = os.getenv("VOICE_PROVIDER", "openai").strip().lower()
    # ISO-639-1 language the voice agent listens and replies in (English only).
    VOICE_LANGUAGE: str = os.getenv("VOICE_LANGUAGE", "en").strip().lower()
    # Extra words/brand names to help speech-to-text spell them right (comma-
    # separated). Add anything it keeps mishearing, e.g. project or client names.
    VOICE_VOCAB: str = os.getenv("VOICE_VOCAB", "").strip()

    # OpenAI (default voice provider)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_BASE_URL: str = os.getenv(
        "OPENAI_BASE_URL", "https://api.openai.com/v1"
    ).rstrip("/")
    # Full transcribe model handles accents + proper nouns far better than mini.
    OPENAI_STT_MODEL: str = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")
    OPENAI_TTS_MODEL: str = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    OPENAI_TTS_VOICE: str = os.getenv("OPENAI_TTS_VOICE", "alloy")
    OPENAI_TTS_VOICES: tuple[str, ...] = (
        "alloy", "ash", "ballad", "coral", "echo",
        "fable", "onyx", "nova", "sage", "shimmer", "verse",
    )

    # Groq (alternative — set VOICE_PROVIDER=groq to use it)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_BASE_URL: str = os.getenv(
        "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
    ).rstrip("/")
    GROQ_STT_MODEL: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    GROQ_TTS_MODEL: str = os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
    GROQ_TTS_VOICE: str = os.getenv("GROQ_TTS_VOICE", "autumn")
    GROQ_TTS_VOICES: tuple[str, ...] = (
        "autumn", "diana", "hannah", "austin", "daniel", "troy",
    )

    # ---- Active-provider resolvers (used by hermes/voice.py and the UI) ----
    @classmethod
    def _use_groq(cls) -> bool:
        return cls.VOICE_PROVIDER == "groq"

    @classmethod
    def voice_api_key(cls) -> str:
        return cls.GROQ_API_KEY if cls._use_groq() else cls.OPENAI_API_KEY

    @classmethod
    def voice_base_url(cls) -> str:
        return cls.GROQ_BASE_URL if cls._use_groq() else cls.OPENAI_BASE_URL

    @classmethod
    def voice_stt_model(cls) -> str:
        return cls.GROQ_STT_MODEL if cls._use_groq() else cls.OPENAI_STT_MODEL

    @classmethod
    def voice_tts_model(cls) -> str:
        return cls.GROQ_TTS_MODEL if cls._use_groq() else cls.OPENAI_TTS_MODEL

    @classmethod
    def voice_tts_voice(cls) -> str:
        return cls.GROQ_TTS_VOICE if cls._use_groq() else cls.OPENAI_TTS_VOICE

    @classmethod
    def voice_voices(cls) -> tuple[str, ...]:
        return cls.GROQ_TTS_VOICES if cls._use_groq() else cls.OPENAI_TTS_VOICES

    @classmethod
    def voice_enabled(cls) -> bool:
        return bool(cls.voice_api_key())

    PORT: int = int(os.getenv("PORT", "5000"))

    # ---- Scheduled Tasks ---------------------------------------------------
    # The run-due endpoint (POST /api/scheduled-tasks/run-due) is what a cron
    # hits to fire due tasks. Set SCHEDULER_SECRET to require it as a bearer
    # token / ?token= on that call, so only your scheduler can trigger runs.
    # If unset, the endpoint is open (fine for local dev; set it in production).
    SCHEDULER_SECRET: str = os.getenv("SCHEDULER_SECRET", "").strip()
    # Wall-clock timezone new tasks default to (the owner's local zone). AFK/Kenya
    # tasks can override per-task in the UI.
    SCHEDULER_DEFAULT_TZ: str = os.getenv("SCHEDULER_DEFAULT_TZ", "America/Chicago").strip()
    # How often the in-process poller scans for due tasks (seconds).
    SCHEDULER_POLL_SECONDS: int = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))

    @classmethod
    def scheduler_inprocess(cls) -> bool:
        """Whether to run the in-process due-task poller so tasks fire without an
        external cron. Auto-ON everywhere EXCEPT Vercel serverless (where a
        background thread wouldn't survive between frozen invocations — the
        vercel.json cron hits /run-due there instead). Force it with
        SCHEDULER_INPROCESS=true/false."""
        raw = os.getenv("SCHEDULER_INPROCESS", "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        return not os.getenv("VERCEL")  # Vercel sets VERCEL=1 in its runtime

    @classmethod
    def cors_origins(cls) -> list[str] | str:
        """Allowed origins for the API. Comma-separated CORS_ORIGINS, or "*"."""
        raw = os.getenv("CORS_ORIGINS", "*").strip()
        if raw == "*" or not raw:
            return "*"
        return [o.strip() for o in raw.split(",") if o.strip()]

    @classmethod
    def apps(cls) -> list[str]:
        raw = os.getenv(
            "HERMES_APPS",
            "quickbooks,pdf-co,google-contacts,fathom,google-meet,google-forms,"
            "google-sheets,google-docs,google-drive,google-calendar,google-mail,wati,notion,"
            "zoom,zoom-admin,jira",
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

    # ---- Persistent memory (owner/business/project/decision) ---------------
    # Kill switch: extraction is pure upside when it works and pure noise when
    # it doesn't, so it's worth being able to turn off without a deploy.
    MEMORY_EXTRACT_ENABLED: bool = _bool(os.getenv("MEMORY_EXTRACT_ENABLED"), True)
    # Cosine-similarity thresholds for write-time consolidation (see
    # hermes/db/memories.py::write_memory) — same scale as pgvector's
    # `1 - cosine_distance` score, so 1.0 is identical, 0.0 is unrelated.
    MEMORY_DUP_THRESHOLD: float = float(os.getenv("MEMORY_DUP_THRESHOLD", "0.92"))
    MEMORY_UPDATE_THRESHOLD: float = float(os.getenv("MEMORY_UPDATE_THRESHOLD", "0.75"))
    # Retrieval knobs (hermes/memory_retrieve.py).
    MEMORY_ALWAYS_INJECT_MAX: int = int(os.getenv("MEMORY_ALWAYS_INJECT_MAX", "20"))
    MEMORY_SEMANTIC_TOP_K: int = int(os.getenv("MEMORY_SEMANTIC_TOP_K", "5"))
    MEMORY_MAX_SENSITIVITY: str = os.getenv("MEMORY_MAX_SENSITIVITY", "confidential")

    # ---- NuExtract (bill / invoice OCR, document text extraction) ----------
    # Base URL isn't configurable — the official `numind` SDK (hermes/bills.py)
    # manages its own endpoint; there used to be a NUEXTRACT_BASE_URL override
    # here pointing at a non-resolving host (api.nuextract.ai), a leftover from
    # a hand-rolled client that never actually reached the real API.
    NUEXTRACT_API_KEY: str = os.getenv("NUEXTRACT_API_KEY", "").strip()
    NUEXTRACT_BASE_URL: str = os.getenv(
        "NUEXTRACT_BASE_URL", "https://api.nuextract.ai"
    ).rstrip("/")
    # Anthropic vision model used to read documents when NuExtract is unavailable
    # (billing exhausted, outage, DNS). Must be an Anthropic model — the fallback
    # calls Claude directly, independent of the switchable agent model.
    OCR_FALLBACK_MODEL: str = os.getenv("OCR_FALLBACK_MODEL", "claude-sonnet-5")

    @classmethod
    def validate(cls) -> list[str]:
        problems = []
        if not cls.ANTHROPIC_API_KEY:
            problems.append("ANTHROPIC_API_KEY is not set")
        if not cls.OPENAI_API_KEY:
            problems.append(
                "OPENAI_API_KEY is not set — needed for embeddings (pgvector search, memory "
                "extraction/retrieval, document ingestion) regardless of which chat model is "
                "active, and additionally for the agent model itself if that's switched to an "
                "OpenAI option"
            )
        if not cls.MATON_API_KEY:
            problems.append("MATON_API_KEY is not set (app integrations will fail)")
        if not cls.NUEXTRACT_API_KEY:
            problems.append("NUEXTRACT_API_KEY is not set (bill extraction will fail)")
        return problems
