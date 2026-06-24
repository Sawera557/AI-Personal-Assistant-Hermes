# Hermes AI Agent — API Backend

A headless JSON API for **Hermes**, an AI executive assistant powered by Claude
that acts across your connected business apps (via the [Maton](https://maton.ai)
gateway), the web, and your documents — plus a voice layer (speech-to-text and
text-to-speech).

> This branch is **backend-only**. There is no UI: every route returns JSON (or
> raw audio bytes). Deploy it on its own and point any frontend, mobile app, or
> service at it.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your keys
cp .env.example .env      # then edit .env and fill in your API keys

# 3. Run the API
python app.py             # serves on http://127.0.0.1:5000
```

For production, run it under a WSGI server:

```bash
gunicorn app:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
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
| `voice` | bool | – | `true` → short, spoken-style reply |

\* `message` or at least one file is required.

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

## Project layout

```
app.py            # Flask API (routes only — no UI)
hermes/
  agent.py        # Claude tool-use loop (the agent brain)
  config.py       # Env-based configuration
  maton.py        # Maton API gateway client
  memory.py       # In-memory conversation store (swap for Redis/DB to persist)
  tools.py        # Tool definitions + dispatcher
  voice.py        # Speech-to-text / text-to-speech
requirements.txt
vercel.json       # Vercel deployment config
```

> **Note:** conversation memory is in-process (`hermes/memory.py`). For a
> multi-instance deployment, swap the store for Redis or a database — the
> `get / set / reset` interface is all the agent depends on.

---

## Deployment

- **Vercel** — `vercel.json` is included; `vercel deploy` works out of the box.
- **Any host / container** — run with `gunicorn app:app` (see Quick start) and
  set the environment variables from `.env.example`.

Set `CORS_ORIGINS` to your frontend's origin(s) in production instead of `*`.
