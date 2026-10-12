# Sawera's AI Personal Assistant — API Backend

> **Read [PLAN.md](PLAN.md) first.** This codebase is a fork of Gandi's Hermes
> (the upstream business assistant), on its way to becoming Sawera's personal
> assistant organized around **Projects** instead of businesses. The runtime
> below is still the Hermes engine — the delta so far is the dynamic Projects
> registry (PLAN §2.0) and the free-tier infra wiring (PLAN §9b).

A headless **FastAPI** JSON API for an AI executive assistant powered by Claude
that acts across your connected apps (via the [Maton](https://maton.ai)
gateway), the web, and your documents — plus a voice layer (speech-to-text and
text-to-speech).

> `/` serves a single-page web UI ([`templates/index.html`](templates/index.html)
> + [`static/`](static/)); everything under `/api/*` returns JSON (or raw audio
> bytes). The API is self-contained, so you can ignore the UI entirely and point
> any frontend, mobile app, or service at the backend.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your keys
cp .env.example .env      # then edit .env and fill in your API keys

# 3. Run the API
python app.py             # serves on http://127.0.0.1:5000 (auto-reload)
```

Interactive API docs are auto-generated:
**Swagger UI → `/docs`**, **ReDoc → `/redoc`**.

For production, run it under an ASGI server:

```bash
uvicorn app:app --host 0.0.0.0 --port 5000 --workers 2
```

### Required configuration

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude — the agent's brain |
| `MATON_API_KEY` | ✅ | Maton gateway — connected app access |
| `OPENAI_API_KEY` | for voice | Speech-to-text + text-to-speech |
| `CORS_ORIGINS` | optional | Allowed frontend origins (default `*`) |

See [`.env.example`](.env.example) for the full list (model, voice provider,
voices, app list, etc.).

---

## The `entity_id` field

Every agent/voice endpoint accepts an optional **`entity_id`** in the request
body. It lets a single backend serve many tenants/users while keeping their
conversation memory **isolated** — internally the store is keyed by
`entity_id` + `session_id`, so two callers with the same `session_id` but
different `entity_id` never see each other's history. It is also echoed back in
responses so the client can correlate them.

- Omit it for a single-tenant deployment — everything still works.
- Pass a stable per-customer / per-user id for multi-tenant isolation.

---

## Endpoints

Base URL: `http://<host>:<port>`  •  All bodies are JSON unless noted.

### `GET /api/health`
Liveness + configuration check.

```json
{ "status": "ok", "problems": [] }
```
`problems` lists any missing required keys (empty when healthy).

---

### `POST /api/chat`
Run one user turn through the Claude + Maton agent. Accepts **JSON** or
**multipart/form-data** (use multipart to attach files — PDFs, images, text).

**JSON body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `message` | string | ✅* | The user's message |
| `session_id` | string | – | Conversation id; auto-generated if omitted |
| `entity_id` | string | – | Tenant/user scope (see above) |
| `attachment_ids` | string[] | – | Ids from `POST /api/attachments` to include |
| `voice` | bool | – | `true` → short, spoken-style reply |

\* `message`, at least one file, or at least one `attachment_id` is required.

```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "What meetings do I have tomorrow?",
        "session_id": "abc123",
        "entity_id": "customer-42"
      }'
```

**With file attachments (multipart):**

```bash
curl -X POST http://localhost:5000/api/chat \
  -F "message=Summarize this PDF" \
  -F "session_id=abc123" \
  -F "entity_id=customer-42" \
  -F "files=@/path/to/report.pdf"
```

**Response**

```json
{
  "reply": "You have two meetings tomorrow...",
  "thinking": "…summarized reasoning, or null…",
  "tool_calls": [ { "tool": "maton_request", "input": {}, "ok": true } ],
  "stop_reason": "end_turn",
  "session_id": "abc123",
  "entity_id": "customer-42"
}
```

---

### `POST /api/attachments`
Upload one or more files **once** and get back stable `attachment_id`s, then
reference them from `/api/chat` (via `attachment_ids`) without re-uploading.
Handy when the same document is used across several turns, or when the upload
and the chat happen in separate requests. Supports PDFs, images, and text-like
files (csv, json, md, code, …).

Attachments are scoped to `entity_id` — only the same entity can reference,
read, or delete them.

**multipart/form-data**

| Field | Type | Required | Notes |
|---|---|---|---|
| `files` | file(s) | ✅ | One or more files (repeat the field) |
| `entity_id` | string | – | Tenant/user scope (see above) |

```bash
curl -X POST http://localhost:5000/api/attachments \
  -F "entity_id=customer-42" \
  -F "files=@/path/to/report.pdf" \
  -F "files=@/path/to/chart.png"
```

**Response**

```json
{
  "attachments": [
    { "attachment_id": "9f1c…", "filename": "report.pdf", "type": "document", "bytes": 18432 },
    { "attachment_id": "a73e…", "filename": "chart.png",  "type": "image",    "bytes": 5120 }
  ],
  "entity_id": "customer-42"
}
```

Then send them through chat:

```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Summarize the report and describe the chart.",
        "entity_id": "customer-42",
        "attachment_ids": ["9f1c…", "a73e…"]
      }'
```

A reference to an unknown or non-owned id returns `400 Unknown attachment_id`.

### `GET /api/attachments/{attachment_id}?entity_id=…`
Return an attachment's metadata. `404` if it doesn't exist or belongs to another
entity.

```json
{ "attachment_id": "9f1c…", "filename": "report.pdf", "type": "document", "bytes": 18432, "entity_id": "customer-42" }
```

### `DELETE /api/attachments/{attachment_id}?entity_id=…`
Delete an attachment. Returns `{ "status": "deleted", "attachment_id": "9f1c…" }`,
or `404` if not found / not owned.

---

### `POST /api/voice/transcribe`
Speech → text. Post a recorded audio clip; returns the transcript. Send the
returned text back through `/api/chat` to run the full agent.

**multipart/form-data**

| Field | Type | Required | Notes |
|---|---|---|---|
| `audio` | file | ✅ | webm, ogg, wav, mp4, mpeg/mp3, m4a, flac |
| `entity_id` | string | – | Tenant/user scope; echoed back |

```bash
curl -X POST http://localhost:5000/api/voice/transcribe \
  -F "audio=@clip.webm" \
  -F "entity_id=customer-42"
```

**Response**

```json
{ "text": "what meetings do I have tomorrow", "entity_id": "customer-42" }
```

---

### `POST /api/voice/speak`
Text → speech. Returns raw audio bytes (`audio/wav`). Markdown/URLs/code are
stripped first so the reply reads cleanly aloud.

**JSON body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | ✅ | Text to speak |
| `voice` | string | – | Override the default voice (see `/api/voice/voices`) |
| `entity_id` | string | – | Echoed back in the `X-Entity-Id` response header |

```bash
curl -X POST http://localhost:5000/api/voice/speak \
  -H "Content-Type: application/json" \
  -d '{ "text": "You have two meetings tomorrow.", "voice": "alloy", "entity_id": "customer-42" }' \
  --output reply.wav
```

---

### `GET /api/voice/voices`
List the TTS voices available for the active provider, plus the current default.
Use this to let the user **change voice**, then pass the chosen name as `voice`
to `/api/voice/speak`.

```json
{
  "provider": "openai",
  "default": "alloy",
  "voices": ["alloy", "ash", "ballad", "coral", "echo", "fable",
             "onyx", "nova", "sage", "shimmer", "verse"],
  "enabled": true
}
```

---

### `GET|POST /api/connections`
Manage Maton app connections.

- **GET** — list connections. Optional query params: `app`, `status`
  (`ACTIVE` | `PENDING` | `FAILED`).
- **POST** `{ "app": "google-mail" }` — start an OAuth connection; the response
  contains a `url` the user opens to authorize the app.

---

### `POST /api/reset`
Clear a conversation's history.

```json
{ "session_id": "abc123", "entity_id": "customer-42" }
```

Returns `{ "status": "reset" }`. Scope it the same way you created it — pass the
same `entity_id` you used for `/api/chat`.

---

## Typical voice flow

```
record audio ──► POST /api/voice/transcribe ──► transcript
transcript   ──► POST /api/chat (voice=true)  ──► reply text
reply text   ──► POST /api/voice/speak        ──► spoken audio
```

The agent's brain is always Claude (`run_turn`), so the voice path has the exact
same multi-app capabilities as the typed chat — the voice provider only does the
listening (STT) and speaking (TTS).

---

## Business skills

A **skill** is a reusable markdown playbook scoped to one business — a rent-roll
normalizer for the property entities, a crop calendar for the farm, a deep-research
pipeline shared by all of them. They show up under **Context → Skills** in the UI
and the agent can pull one up mid-conversation.

There is no separate skills API. A skill is an ordinary document tagged
`source_system = "business_skill"`, which is the only thing distinguishing it
from a Context file (`business_context`) — see the
[`source_system` table](docs/architecture/API_ENDPOINTS.md#source_system--what-the-context-pages-sub-tabs-filter-on).
Because it goes through the normal ingest path it is chunked and embedded too,
so `search_business_knowledge` can reach it like any other document.

[`skills/`](skills/README.md) holds an openly-licensed starter set sourced from
public skill repos, three per business plus three global ones, with
[`skills/MANIFEST.json`](skills/MANIFEST.json) recording where each came from:

```bash
# Re-download every skill from its upstream repo
python scripts/fetch_business_skills.py

# See what would be loaded, without touching the database
python scripts/seed_business_skills.py --dry-run

# Load them (embeds each one, so it costs API calls)
python scripts/seed_business_skills.py --base-url http://127.0.0.1:5000
```

Seeding twice duplicates rows — `/api/documents` has no upsert. Pass `--replace`
to drop a same-titled skill in the same scope first, or `--scope gak nh` to
re-seed just those businesses. You can also add one by hand from the Skills
sub-tab, which writes under the same tag.

---

## Project layout

```
app.py            # FastAPI app (routes only)
hermes/
  agent.py        # Claude tool-use loop (the agent brain)
  attachments.py  # In-memory attachment store (swap for Redis/object storage)
  config.py       # Env-based configuration
  maton.py        # Maton API gateway client
  memory.py       # In-memory conversation store (swap for Redis/DB to persist)
  tools.py        # Tool definitions + dispatcher
  voice.py        # Speech-to-text / text-to-speech
skills/           # Per-business skill library + MANIFEST.json (see above)
scripts/          # Operational scripts — fetch/seed skills, tests, one-off pulls
static/           # Front-end assets for the web UI
templates/        # index.html — the single-page UI
requirements.txt
vercel.json       # Vercel deployment config
```

> **Note:** conversation memory (`hermes/memory.py`) and uploaded attachments
> (`hermes/attachments.py`) are held in-process. For a multi-instance
> deployment, swap these stores for Redis / object storage / a database — their
> small public interfaces are all the API depends on.

---

## Deployment

- **Vercel** — `vercel.json` is included; the ASGI `app` is detected automatically.
- **Any host / container** — run with `uvicorn app:app` (see Quick start) and
  set the environment variables from `.env.example`.

Set `CORS_ORIGINS` to your frontend's origin(s) in production instead of `*`.
