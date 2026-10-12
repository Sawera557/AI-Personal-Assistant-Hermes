"""Configuration loaded from environment variables (.env supported)."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

# override=True makes values in .env win over any stale variable already set in
# the OS environment (a common cause of "invalid x-api-key").
load_dotenv(override=True)

log = logging.getLogger("hermes.config")


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


def int_env(name: str, default: int) -> int:
    """Read an int env var, treating blank/whitespace/garbage as "not set".

    os.getenv returns "" — not None — for a variable that exists with an empty
    value, so `int(os.getenv(NAME, "8000"))` raises ValueError instead of using
    its default. That happens in the class body below, at import time, which
    takes the whole app down with a 500 on every route before any handler runs.
    A dashboard env var left blank (or holding a typo) must degrade to the
    default, never crash the process.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — falling back to %s", name, raw, default)
        return default


def float_env(name: str, default: float) -> float:
    """Float counterpart of `_int_env`; same blank-and-garbage tolerance."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — falling back to %s", name, raw, default)
        return default


class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    MATON_API_KEY: str = os.getenv("MATON_API_KEY", "").strip()
    MATON_BASE_URL: str = os.getenv("MATON_BASE_URL", "https://api.maton.ai").rstrip("/")

    # Per-project Maton API keys are looked up via the project registry
    # (entities.connections.maton_key_env → that env var), falling back to the
    # shared MATON_API_KEY — see hermes/projects.py::maton_key_for_slug
    # (PLAN.md §2.0). MATON_BUSINESS_KEYS below is the legacy per-Gandi-business
    # override table, kept for compatibility and as the offline fallback.
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
        """The Maton API key for a project slug, or None if unset.

        Delegates to the project registry (per-project `connections` in the
        `entities` table) when the DB is available, with the legacy
        MATON_BUSINESS_KEYS env map as the offline/test fallback."""
        try:
            from .projects import maton_key_for_slug as resolve

            return resolve(slug)
        except Exception as exc:  # noqa: BLE001 — never 500 a turn on config
            log.warning("project key lookup failed for %r: %s", slug, exc)
            return cls.MATON_BUSINESS_KEYS.get(slug or "") or None

    @classmethod
    def connected_business_slugs(cls) -> list[str]:
        """Slugs with a resolvable Maton key (drives the tool-schema enum)."""
        try:
            from .projects import connected_slugs

            return connected_slugs()
        except Exception:  # noqa: BLE001
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
    MAX_TOKENS: int = int_env("HERMES_MAX_TOKENS", 8000)
    # Optional hard cap on generated tokens for providers whose API lets the
    # field be omitted (OpenAI, NVIDIA NIM). 0 / unset — the default — means the
    # reply ends when the model has finished answering, not when it hits a
    # number an operator picked. Set it only as a cost guard; Anthropic ignores
    # it, because max_tokens is mandatory there (see MAX_TOKENS above).
    OUTPUT_CAP: int = int_env("HERMES_OUTPUT_CAP", 0)
    # Upper bound the agent may auto-grow a single turn's output budget to when a
    # reply is cut off by the token cap (stop_reason == "max_tokens"). The turn
    # keeps continuing with a doubled budget until it finishes or hits this cap.
    # Default is 4x the base budget, so lowering HERMES_MAX_TOKENS lowers the
    # ceiling proportionally rather than leaving a surprising fixed floor.
    MAX_TOKENS_CEILING: int = int_env(
        "HERMES_MAX_TOKENS_CEILING", int_env("HERMES_MAX_TOKENS", 8000) * 4
    )
    MAX_TOOL_ITERATIONS: int = int_env("HERMES_MAX_TOOL_ITERATIONS", 12)

    # Dedicated fast model for the live meeting copilot's per-utterance guidance
    # calls (hermes.meeting_copilot.get_guidance / get_guidance_comprehensive).
    # Kept separate from MODEL above: that call is 0-4 short bullets, no tool
    # use, in the middle of a live conversation — latency matters more than the
    # extra reasoning depth a bigger model would add. Chat keeps MODEL so
    # tool-use/agentic quality isn't affected by this.
    # Background jobs run on NVIDIA NIM by policy (see models.BACKGROUND_MODELS).
    # This is a PREFERENCE within that set, not an escape from it: name an
    # Anthropic/OpenAI model here and models.background_model() overrides it, so
    # an old value in a deployed .env can't drag background spend back onto a
    # metered account.
    GUIDANCE_MODEL: str = os.getenv(
        "HERMES_GUIDANCE_MODEL", "google/diffusiongemma-26b-a4b-it"
    )

    WEB_SEARCH: bool = _bool(os.getenv("HERMES_WEB_SEARCH"), True)

    # ---- Guardrails -------------------------------------------------------
    # Guardrail #1: require the user to confirm before any external send/write
    # (email, WhatsApp, Zoom invite, Jira, mutating maton_request). Enforced
    # structurally in hermes.guardrails at the tool-dispatch chokepoint. This is
    # only the DEFAULT — it can be toggled at runtime, globally or per-business,
    # via hermes_settings (see hermes.guardrails.set_confirm_writes). Default on.
    GUARDRAIL_CONFIRM_WRITES: bool = _bool(os.getenv("HERMES_GUARDRAIL_CONFIRM_WRITES"), True)

    # Guardrail #2: business data isolation. When on, knowledge retrieved from
    # Pinecone is verified to belong to the business in scope and cross-business
    # matches are dropped (defense-in-depth over namespace scoping). Enforced in
    # hermes.pinecone_store via hermes.rag.federated_search; runtime-toggleable
    # per-business via hermes_settings. Default on.
    GUARDRAIL_BUSINESS_ISOLATION: bool = _bool(os.getenv("HERMES_GUARDRAIL_BUSINESS_ISOLATION"), True)

    # Guardrail #3: block secrets in outbound actions. A send or external write
    # whose arguments carry a credential (API key, private key, access token) or
    # a payment card number is refused outright — in live chat AND unattended
    # runs, where a prompt-injected email could otherwise exfiltrate one.
    GUARDRAIL_SECRET_EGRESS: bool = _bool(os.getenv("HERMES_GUARDRAIL_SECRET_EGRESS"), True)

    # Guardrail #4: cap sends and deletes per turn. Stops a looping model (or an
    # injected "email everyone in this list") from mass-sending or mass-deleting
    # in one turn, including unattended runs where nobody approves each action.
    GUARDRAIL_SEND_LIMIT: bool = _bool(os.getenv("HERMES_GUARDRAIL_SEND_LIMIT"), True)
    GUARDRAIL_MAX_SENDS_PER_TURN: int = int_env("HERMES_GUARDRAIL_MAX_SENDS_PER_TURN", 10)

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

    # Gemini Live — real-time voice on the Home orb (hermes/gemini_live.py).
    # Gemini does the speaking/listening; questions about the user's data still
    # go to the Claude agent through its `ask_hermes` tool.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_LIVE_MODEL: str = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
    GEMINI_LIVE_VOICE: str = os.getenv("GEMINI_LIVE_VOICE", "Sulafat").strip()

    # NVIDIA NIM — a CHAT provider, not a voice one. Its API is OpenAI-shaped
    # (the `openai` SDK talks to it unchanged) but it needs its own key and
    # host, so it gets its own client in hermes/agent.py rather than
    # overloading OPENAI_BASE_URL, which would hijack embeddings and voice too.
    NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "").strip()
    # build.nvidia.com issues a key per model page, so a NIM model can be
    # provisioned under its own key. Anything not listed here (or listed with a
    # blank value) falls back to NVIDIA_API_KEY — which is usually fine, since
    # one account key normally works across models.
    NVIDIA_MODEL_KEYS: dict[str, str] = {
        "openai/gpt-oss-20b": os.getenv("NVIDIA_GPT_OSS_API_KEY", "").strip(),
        "google/gemma-4-31b-it": os.getenv("NVIDIA_GEMMA_API_KEY", "").strip(),
        "google/diffusiongemma-26b-a4b-it": os.getenv("NVIDIA_DIFFUSIONGEMMA_API_KEY", "").strip(),
        "moonshotai/kimi-k3": os.getenv("NVIDIA_KIMI_API_KEY", "").strip(),
    }
    NVIDIA_BASE_URL: str = os.getenv(
        "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
    ).rstrip("/")
    # Nemotron streams its reasoning separately (`reasoning_content`) before the
    # answer. This caps how many tokens it may spend there; it's clamped to the
    # turn's own output budget at call time so reasoning can never eat the whole
    # cap and leave no room for a reply.
    NVIDIA_REASONING_BUDGET: int = int_env("NVIDIA_REASONING_BUDGET", 16384)
    # Nemotron's recommended sampling defaults (NVIDIA's own model card).
    NVIDIA_TEMPERATURE: float = float_env("NVIDIA_TEMPERATURE", 1.0)
    NVIDIA_TOP_P: float = float_env("NVIDIA_TOP_P", 0.95)
    # Seconds to wait on a NIM call before giving up. build.nvidia.com's FREE
    # endpoints are shared capacity: a Gemma reasoning call was measured at
    # ~145s wall clock, so this has to be generous — but the SDK's own default
    # is 600s with retries on top, which would hang a chat turn for ten minutes.
    NVIDIA_TIMEOUT: float = float_env("NVIDIA_TIMEOUT", 300.0)
    # Background jobs get a much shorter leash than interactive chat. They walk
    # a list of candidate models on timeout (see models.background_candidates),
    # so the interactive 300s would let one memory extraction occupy a thread
    # for 25 minutes across five attempts. Nobody is waiting on this reply —
    # moving on to a responsive model beats waiting out a saturated one.
    BACKGROUND_TIMEOUT: float = float_env("HERMES_BACKGROUND_TIMEOUT", 60.0)

    @classmethod
    def nvidia_key_for(cls, model_id: str) -> str:
        """The NIM key for `model_id` — its own if one is configured, else the
        shared account key."""
        return cls.NVIDIA_MODEL_KEYS.get(model_id) or cls.NVIDIA_API_KEY

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

    PORT: int = int_env("PORT", 5000)

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
    SCHEDULER_POLL_SECONDS: int = int_env("SCHEDULER_POLL_SECONDS", 60)

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
    MEMORY_DUP_THRESHOLD: float = float_env("MEMORY_DUP_THRESHOLD", 0.92)
    MEMORY_UPDATE_THRESHOLD: float = float_env("MEMORY_UPDATE_THRESHOLD", 0.75)
    # Retrieval knobs (hermes/memory_retrieve.py).
    MEMORY_ALWAYS_INJECT_MAX: int = int_env("MEMORY_ALWAYS_INJECT_MAX", 20)
    MEMORY_SEMANTIC_TOP_K: int = int_env("MEMORY_SEMANTIC_TOP_K", 5)
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
    RAG_TOP_K: int = int_env("HERMES_RAG_TOP_K", 6)
    RAG_MAX_SENSITIVITY: str = os.getenv("HERMES_RAG_MAX_SENSITIVITY", "confidential")
    # Minimum cosine similarity (1 - distance, same scale for pgvector and
    # Pinecone) for a chunk to enter the always-on context block. Below this the
    # block stays empty rather than injecting weak/irrelevant matches — the guard
    # that keeps "considered on every query" from bloating the prompt on
    # small-talk turns.
    RAG_SCORE_THRESHOLD: float = float_env("HERMES_RAG_SCORE_THRESHOLD", 0.30)
    # Char cap on the injected RETRIEVED CONTEXT block.
    RAG_CONTEXT_MAX_CHARS: int = int_env("HERMES_RAG_CONTEXT_MAX_CHARS", 6000)

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
    CONVO_MEMORY_TOP_K: int = int_env("HERMES_CONVO_MEMORY_TOP_K", 6)
    # Higher than the RAG threshold: conversation recall favours precision — a
    # weak match here reads as "you said X" and is worse than silence.
    CONVO_MEMORY_SCORE_THRESHOLD: float = float_env("HERMES_CONVO_MEMORY_SCORE_THRESHOLD", 0.35)
    CONVO_MEMORY_MAX_CHARS: int = int_env("HERMES_CONVO_MEMORY_MAX_CHARS", 4000)
    # Verbatim 'turn' rows older than this are pruned (summaries are kept).
    CONVO_MEMORY_TURN_TTL_DAYS: int = int_env("HERMES_CONVO_MEMORY_TTL_DAYS", 45)

    @classmethod
    def convo_memory_enabled(cls) -> bool:
        """Whether the EARLIER CONVERSATIONS recall block is injected each turn."""
        return _bool(os.getenv("HERMES_CONVO_MEMORY"), False)

    @classmethod
    def convo_memory_write_enabled(cls) -> bool:
        """Whether finished turns are recorded into conversation_memory."""
        return _bool(os.getenv("HERMES_CONVO_MEMORY_WRITE"), False)

    # ---- Apps / Business-context split (TURN_COST_PLAN step 0.3) -----------
    # Both read the environment on every call (not once at import), like the
    # conversation-memory switches above, so a test can flip one for itself
    # without reloading the module. Both default OFF, and anything that isn't a
    # recognised "on" value reads as off — an unknown value must fall back to
    # today's behaviour, never into the new state.

    @classmethod
    def split_toggles(cls) -> bool:
        """HERMES_SPLIT_TOGGLES: whether Business context OFF with Apps access ON
        keeps the tools (the new state — tools on, retrieval blocks off, the
        standing-rules core injected in their place).

        Off (the default) keeps the legacy coupling for interactive chat:
        Context off also strips every tool and answers from the plain prompt,
        exactly as before the split. It ships only together with the
        standing-rules core, because tools without the pack would otherwise
        lose the owner's hard rules (money only from live data, security)."""
        return _bool(os.getenv("HERMES_SPLIT_TOGGLES"), False)

    @classmethod
    def standing_rules_core(cls) -> bool:
        """HERMES_STANDING_RULES_CORE: whether a turn with Business context ON
        but an EMPTY context-pack block (the `context_packs` table missing or
        empty — TURN_COST_PLAN B24) still carries the standing-rules core next
        to its tools.

        Separate from split_toggles() on purpose: it changes the payload of
        today's default state (+~377 tokens per call wherever the pack is
        absent), which is an owner decision (§8 Q5). The Context-off + Apps-on
        state always carries the core and does not consult this switch."""
        return _bool(os.getenv("HERMES_STANDING_RULES_CORE"), False)

    # ---- Provider reasoning-effort values (TURN_COST_PLAN step 0.4) --------

    @classmethod
    def provider_effort(cls) -> bool:
        """HERMES_PROVIDER_EFFORT: whether a `light` provider call (the CHAT and
        LIGHT tiers — never a FULL turn) carries the per-model reasoning_effort
        values in hermes.agent._REASONING (Kimi, gpt-oss, gpt-5.x, Gemini).

        Off by default and meant to stay off until the owner-run probes in step
        2.3 have shown each value is accepted and actually lowers reasoning
        tokens: none of them has been sent to a provider yet. The proven
        switches — Anthropic thinking omitted, the NIM boolean thinking toggles
        flipped to False — do not depend on this flag. Read per call, like the
        switches above, so a test can flip it for itself."""
        return _bool(os.getenv("HERMES_PROVIDER_EFFORT"), False)

    # ---- Per-turn router (TURN_COST_PLAN Phase 1, hermes/turn_router.py) ---
    # All read per call, like the switches above, and all fail to today's
    # behaviour: an unknown mode is "off", a garbage number is the default
    # (int_env / float_env), an out-of-range one is clamped. Hard invariant 9:
    # a misconfigured router must route FULL, never cheaper.

    ROUTER_MODES: tuple[str, ...] = ("off", "shadow", "on")
    ROUTER_LIGHT_MODES: tuple[str, ...] = ("off", "shadow", "strict")

    @classmethod
    def router_mode(cls) -> str:
        """HERMES_ROUTER_MODE: off | shadow | on (default off).

        off    — the router is bypassed; every turn is FULL (today's path).
        shadow — the router decides and logs the would-be tier, but the turn
                 still runs FULL (step 1.6's measurement week).
        on     — CHAT-tier turns actually run on the tool-free CHAT path
                 (and, from step 1.5, only while auto_trim_enabled is also on).

        Anything else — a typo, "true", "1" — reads as off. "on" is spelled out
        on purpose: a boolean-looking value must not switch routing on."""
        raw = (os.getenv("HERMES_ROUTER_MODE") or "").strip().lower()
        return raw if raw in cls.ROUTER_MODES else "off"

    @classmethod
    def router_chat_max_tokens(cls) -> int:
        """HERMES_ROUTER_CHAT_MAX_TOKENS (default 1024): the output cap for a
        CHAT-tier call — applied only where reasoning is switched off on that
        call (step 1.2), because NIM bills reasoning against max_tokens and a
        capped Kimi-at-"max" greeting would escalate on every turn. A value
        below 1 falls back to the default rather than producing a 0-token cap."""
        value = int_env("HERMES_ROUTER_CHAT_MAX_TOKENS", 1024)
        return value if value >= 1 else 1024

    @classmethod
    def router_lite_history(cls) -> int:
        """HERMES_ROUTER_LITE_HISTORY (default 2): how many trailing CHAT
        exchanges a CHAT turn may replay. Never negative; 0 means none."""
        return max(0, int_env("HERMES_ROUTER_LITE_HISTORY", 2))

    @classmethod
    def router_lite_history_chars(cls) -> int:
        """HERMES_ROUTER_LITE_HISTORY_CHARS (default 600): per-message char
        budget for that replayed CHAT history. Never negative."""
        return max(0, int_env("HERMES_ROUTER_LITE_HISTORY_CHARS", 600))

    @classmethod
    def router_extra_terms(cls) -> tuple[str, ...]:
        """HERMES_ROUTER_EXTRA_TERMS (default empty): comma-separated extra
        proper nouns — tenants, staff, vendors, project names — that force FULL
        whenever a message contains one, on top of the business names and app
        words the router already knows. Matched case-insensitively as whole
        words/phrases. Blank entries are ignored."""
        raw = os.getenv("HERMES_ROUTER_EXTRA_TERMS") or ""
        return tuple(t.strip() for t in raw.split(",") if t.strip())

    @classmethod
    def router_shadow_sample(cls) -> float:
        """HERMES_ROUTER_SHADOW_SAMPLE (default 0.0): in shadow mode, the share
        (0.0–1.0) of would-be-CHAT turns that also run — and discard — the CHAT
        call, so the escalation rate is measured before go-live (step 1.6).
        Clamped to [0, 1]; NaN reads as 0."""
        value = float_env("HERMES_ROUTER_SHADOW_SAMPLE", 0.0)
        if value != value:  # NaN
            return 0.0
        return min(1.0, max(0.0, value))

    @classmethod
    def router_light(cls) -> str:
        """HERMES_ROUTER_LIGHT: off | shadow | strict (default off) — the Phase 3
        LIGHT tier (general-knowledge questions on a strict allow-list).

        Only "strict" lets the router return LIGHT, and only for a model listed
        in HERMES_ROUTER_LIGHT_MODELS. "shadow" evaluates the LIGHT rule and
        records `light:shadow` on the decision while the turn stays FULL.
        Anything else reads as off."""
        raw = (os.getenv("HERMES_ROUTER_LIGHT") or "").strip().lower()
        return raw if raw in cls.ROUTER_LIGHT_MODES else "off"

    @classmethod
    def router_light_models(cls) -> tuple[str, ...]:
        """HERMES_ROUTER_LIGHT_MODELS (default empty): comma-separated model
        ids that passed the `need_context` smoke test (step 3.2). LIGHT is never
        chosen for a model outside this list, so the default — empty — keeps
        LIGHT off even with HERMES_ROUTER_LIGHT=strict."""
        raw = os.getenv("HERMES_ROUTER_LIGHT_MODELS") or ""
        return tuple(m.strip() for m in raw.split(",") if m.strip())

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
    NUEXTRACT_TIMEOUT: int = int_env("NUEXTRACT_TIMEOUT", 45)
    # ---- Subscriptions & bills register (hermes/register.py) ---------------
    # The Google Sheet that OWNS the recurring-bill register. Hermes reads it
    # and proposes verified amounts for its "AMOUNT IN PDF - UNVERIFIED" lines;
    # it never authors a line of its own. REGISTER_SHEET_BUSINESS names the
    # business slug whose Maton Google account can open that spreadsheet.
    REGISTER_SHEET_ID: str = os.getenv(
        "REGISTER_SHEET_ID", "1YKoQNPEe-EPNmZ8-eDa2aKESiDbuq7N4IcV8n8-AePo"
    ).strip()
    REGISTER_SHEET_BUSINESS: str = os.getenv("REGISTER_SHEET_BUSINESS", "afk").strip()

    # Anthropic vision model used to read documents when NuExtract is unavailable
    # (billing exhausted, outage, DNS). Must be an Anthropic model — the fallback
    # calls Claude directly, independent of the switchable agent model.
    # Vision fallback for OCR when NuExtract is unavailable. Also background,
    # also NVIDIA-only — same override rule as GUIDANCE_MODEL above.
    OCR_FALLBACK_MODEL: str = os.getenv(
        "OCR_FALLBACK_MODEL", "google/diffusiongemma-26b-a4b-it"
    )

    @classmethod
    def validate(cls) -> list[str]:
        problems = []
        if not cls.ANTHROPIC_API_KEY:
            problems.append("ANTHROPIC_API_KEY is not set")
        if os.getenv("EMBED_PROVIDER", "openai").strip().lower() == "gemini":
            if not cls.GEMINI_API_KEY:
                problems.append("GEMINI_API_KEY is not set — needed for embeddings (EMBED_PROVIDER=gemini)")
        elif not cls.OPENAI_API_KEY:
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
