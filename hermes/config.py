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

    # ── Owner-notification channel (hermes/notify.py) ────────────────────────
    # Hermes emails the owner (and any per-task "notify to" address) through one
    # business's connected Gmail — there's no dedicated Hermes sender address yet.
    # NOTIFY_BUSINESS picks which business's Maton key sends that mail; it falls
    # back to the first connected slug so notifications still work out of the box.
    NOTIFY_BUSINESS: str = os.getenv("HERMES_NOTIFY_BUSINESS", "").strip()
    # Public origin used to build deep links into the app inside notification
    # emails (e.g. ".../#task/<id>"). On Vercel, VERCEL_URL is the deployment host.
    PUBLIC_BASE_URL: str = (
        os.getenv("PUBLIC_BASE_URL", "").strip()
        or (f"https://{os.getenv('VERCEL_URL', '').strip()}" if os.getenv("VERCEL_URL") else "")
    ).rstrip("/")

    @classmethod
    def notify_business(cls) -> str | None:
        """The business slug whose Gmail sends owner/notify mail. Explicit config
        wins; otherwise the first business with a connected Maton key."""
        if cls.NOTIFY_BUSINESS and cls.maton_key_for_slug(cls.NOTIFY_BUSINESS):
            return cls.NOTIFY_BUSINESS
        slugs = cls.connected_business_slugs()
        return slugs[0] if slugs else None

    @classmethod
    def owner_email(cls) -> str:
        """The single owner's email — the fixed recipient of the daily attention
        report. Same source as db.scheduled_tasks._owner()."""
        return (os.getenv("OWNER_EMAIL") or "owner@hermes.local").strip()

    MODEL: str = os.getenv("HERMES_MODEL", "claude-sonnet-5")
    EFFORT: str = os.getenv("HERMES_EFFORT", "low")
    MAX_TOKENS: int = int(os.getenv("HERMES_MAX_TOKENS", "8000"))
    # Upper bound the agent may auto-grow a single turn's output budget to when a
    # reply is cut off by the token cap (stop_reason == "max_tokens"). The turn
    # keeps continuing with a doubled budget until it finishes or hits this cap.
    # Default is 4x the base budget, so lowering HERMES_MAX_TOKENS lowers the
    # ceiling proportionally rather than leaving a surprising fixed floor.
    MAX_TOKENS_CEILING: int = int(
        os.getenv("HERMES_MAX_TOKENS_CEILING", str(int(os.getenv("HERMES_MAX_TOKENS", "8000")) * 4))
    )
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

    # ---- Jobs (code-driven cron jobs — the Jobs tab) -----------------------
    # A registry of Python job functions (hermes/job_runner.py) fired by the same
    # pull-based cron pattern as Scheduled Tasks (POST /api/jobs/run-due). The
    # first job is the Google-reviews scraper (hermes/reviews_scraper.py).
    #
    # SerpAPI is the pluggable review provider. The free tier is 100 searches/mo
    # (~3/day), so the scraper defaults to ONE bucket per day on rotation at a
    # small per-run search budget — swap the key / raise the budget when you
    # upgrade. Reuses SCHEDULER_SECRET for the /api/jobs/run-due cron auth.
    SERPAPI_API_KEY: str = os.getenv("SERPAPI_API_KEY", "").strip()
    SERPAPI_BASE_URL: str = os.getenv("SERPAPI_BASE_URL", "https://serpapi.com/search").rstrip("/")
    # S3 key prefix the daily reviews CSV lands under: <prefix>/<YYYY-MM-DD>.csv
    REVIEWS_S3_PREFIX: str = os.getenv("REVIEWS_S3_PREFIX", "google-reviews").strip("/") or "google-reviews"

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

    # ---- Retrieval-augmented context (RAG over business knowledge) ---------
    # The autopilot ingestion-service (a separate deployable) embeds each
    # business's emails/Drive files/meeting notes into Pinecone — index
    # `hermes`, one namespace per business (`entity__<slug>`), 1536-dim
    # text-embedding-3-small, with the same sensitivity metadata the pgvector
    # reader enforces. Those vectors are NOT mirrored into Postgres
    # document_chunks.embedding, so hermes/vector_store.py can't see them. These
    # settings let the main app read that Pinecone index directly (read-only —
    # the ingestion-service stays the sole writer) so `search_business_knowledge`
    # and the always-on context block reach the full corpus, not just the
    # hand-added Context-tab notes.
    PINECONE_API_KEY: str = os.getenv("PINECONE_API_KEY", "").strip()
    PINECONE_INDEX: str = os.getenv("PINECONE_INDEX", "hermes").strip()
    PINECONE_CLOUD: str = os.getenv("PINECONE_CLOUD", "aws").strip()
    PINECONE_REGION: str = os.getenv("PINECONE_REGION", "us-east-1").strip()

    # Federated retrieval knobs (hermes/rag.py). max_sensitivity is hard-capped
    # at 'confidential' in the reader so 'restricted' content never surfaces,
    # regardless of this value.
    RAG_TOP_K: int = int(os.getenv("HERMES_RAG_TOP_K", "6"))
    RAG_MAX_SENSITIVITY: str = os.getenv("HERMES_RAG_MAX_SENSITIVITY", "confidential")
    # Minimum cosine similarity (1 - distance, same scale for pgvector and
    # Pinecone) for a chunk to enter the always-on context block. Below this the
    # block stays empty rather than injecting weak/irrelevant matches — the guard
    # that keeps "considered on every query" from bloating the prompt on
    # small-talk turns.
    RAG_SCORE_THRESHOLD: float = float(os.getenv("HERMES_RAG_SCORE_THRESHOLD", "0.30"))
    # Char cap on the injected RETRIEVED CONTEXT block.
    RAG_CONTEXT_MAX_CHARS: int = int(os.getenv("HERMES_RAG_CONTEXT_MAX_CHARS", "6000"))

    @classmethod
    def rag_pinecone_enabled(cls) -> bool:
        """Whether federated retrieval also queries Pinecone (the autopilot
        corpus). Off unless HERMES_RAG_PINECONE is truthy AND a key is set — so a
        missing key fails safe to pgvector-only rather than erroring every turn."""
        return _bool(os.getenv("HERMES_RAG_PINECONE"), False) and bool(cls.PINECONE_API_KEY)

    @classmethod
    def rag_context_injection(cls) -> bool:
        """Whether the always-on RETRIEVED CONTEXT block is built and injected
        into the system prompt every turn. Independent of rag_pinecone_enabled:
        with Pinecone off it still injects pgvector (Context-tab) matches."""
        return _bool(os.getenv("HERMES_RAG_CONTEXT_INJECTION"), False)

    # ---- Conversation-history memory (Phase 2 — pgvector, provenance-first) --
    # Recall of PAST conversations (not just distilled facts): verbatim turns +
    # extractive session summaries in the conversation_memory table (migration
    # 0018). Deliberately separate from the business RAG above so the two never
    # share a failure. Two independent switches (both default OFF — dark merge):
    #   HERMES_CONVO_MEMORY_WRITE  — record each turn (+ summary) after it ends.
    #   HERMES_CONVO_MEMORY        — inject the EARLIER CONVERSATIONS recall block.
    # Write can run ahead of read to backfill history before switching recall on.
    CONVO_MEMORY_TOP_K: int = int(os.getenv("HERMES_CONVO_MEMORY_TOP_K", "6"))
    # Higher than the RAG threshold: conversation recall favours precision — a
    # weak match here reads as "you said X" and is worse than silence.
    CONVO_MEMORY_SCORE_THRESHOLD: float = float(os.getenv("HERMES_CONVO_MEMORY_SCORE_THRESHOLD", "0.35"))
    CONVO_MEMORY_MAX_CHARS: int = int(os.getenv("HERMES_CONVO_MEMORY_MAX_CHARS", "4000"))
    # Verbatim 'turn' rows older than this are pruned (summaries are kept).
    CONVO_MEMORY_TURN_TTL_DAYS: int = int(os.getenv("HERMES_CONVO_MEMORY_TTL_DAYS", "45"))

    @classmethod
    def convo_memory_enabled(cls) -> bool:
        """Whether the EARLIER CONVERSATIONS recall block is injected each turn."""
        return _bool(os.getenv("HERMES_CONVO_MEMORY"), False)

    @classmethod
    def convo_memory_write_enabled(cls) -> bool:
        """Whether finished turns are recorded into conversation_memory."""
        return _bool(os.getenv("HERMES_CONVO_MEMORY_WRITE"), False)

    # ---- NuExtract (bill / invoice OCR, document text extraction) ----------
    # Calls go through the official `numind` SDK (hermes/bills.py), which targets
    # https://nuextract.ai by default. NUEXTRACT_BASE_URL is an OPTIONAL host
    # override for self-hosted/proxy setups only — leave it blank to use the SDK
    # default. (The old default pointed at api.nuextract.ai, a non-resolving host
    # from a since-removed hand-rolled client that never reached the real API.)
    NUEXTRACT_API_KEY: str = os.getenv("NUEXTRACT_API_KEY", "").strip()
    NUEXTRACT_BASE_URL: str = os.getenv("NUEXTRACT_BASE_URL", "").strip().rstrip("/")
    # Optional structured-extraction project id (sprj_…). When set, structured
    # (bill) extraction runs against this project — whose template/schema lives in
    # the NuExtract dashboard — instead of sending Hermes' per-call schema as a
    # throwaway template. The project's fields must match what bills._normalise
    # reads (vendor_name, total_amount, currency, due_date, issue_date,
    # invoice_number, category, line_items); otherwise leave it blank.
    NUEXTRACT_PROJECT_ID: str = os.getenv("NUEXTRACT_PROJECT_ID", "").strip()
    # Client-side wall-clock bound (seconds) on a NuExtract call. The SDK submits a
    # job and blocks on an SSE stream with no client timeout of its own, so without
    # this a slow document could outlast the serverless request limit and kill the
    # function BEFORE the Claude-vision fallback gets to run. Keep it comfortably
    # under the platform's function timeout (see vercel.json maxDuration).
    NUEXTRACT_TIMEOUT: int = int(os.getenv("NUEXTRACT_TIMEOUT", "45"))
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
