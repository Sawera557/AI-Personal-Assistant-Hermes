# Hermes AI Agent

A Flask-based AI assistant powered by **Claude (Opus 4.8)** that acts across your
business apps through **[Maton](https://maton.ai)**, searches the web, and answers
questions about documents you upload.

It ships with a browser chat UI **and** a JSON API.

## What it can do

- **Connect to your apps via Maton** — QuickBooks, PDF.co, Gmail, Google
  Drive / Docs / Sheets / Forms / Contacts / Calendar / Meet, and Fathom. Maton
  handles OAuth and proxies native API calls; Hermes just asks for the data.
- **Tool / function calling** — Claude decides which app + endpoint to call and
  chains calls to complete a task.
- **Conversation memory** — remembers context per chat session.
- **Web search** — Anthropic's built-in server-side web search & fetch.
- **File / document Q&A** — upload PDFs, images, CSVs, or text and ask about them.
- **Hands-free voice agent** — talk to Hermes like a phone call. Groq handles the
  voice (Whisper speech-to-text + Orpheus text-to-speech); the *brain stays
  Claude*, so voice has the **exact same app functionality** as the typed chat.

## Architecture

```
Browser chat UI ──► Flask (app.py)
                      │
                      ├─ hermes/agent.py    Claude tool-use loop (Opus 4.8)
                      ├─ hermes/tools.py     tool schemas + dispatch
                      ├─ hermes/maton.py     Maton API gateway client
                      ├─ hermes/memory.py    per-session conversation store
                      ├─ hermes/voice.py     Groq STT + TTS (voice mode only)
                      └─ hermes/config.py    env config
                      │
                      ▼
        Anthropic API  +  Maton gateway (api.maton.ai)  +  Groq (voice)
```

**Voice mode** (`static/voice.js` + `/api/voice/*`) does
`mic → Groq Whisper → /api/chat (same Claude agent) → Groq Orpheus → speaker`,
with browser-side voice-activity detection driving a continuous, hands-free call.

The agent exposes three Maton tools to Claude — `maton_request` (generic native
API passthrough for any connected app), `maton_list_connections`, and
`maton_create_connection` — plus Anthropic's `web_search` / `web_fetch`.

## Setup

1. **Python 3.10+**, then install deps:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure keys** — copy the example env file and fill it in:
   ```bash
   cp .env.example .env      # Windows: copy .env.example .env
   ```
   - `ANTHROPIC_API_KEY` — from https://platform.claude.com
   - `MATON_API_KEY` — from https://maton.ai/api-keys
   - `GROQ_API_KEY` *(optional, voice mode)* — from https://console.groq.com/keys.
     Also accept the Orpheus TTS terms once at
     https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english
     Leave the key blank to hide the voice button.

3. **Run it:**
   ```bash
   python app.py
   ```
   Open http://127.0.0.1:5000

## Connecting an app

In the sidebar, type an app slug (e.g. `quickbooks`, `google-mail`) and click
**Connect** — you'll get an OAuth link to authorize that account. You can also
just ask Hermes ("connect my QuickBooks") and it will return the link.

Known slugs: `quickbooks`, `pdf-co`, `google-contacts`, `fathom`, `google-meet`,
`google-forms`, `google-sheets`, `google-docs`, `google-drive`,
`google-calendar`, `google-mail` (Gmail). Adjust the list with `HERMES_APPS`.

## JSON API

| Method | Endpoint            | Body / notes                                              |
|--------|---------------------|-----------------------------------------------------------|
| POST   | `/api/chat`         | JSON `{session_id, message}` or multipart with `files`    |
| GET    | `/api/connections`  | `?app=&status=ACTIVE` — list Maton connections            |
| POST   | `/api/connections`  | JSON `{app}` — start OAuth, returns `{data:{url}}`         |
| POST   | `/api/reset`        | JSON `{session_id}` — clear a conversation                 |
| POST   | `/api/voice/transcribe` | multipart `audio` — Groq Whisper, returns `{text}`    |
| POST   | `/api/voice/speak`  | JSON `{text}` — returns Groq Orpheus audio (`audio/wav`)   |
| GET    | `/api/health`       | config check                                              |

Example:
```bash
curl -X POST http://127.0.0.1:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"demo","message":"List my 5 most recent Gmail messages"}'
```

## Safety

Hermes is instructed to perform **read-only** calls first and to **ask for
confirmation before any write/destructive action** (sending email, editing
QuickBooks, deleting Drive files, etc.). Your `MATON_API_KEY` and
`ANTHROPIC_API_KEY` are server-side only and never sent to the browser.

## Notes / next steps

- Conversation memory is in-process (`hermes/memory.py`). Swap in Redis or a DB
  for persistence across restarts / multiple workers.
- App slugs follow Maton's naming; if a call 404s, verify the slug and native
  path against Maton's docs.
- For production, run behind a real WSGI server (e.g. `gunicorn app:app`) and set
  a strong `FLASK_SECRET`.
