# Hermes — Full Project Scope

A single, comprehensive reference for what this repository contains: every deployable
service, every external connection, every dashboard feature, every API endpoint, and
the data architecture underneath — as the code actually is today (not aspirational).

Companion docs (this file summarizes and cross-links them, it doesn't replace them):
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) · [SCHEDULED_TASKS.md](../features/SCHEDULED_TASKS.md) ·
[API_ENDPOINTS.md](../architecture/API_ENDPOINTS.md) · [DATABASE.md](../architecture/DATABASE.md) ·
[DATA_ARCHITECTURE_REPORT.md](../architecture/DATA_ARCHITECTURE_REPORT.md) ·
[MEETINGS_COPILOT_SPECIFICATION.md](../features/MEETINGS_COPILOT_SPECIFICATION.md)

> ⚠️ **Note on drift:** `DATABASE.md` and `DATA_ARCHITECTURE_REPORT.md` describe the
> vector store as **Pinecone**. That was true at one point, but the **main app has since
> migrated to pgvector** (embeddings stored as `vector` columns in the same Postgres
> instance — see [hermes/vector_store.py](hermes/vector_store.py) and migration
> `0008_pgvector.sql`). The **standalone `ingestion-service/` has not been migrated** and
> still writes to Pinecone (`ingestion-service/ingestion/pinecone_store.py`). See
> [§7 Known gaps](#7-known-gaps--architectural-drift).

---

## 1. What this repository is

Two separately-deployable FastAPI services plus one dashboard UI, all under one repo:

| Component | Entry point | Port | Role |
|---|---|---|---|
| **Main API + Dashboard** | [app.py](app.py) | `5000` | Hermes: the Claude-powered agent, all chat/business logic, and the web dashboard UI it serves at `/` |
| **Ingestion Service** | [ingestion-service/app.py](ingestion-service/app.py) | `5100` | Standalone background worker: syncs Gmail/Drive/Fathom into a vector store on a schedule, exposes a protected `/api/search` for the meeting copilot |
| **Dashboard UI** | [templates/index.html](../../templates/index.html) + [static/*.js](static) | served by the Main API | 7-tab single-page app (vanilla JS, no framework/build step) |

**Hermes** is an AI executive assistant for **Gandi**, a founder running a multi-entity
portfolio (real estate, farming, commodities, a spa, personal life). It answers in chat
and — through the **Maton** API gateway — reads and acts across each business's connected
accounts (email, calendar, drive, accounting, messaging, meetings, notes). The same agent
also runs **unattended on a schedule** via Scheduled Tasks, and **code-driven crons** via
Jobs.

---

## 2. Businesses (entities) — two parallel slug schemes

There are genuinely **two** business-identity systems in this codebase, built by separate
features and bridged by one module ([hermes/businesses.py](hermes/businesses.py)):

| `businesses` table slug (Bills, chat, tools, Maton keys) | `entities` table slug (chat history, documents, vector search) | Business | Notes |
|---|---|---|---|
| `afk` | `afro_farm` | Afro Farm Holding (Kenya) | Farm ops, ~54 staff payroll (KES), GLOBALG.A.P., market prices |
| `gak` | `gak_properties` | GAK Properties LLC | Landlord — leases, rent, tenants (720 E Lake) |
| `gif` | `gif_properties` | GIF Properties LLC | Landlord — leases, rent, tenants (2609 15th Ave S, 1229 E Lake, 1230 Logan) |
| `gakc` | `gak_commodities` | GAK Commodities Ltd | Trading / commodities |
| `nh` | `nour_hammam` | Nour Hammam LLC | Spa pre-launch (buildout at 720 E Lake) |
| `per` | `personal` | Personal & Family | Personal inbox, father's care, prayer/halal-aware scheduling |
| — | `comfort_services` | *archived* | Seeded in `entities` but has no corresponding `businesses` row — excluded from the mapping on purpose |
| — | `kenya_ventures` | *archived* | Same — archived, intentionally unmapped |

Every request is scoped to one business (locked per conversation) or to **All
Businesses** (`"all"`). Each business has its own connected accounts via a
per-business **Maton API key** (`hermes/config.py::MATON_BUSINESS_KEYS`), so one
business can never see another's data. `per` intentionally shares the default/global
Maton key (the original personal inbox); the other five each have a dedicated key.

---

## 3. Connections — every external system this app talks to

### 3.1 Maton-gated business apps (per-business OAuth, one gateway)

Maton holds each business's OAuth tokens and proxies calls to each app's native REST
API. Hermes reaches every app below either through a **dedicated tool** or the universal
`maton_request` tool (any endpoint of any connected app). Configured via `HERMES_APPS`
in `.env` (default list below).

| App slug | What it is | Dedicated tools | Small/big features enabled |
|---|---|---|---|
| `google-mail` | Gmail | `send_email`, `email_with_attachment` | Read/search/triage inbox, summarize threads, rank by urgency, send mail, create drafts, email a generated file/attachment, bill-scanning source, invoice payment-confirmation detection |
| `google-calendar` | Google Calendar | `calendar_list_upcoming` | List upcoming events; create/update/delete + availability via `maton_request`; feeds Meeting Copilot's upcoming-meetings list and prep briefs |
| `google-drive` | Google Drive | `drive_read_file` | Open a Drive file as **real readable content** (PDFs/images), search Drive, bill-scanning source |
| `google-sheets` | Google Sheets | — (via `maton_request`) | Read/write spreadsheet ranges |
| `google-docs` | Google Docs | — (via `maton_request`) | Read/write documents |
| `google-forms` | Google Forms | — (via `maton_request`) | Read form responses |
| `google-contacts` | Google Contacts | — (via `maton_request`) | Look up contacts |
| `google-meet` | Google Meet | — (via `maton_request`) | Meeting info |
| `quickbooks` | QuickBooks (accounting) | — (via `maton_request`) | AR/AP aging, P&L, balance sheet, invoices, estimates, customers, product/sales summaries, payroll data — feeds cash-flow and rent-arrears style analysis |
| `notion` | Notion | — (via `maton_request`) | Search workspace; read/create/update pages & database rows; read/post comments |
| `jira` | Jira | `jira_search_issues`, `jira_get_issue`, `jira_create_issue`, `jira_assign_issue` | Search/get issues, create issues, assign to people by email |
| `zoom` | Zoom (personal account) | `zoom_send_invite`, `zoom_list_meetings` | Your meetings, recordings, webinars, registrants; send an invite |
| `zoom-admin` | Zoom Admin | same tools as `zoom` (account-wide) | Any user's meetings/recordings/settings |
| `wati` | WATI (WhatsApp Business) | `wati_list_contacts`, `wati_add_contact`, `wati_update_contact_attributes`, `wati_get_messages`, `wati_send_message`, `wati_send_file`, `wati_assign_operator`, `wati_get_media` | Full WhatsApp Business operations: contacts, messaging, files, operator assignment, media fetch |
| `fathom` | Fathom | — (via `maton_request`) | Meeting notes & recordings; also has a dedicated `hermes/fathom.py` helper module and a "attach Fathom link" endpoint on meeting sessions |
| `pdf-co` | PDF.co | — (via `maton_request`) | PDF parsing / OCR / conversion (also referenced as the intended path for PDF/image text extraction into the knowledge base — see §7) |

Default `HERMES_APPS` (`.env.example`): `quickbooks, pdf-co, google-contacts, fathom,
google-meet, google-forms, google-sheets, google-docs, google-drive, google-calendar,
google-mail, zoom, wati, jira`.

**Connection management (dashboard-visible):**
- `GET /api/connections` — list connections (optional `app`, `status` filters) — live
  ACTIVE/PENDING/FAILED status per business.
- `POST /api/connections` `{app}` — start an OAuth connection flow; returns a `url`.
- Agent-side equivalents: `maton_list_connections`, `maton_create_connection` tools.
- A business without a configured Maton key returns a clear "not connected" message
  rather than silently using another business's account.

### 3.2 Direct AI / infrastructure integrations (not via Maton)

| Service | Purpose | Where wired |
|---|---|---|
| **Anthropic Claude** | The agent's brain — the whole tool-use loop | `hermes/agent.py`; model configurable via `HERMES_MODEL` / `POST /api/settings/model` |
| **Claude Haiku (guidance model)** | Fast, cheap model dedicated to live Meeting Copilot guidance bullets | `HERMES_GUIDANCE_MODEL` in `hermes/meeting_copilot.py` |
| **OpenAI** | Speech-to-text (`gpt-4o-transcribe`) + text-to-speech (`gpt-4o-mini-tts`); also the embeddings provider (`text-embedding-3-small`, 1536-dim) for pgvector | `hermes/voice.py`, `hermes/vector_store.py` |
| **Groq** (alternative voice provider) | STT (`whisper-large-v3-turbo`) + TTS (Orpheus voices) | `hermes/voice.py`, toggled via `VOICE_PROVIDER=groq` |
| **NuExtract** | Structured-data extraction from bill/invoice PDFs and images | `hermes/bills.py`, `hermes/extract.py` |
| **SerpAPI** | Google-reviews data provider for the review-intelligence scraper job | `hermes/reviews_scraper.py` |
| **World Bank Open Data API** | Public macro/development indicators (e.g. Kenya maize prices) for forecasting | `hermes/worldbank.py` → `worldbank_data` tool |
| **Web search / fetch** (Claude native tools) | Live external information, when enabled | `hermes/tools.py`, gated by `HERMES_WEB_SEARCH` |
| **AWS RDS (Postgres 17.7)** | System of record: users, businesses/entities, chat history, documents, memories, scheduled tasks, jobs, bills | `hermes/db/*`, `hermes/db/connection.py` |
| **AWS S3** | The locker — original file bytes (bills, generated files, WATI media, Drive reads, daily review-report HTML/CSV) | `hermes/storage.py` |
| **pgvector (on the same Postgres)** | The search brain for the main app's knowledge base / semantic search | `hermes/vector_store.py` |
| **Pinecone** (ingestion-service only) | Vector store for the separate autopilot ingestion pipeline — **not** the same backend the main app now uses (see §7) | `ingestion-service/ingestion/pinecone_store.py` |
| **boto3 / AWS IAM** | Either generates a short-lived RDS auth token (IAM DB auth) or is used for S3 | `hermes/db/connection.py`, `hermes/storage.py` |

---

## 4. Dashboard — 7 tabs, feature by feature

The dashboard is a single-page app (`templates/index.html` + `static/*.js`, no build
step) with a left strip of tabs:

### 4.1 AI Agent (`static/app.js`, `static/voice.js`, `static/model.js`)
- Free-form chat with the Claude-powered agent, scoped to a locked business or "All
  Businesses."
- Streaming replies (SSE) or single-shot JSON.
- File attachments (PDF/image/text) uploaded once, referenced across turns.
- **Voice mode**: record → transcribe (STT) → run the agent → speak the reply (TTS);
  choice of voice.
- Conversation history sidebar: list, reopen, and delete past conversations (persisted
  in Postgres, titled from the first user message).
- Tool-call trace shown per turn (which apps/tools the agent actually used).
- Model switcher (`GET/POST /api/settings/model`) — swap the active LLM platform-wide.
- **Safety model**: before any write (send email, modify QuickBooks, delete, etc.) the
  agent summarizes the change and waits for confirmation in chat.
- **Persistent memory**: the agent silently extracts durable owner/business/project
  memories after each turn (background thread, zero added latency) and recalls relevant
  ones before answering future turns — see §5.9.

### 4.2 Billing & Invoices (`static/bills.js`)
- Upload a bill/invoice (PDF/image) → NuExtract pulls vendor, amount, invoice number,
  due date, category → auto-guesses the business if not specified → stored + viewable.
- Manual edit of any bill field (status, vendor, amount, due date, category, notes).
- Delete a bill (also removes its S3 file).
- **Insights** view (`GET /api/bills/insights`) — portfolio-wide billing analytics.
- View original document: presigned S3 URL, Drive deep link, or Gmail deep link
  depending on source.
- **Scan** (`POST /api/bills/scan`) — proactively scans a business's Gmail/Drive for new
  bills over a configurable lookback window.
- **Auto-mark-paid** (`POST /api/bills/check-payments`) — cross-checks Gmail for payment
  confirmation emails and flips matching bills to paid.

### 4.3 Meeting Copilot (`static/meeting.js`)
- **Live guidance**: as a meeting is transcribed, get 0–4 grounded guidance bullets per
  turn (risk/conflict, unasked question, dependency/capacity constraint, next step, data
  insight) — never generic, never repeated within a session.
- **Comprehensive mode** (`comprehensive: true`): pulls from 5 data layers concurrently —
  business strategy (goals/roadmap/budget), team structure/capacity, known risks &
  blockers, decision history (conflict detection against past decisions), and indexed
  historical records (semantic search over Gmail/Drive/notes) — see
  [MEETINGS_COPILOT_SPECIFICATION.md](../features/MEETINGS_COPILOT_SPECIFICATION.md).
- **Upcoming meetings** view — calendar events annotated with copilot status, per
  business, look-ahead window configurable.
- **Pre-meeting AI brief** (`POST /api/meetings/prepare`) — generates prep notes for a
  specific calendar event before it happens.
- **Deep AI notes** (`POST /.../ai-notes`) — live Gmail/Drive search to enrich notes
  mid-flow.
- **Session history** — full replay of a past meeting's turns/guidance; wipe turns while
  keeping the session; delete a session entirely.
- **Post-meeting summary** (`POST /.../end`) — auto-generates a summary and a Fathom
  recording link; a Fathom link can also be attached manually.

### 4.4 Business Context (`static/context.js`) — two sub-tabs
- **Files sub-tab**: the per-business knowledge base. Upload documents (or ingest
  globally with `entity_id=all` so a doc surfaces across every business); list, view
  full body text, edit (title/sensitivity/body — editing body re-embeds), delete
  (cascades chunks + best-effort S3 cleanup).
- **Memories sub-tab**: view/delete the durable owner/business/project memories the
  agent has extracted from past conversations.
- **Semantic search** (`GET /api/search`) underlies both — meaning search scoped to one
  business, gated by a sensitivity ceiling (`public < internal < confidential <
  restricted`), with results joined back to source documents + presigned download links.

### 4.5 Scheduled Tasks ⏰ (`static/tasks.js`)
The full recurring-automation feature — see [SCHEDULED_TASKS.md](../features/SCHEDULED_TASKS.md) for
the authoritative deep dive. Summary:
- **Mental model**: a saved chat turn plus a clock — same agent, same tools, same
  business scoping, fired automatically instead of typed.
- **Create** a task from the UI *or* by asking Hermes in chat ("Every morning at 7 give
  me a farm briefing from AFK Gmail and Calendar" → daily 07:00, business `afk`,
  read-only). Hermes infers schedule/business/apps/autonomy and confirms if ambiguous.
- **Schedule shapes**: daily, weekdays (Mon–Fri), weekly (chosen days), hourly; wall-clock
  times in the task's own IANA timezone.
- **Autonomy ceiling** (the unattended safety substitute for a human approving writes):
  `readonly` (default; read/analyze/report/`create_file` only) → `draft` (+ create Gmail
  drafts, never send) → `full` (may actually send/modify — an explicit standing
  authorization).
- **App restriction**: a task can be locked to a subset of connected apps; the agent's
  toolset is narrowed accordingly (including `maton_request`'s app enum).
- **Date-window backfills**: an optional per-task cursor that processes one calendar day
  per run across a bounded `[window_start … window_end]` range (e.g. "read each day's
  WhatsApp for a month, one focused batch per fire") — `catchup` pace drains a past range
  as fast as the poller ticks, `scheduled` pace advances one day per normal fire.
- **Run history** per task: reply text + full tool-call trace, status
  (running/success/error), trigger (schedule/manual).
- **Manual "Run now"** — fires immediately without moving the schedule.
- **Firing**: pull-based (works on serverless) — an in-process poller (always-on hosts,
  default 60 s) or Vercel Cron hitting `run-due` every 15 min; secured by
  `SCHEDULER_SECRET`/`CRON_SECRET`; atomic claim prevents double-firing; stale "running"
  runs self-heal after a dead process.

### 4.6 File Explorer (`static/files.js`)
- Browse ingested/generated files across the S3-backed storage as a tree
  (`GET /api/files/tree`), with list/tiles view toggle.
- Surfaces documents from the knowledge base, generated files (from `create_file`),
  Drive reads, and WATI media under their business-scoped prefixes.

### 4.7 Jobs (`static/jobs.js`)
Code-driven cron jobs — the sibling system to Scheduled Tasks, but firing a **registered
Python function** instead of an agent prompt (`hermes/job_runner.py`, mirrors the
scheduler's schedule math). Currently one job type is registered:
- **`google_reviews_scraper`** (`hermes/reviews_scraper.py`) — gathers Google reviews of
  hammams/spas across three buckets (Minnesota, Somalia, general), on a daily rotation,
  within a hard SerpAPI search budget; labels reviews top/worst/neutral by star rating;
  dedups across sort/page overlaps; writes a date-named CSV to S3 (appending same-day
  runs) and a self-contained HTML intelligence report served at
  `GET /api/reviews/report/{date}`.
- Job cards show name, schedule, enabled state, run history; **Start** (run now) and
  **Stop** (flag a running job to cancel) controls.
- `allow_multiple_runs` toggle — whether overlapping runs of the same job are permitted.
- New job types are added purely by writing a function and registering it in
  `job_runner.REGISTRY` — DB layer, API, schedule math, and UI are all generic.

---

## 5. Feature catalog by domain (cross-cutting, not tied to one tab)

1. **Chat agent core** — Claude tool-use loop (`run_turn`/`stream_turn`), multi-tenant via
   `entity_id`, conversation memory isolated per entity+session, up to
   `HERMES_MAX_TOOL_ITERATIONS` (default 12) tool calls per turn.
2. **Email** — read/search/triage/summarize/rank Gmail; send mail; create drafts; email a
   generated file as an attachment (with a >25MB Drive-link fallback).
3. **Calendar** — list upcoming events; create/update/delete/check-availability via
   `maton_request`.
4. **Drive & documents** — read a Drive file as real content (PDF/image), search Drive;
   read/write Sheets, Docs, Forms.
5. **Accounting (QuickBooks)** — AR/AP aging, P&L, balance sheet, invoices, estimates,
   customers, product/sales summaries, payroll data.
6. **Notion** — search workspace; read/create/update pages & database rows; comments.
7. **Jira** — search/get/create/assign issues.
8. **Meetings (Zoom / Zoom-admin / Fathom / Google Meet)** — list/create/update meetings,
   recordings, webinars, registrants; send invites.
9. **WhatsApp (WATI)** — contacts, messaging, file sends, operator assignment, media
   fetch.
10. **File generation** — `create_file` turns agent-written text into a downloadable
    document/report/CSV/PDF with a working link, stored under a business-scoped
    `generated/` S3 prefix so Hermes can read back what it created.
11. **Knowledge base / RAG** — ingest (`POST /api/documents`) → chunk → embed → pgvector
    upsert; guarded semantic search (`search_business_knowledge` tool /
    `GET /api/search`) scoped by business namespace + sensitivity ceiling + `is_active`;
    a **global** namespace (`entity_id IS NULL`) surfaces "for all businesses" documents
    automatically in every business's search.
12. **Persistent memory** — silent post-turn extraction of owner/business/project
    memories into Postgres with a pgvector embedding, deduped/updated by similarity
    threshold, recalled semantically before future turns (`hermes/memory_extract.py`,
    `hermes/memory_retrieve.py`).
13. **Bills / invoice pipeline** — NuExtract-based structured extraction, business
    auto-guessing, Gmail/Drive scanning, payment-confirmation auto-detection, insights.
14. **Review intelligence** — the Google-reviews scraper Job + daily HTML report.
15. **Scheduled Tasks** — see §4.5.
16. **Jobs** — see §4.7.
17. **World Bank data** — public macro indicators for forecasting (e.g. Kenya maize
    prices).
18. **Web search / fetch** — live external information (Claude native tools), when
    enabled.
19. **Voice** — STT (transcribe) → chat → TTS (speak), provider-swappable (OpenAI/Groq),
    with a custom vocabulary hint list for names STT commonly mishears.
20. **Businesses admin** — list/upsert businesses (`GET/POST /api/businesses`), each
    tagged with live `connected` status.
21. **Multi-tenant attachment store** — upload once, reference by id across chat turns,
    scoped to `entity_id`, with metadata get/delete.
22. **Media presign** — short-lived HTTPS links for tool-generated S3 media
    (`GET /api/media/presign`).

---

## 6. API surface — condensed reference

Full detail in [API_ENDPOINTS.md](../architecture/API_ENDPOINTS.md) and the Postman collection
([postman/Hermes_API.postman_collection.json](../../postman/Hermes_API.postman_collection.json)).
Base conventions: JSON in/out (a few multipart uploads + one SSE stream); non-2xx is
`{"error": "..."}`; almost everything is scoped by a business slug or `"all"`.

**Main API — `:5000`**

| Area | Routes |
|---|---|
| Health & settings | `GET /api/health`, `GET/POST /api/settings/model`, `POST /api/admin/migrate` |
| Chat | `POST /api/chat`, `POST /api/chat/stream` (SSE) |
| Conversations | `GET /api/conversations`, `GET/DELETE /api/conversations/{id}` |
| Attachments | `POST /api/attachments`, `GET/DELETE /api/attachments/{id}`, `GET /api/media/presign` |
| Documents (KB) | `POST/GET /api/documents`, `GET/PATCH/DELETE /api/documents/{id}`, `GET /api/files/tree` |
| Memories | `GET /api/memories`, `DELETE /api/memories/{id}` |
| Search | `GET /api/search` |
| Meeting Copilot | `POST /api/meetings/copilot`, `GET /api/meetings/sessions`, `GET /api/meetings/upcoming`, `POST /api/meetings/prepare`, `POST .../ai-notes`, `GET .../sessions/{id}`, `POST .../end`, `POST .../fathom`, `DELETE .../turns`, `DELETE .../sessions/{id}` |
| Connections | `GET/POST /api/connections` |
| Voice | `POST /api/voice/transcribe`, `POST /api/voice/speak`, `GET /api/voice/voices` |
| Session | `POST /api/reset` |
| Businesses | `GET/POST /api/businesses` |
| Bills | `GET /api/bills`, `POST /api/bills/upload`, `PATCH/DELETE /api/bills/{id}`, `GET /api/bills/insights`, `GET .../view`, `GET .../email-body`, `POST /api/bills/scan`, `POST /api/bills/check-payments` |
| Scheduled Tasks | `GET/POST /api/scheduled-tasks`, `GET/PATCH/DELETE /api/scheduled-tasks/{id}`, `GET .../runs`, `POST .../run`, `GET|POST /api/scheduled-tasks/run-due` |
| Jobs | `GET/POST /api/jobs`, `GET|POST /api/jobs/run-due`, `GET/PATCH/DELETE /api/jobs/{id}`, `GET .../runs`, `POST .../run`, `POST .../stop` |
| Reviews | `GET /api/reviews/report/{date}` |

**Ingestion Service — `:5100`** (all but `/api/health` require `X-Cron-Secret` or
`Authorization: Bearer` auth)

| Route | Purpose |
|---|---|
| `GET /api/health` | Public liveness check |
| `POST /api/workers/sync-business` | Sync one business's Gmail/Drive/Fathom |
| `POST /api/workers/sync-all` | Sync every connected business |
| `POST /api/workers/reindex-business` | Rebuild embeddings |
| `POST /api/workers/index-source-item` | Index one arbitrary item |
| `POST /api/search` | RAG retrieval read path (protected) |
| `GET /api/cron/sync-all` | GET variant for external schedulers |
| `POST /api/admin/migrate` | Apply pending SQL migrations |
| `GET /api/workers/status` | Recent sync runs / per-business state |

---

## 7. Known gaps / architectural drift

Worth knowing before building on top of this — surfaced by reading the current code
against the existing docs, not from the docs themselves:

- **Vector-store split-brain.** The main app moved from Pinecone to **pgvector**
  (`hermes/vector_store.py`, migration `0008_pgvector.sql`), but `ingestion-service/`
  still writes to **Pinecone** (`ingestion-service/ingestion/pinecone_store.py`) and its
  own README/comments still describe the main app reading from Pinecone
  (`hermes/pinecone_store.query` — that module no longer exists). As-is, anything the
  ingestion service indexes does **not** land where `hermes/search.py` /
  `search_business_knowledge` actually look. Either migrate the ingestion service to
  pgvector too, or point the main app back at Pinecone for that path — right now they're
  not talking to the same store.
- **Two business-identity schemes.** `businesses` (Bills/chat/tools, slugs like `afk`)
  and `entities` (chat history/documents/search, slugs like `afro_farm`) were built
  separately and are bridged only by `hermes/businesses.py`. Two `entities` rows
  (`comfort_services`, `kenya_ventures`) are archived/orphaned with no `businesses`
  counterpart.
- **~16 Postgres tables are schema-only.** Per `DATA_ARCHITECTURE_REPORT.md`: only 6
  tables are touched by runtime code (`users`, `entities`, `hermes_sessions`,
  `hermes_messages`, `documents`, `document_chunks`, plus `roles`/`connected_apps` at
  seed time). `sync_jobs`, `ai_runs`, `hermes_agent_runs`, `hermes_tool_calls`,
  `approvals`, `audit_logs`, `activity_events`, `pipeline_runs`, `notifications`,
  `quickbooks_snapshots`, `calendar_events`, `email_threads`, `email_messages`,
  `farm_form_submissions` exist as forward-looking structure with no code path yet.
- **PDF/image text extraction is unwired in the main app's ingest path.**
  `ingest_document` only indexes text-decodable files; binaries get a `documents` row
  but no chunks/vectors. `pdf-co` is a connected app and the intended route, not yet
  wired into `hermes/ingest.py`.
- **Chat memory isn't embedded for semantic recall.** The `user__<user_id>` private
  namespace is designed (in `vector_store.py`'s namespace scheme) but never populated —
  chat turns aren't vectorized, only extracted memories and documents are.
- **No connection pooling.** `hermes/db/connection.py` opens a fresh psycopg connection
  per request — fine at current load, flagged in the code as a swap-in-a-pool-under-load
  item.
- **S3 is optional.** If `S3_BUCKET` is unset, documents/bills are still recorded and
  indexed, but original bytes aren't persisted — several features (view original bill,
  presigned links, generated files) degrade without it.
- **No auth layer on the Main API.** Per `API_ENDPOINTS.md`: secure it at the
  network/CORS/reverse-proxy level (or add one) before exposing it publicly; only the
  ingestion service's worker/cron/search/admin routes are protected today.

---

## 8. Code map

| Area | File(s) |
|---|---|
| Agent loop | `hermes/agent.py` (`run_turn`, `stream_turn`) |
| Tools & dispatch | `hermes/tools.py` (`build_tools`, `dispatch`, app-gating via `_TOOL_REQUIRED_APPS`) |
| Maton gateway | `hermes/maton.py`; per-business keys in `hermes/config.py` |
| Business ↔ entity bridge | `hermes/businesses.py` |
| Chat memory (in-turn) | `hermes/memory.py` (Postgres-backed when `DBRDS_HOST` set, else in-process) |
| Persistent memory (cross-turn) | `hermes/memory_extract.py`, `hermes/memory_retrieve.py`, `hermes/db/memories.py` |
| Knowledge base ingest/search | `hermes/ingest.py`, `hermes/search.py`, `hermes/vector_store.py`, `hermes/db/documents.py` |
| Storage (S3) | `hermes/storage.py` |
| DB connection/auth | `hermes/db/connection.py` |
| Bills pipeline | `hermes/bills.py`, `hermes/extract.py`, `hermes/db/bills.py`, `hermes/email_attachments.py` |
| Meeting Copilot | `hermes/meeting_copilot.py`, `hermes/db/meetings.py`, `hermes/fathom.py`, `hermes/calendar.py` |
| Scheduled Tasks | `hermes/scheduler.py`, `hermes/db/scheduled_tasks.py` |
| Jobs | `hermes/job_runner.py`, `hermes/jobs_api.py`, `hermes/db/jobs.py`, `hermes/reviews_scraper.py`, `hermes/reviews_report.py`, `hermes/reviews_api.py` |
| Voice | `hermes/voice.py` |
| World Bank data | `hermes/worldbank.py` |
| WhatsApp | `hermes/wati.py` |
| Attachments (chat uploads) | `hermes/attachments.py` |
| File tree (File Explorer) | `hermes/files_tree.py` |
| Config | `hermes/config.py` |
| HTTP API (main) | `app.py` |
| Dashboard UI | `templates/index.html`, `static/*.js`, `static/style.css` |
| Ingestion service | `ingestion-service/app.py`, `ingestion-service/ingestion/*` |
| SQL migrations | `hermes/db/migrations/0001…0015` |
| Postman collection | `postman/Hermes_API.postman_collection.json` |
