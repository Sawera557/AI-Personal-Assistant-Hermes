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

## Architecture

```
Browser chat UI ──► Flask (app.py)
                      │
                      ├─ hermes/agent.py    Claude tool-use loop (Opus 4.8)
                      ├─ hermes/tools.py     tool schemas + dispatch
                      ├─ hermes/maton.py     Maton API gateway client
                      ├─ hermes/memory.py    per-session conversation store
                      └─ hermes/config.py    env config
                      │
                      ▼
        Anthropic API  +  Maton gateway (api.maton.ai)
```

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

## Voice mode (read-only)

A real-time, phone-call-style voice assistant powered by **Groq** (STT + LLM +
TTS) via **Pipecat**. You talk, Hermes talks back — no push-to-talk. It can
**read and summarize** data from your connected Maton apps, but is **hard-locked
to read-only** in code (`hermes/voice_tools.py`): it cannot send, create, edit,
or delete anything. Your `GROQ_API_KEY` stays server-side; the browser only ever
connects to your own voice service, never to `api.groq.com`.

The voice service runs as a **separate process** from Flask:

```bash
# 1. Add GROQ_API_KEY to your .env  (https://console.groq.com/keys)
# 2. Install the voice deps (heavy — pulls torch via silero):
pip install -r requirements.txt -r requirements-voice.txt
# 3. Run Flask (terminal A) and the voice service (terminal B):
python app.py
python voice_server.py        # listens on ws://0.0.0.0:8765
```

Then open the web app and click **🎙 Start voice** in the sidebar. Speak a
question ("what are my most recent emails about?") and Hermes answers aloud. You
can interrupt it mid-sentence (barge-in). A status pill shows connecting /
listening / speaking.

Voice settings live in `.env` (`GROQ_STT_MODEL`, `GROQ_LLM_MODEL`,
`GROQ_TTS_MODEL`, `GROQ_TTS_VOICE`, `VOICE_WS_PORT`). The typed/Claude assistant
is unchanged and still handles any writes (with its own confirmation step).

## JSON API

| Method | Endpoint            | Body / notes                                              |
|--------|---------------------|-----------------------------------------------------------|
| POST   | `/api/chat`         | JSON `{session_id, message}` or multipart with `files`    |
| GET    | `/api/connections`  | `?app=&status=ACTIVE` — list Maton connections            |
| POST   | `/api/connections`  | JSON `{app}` — start OAuth, returns `{data:{url}}`         |
| POST   | `/api/reset`        | JSON `{session_id}` — clear a conversation                 |
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
