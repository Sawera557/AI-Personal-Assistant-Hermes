# Hermes API Reference

Two separately-deployable FastAPI services live in this repo:

| Service | Entry point | Default port | Purpose |
|---|---|---|---|
| **Main API** | [app.py](app.py) | `5000` (`PORT` env var) | Chat agent, conversations, attachments, documents/RAG, memories, meeting copilot, connections, voice, businesses, bills |
| **Ingestion Service** | [ingestion-service/app.py](ingestion-service/app.py) | `5100` (`INGESTION_PORT` env var) | Standalone autopilot worker: syncs Gmail/Drive/etc into pgvector, exposes protected `/api/search` for retrieval |

Both are plain JSON REST APIs (a few multipart-upload and one SSE-streaming endpoint) — call them from any frontend or service. Interactive Swagger docs are auto-available at `/docs` on each running service.

A ready-to-import collection is at [postman/Hermes_API.postman_collection.json](../../postman/Hermes_API.postman_collection.json), with an environment at [postman/Hermes_Local.postman_environment.json](../../postman/Hermes_Local.postman_environment.json).

---

## Conventions

- **`entity_id` / `business`** — almost every endpoint is scoped by a *business slug* (e.g. `afk`, `nh`, `gak`) or the sentinel `"all"`. Get valid slugs from `GET /api/businesses`.
- **CORS** — controlled by `CORS_ORIGINS` (comma-separated) on the main API and `CORS_ORIGINS` on the ingestion service; defaults to `*` if unset, so any origin can call it out of the box.
- **Errors** — non-2xx responses are `{"error": "..."}` with a matching HTTP status (400/404/409/500/502).
- **Uploads** — capped at 25 MB per request on the main API (`_MAX_UPLOAD_BYTES`).
- **Auth** — the main API has no auth layer of its own (secure it at the network/CORS/reverse-proxy level, or add one, before exposing it publicly). The ingestion service protects every `/api/workers/*`, `/api/cron/*`, `/api/search`, and `/api/admin/*` route with `require_worker_auth`:
  - header `X-Cron-Secret: <CRON_SECRET>`, **or**
  - header `Authorization: Bearer <CRON_SECRET or INTERNAL_API_TOKEN>`

---

## Main API — `http://localhost:5000`

### Health & Config

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/health` | — | `{status, problems: []}` |
| GET | `/api/settings/model` | — | `{active, options}` |
| POST | `/api/settings/model` | `{"model": "claude"}` | Switch active LLM platform-wide |
| POST | `/api/admin/migrate` | — | Applies pending SQL migrations |

### Chat

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/chat` | JSON or multipart: `session_id?, entity_id?, message?, voice?, attachment_ids?[], files?[], agent_id?, agent_mode?` | Runs one turn through the agent. JSON response includes `reply, session_id, entity_id, ...`. Multipart needed only when attaching new files. With `agent_id` (uuid or slug) the turn runs as that Multi-agents agent: its prompt + mode, only its tools, the mode's autonomy/apps; a business-bound agent overrides `entity_id`. Unknown agent → 404, switched-off → 400. |
| POST | `/api/chat/stream` | same as `/api/chat` | Server-Sent Events (`text/event-stream`): `{"type":"text","text":...}`, `{"type":"tool_call",...}`, final `{"type":"done", ...same fields as /api/chat}` |

### Multi-agents (the Multi-agents tab) — `hermes/agents_api.py`

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| GET | `/api/agents` | `entity_id?` | Roster. Built-ins (`hermes/agent_presets.py`) are seeded on first read. With a business, returns its agents + all-business ones |
| GET | `/api/agents/tool-catalog` | — | `{groups:[{group, tools:[{name, description}]}], autonomies, apps}` for the editor |
| POST | `/api/agents` | `{name, system_prompt, business?, description?, icon?, tools[], modes[], default_mode?, enabled?}` | Mode: `{label, instructions?, autonomy: readonly\|draft\|full, tools?[], allowed_apps?[]}` — `key` is derived from the label |
| GET | `/api/agents/{id}` | — | `id` is the uuid or the slug |
| PATCH | `/api/agents/{id}` | any POST field | Validated against the merged agent (mode tools ⊆ agent tools) |
| DELETE | `/api/agents/{id}` | — | Custom agents only; built-ins → 400 (switch off or reset instead) |
| POST | `/api/agents/{id}/reset` | — | Built-ins only: restore the shipped prompt, tools and modes |

### Conversations

| Method | Path | Query | Notes |
|---|---|---|---|
| GET | `/api/conversations` | `entity_id?, q?` | List past conversations. Each item: `{session_id, title, created_at, updated_at, message_count, snippet, business}` — `created_at`/`updated_at` are ISO 8601 UTC strings (e.g. `"2026-07-13T14:32:07.078369+00:00"`); parse directly with `new Date(created_at)` |
| GET | `/api/conversations/{session_id}` | `entity_id?` | Full transcript replay. Turns are `{role, text}` (+`thinking`, `tool_calls` for assistant turns) — **no per-message timestamp today**, only the conversation-level dates above |
| DELETE | `/api/conversations/{session_id}` | `entity_id?` | Deletes memory + transcript |

### Attachments (chat file uploads)

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| POST | `/api/attachments` | multipart: `files[]` (required), `entity_id?` | Returns `attachment_id`s to reuse in `/api/chat` |
| GET | `/api/attachments/{attachment_id}` | `entity_id?` | Metadata |
| DELETE | `/api/attachments/{attachment_id}` | `entity_id?` | — |
| GET | `/api/media/presign` | `uri` (s3:// URI) | Short-lived HTTPS link for tool-generated media |

### Documents (Knowledge Base / RAG)

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| POST | `/api/documents` | multipart: `files[]`, `entity_id` (required, or `"all"` for global), `sensitivity_level?`, `source_system?`, `message_id?` | Ingest + chunk + embed |
| GET | `/api/documents` | `entity_id` (required, or `"all"`), `source_system?` | List (no `body_text`) |
| GET | `/api/documents/{document_id}` | — | Full detail incl. `body_text` |
| PATCH | `/api/documents/{document_id}` | `{title?, sensitivity_level?, body_text?}` | Editing `body_text` re-embeds; title-only is free |
| DELETE | `/api/documents/{document_id}` | — | Cascades chunks + best-effort S3 cleanup |

A real `entity_id` returns that business's documents **plus** global (`"all"`)
ones — so a business-scoped list always includes anything saved under `"all"`.

#### `source_system` — what the Context page's sub-tabs filter on

`source_system` is a free-form tag, and it is the only thing separating the
Context page's two document-backed sub-tabs. Both read and write through the
endpoints above:

| Sub-tab | `source_system` | What it holds |
|---|---|---|
| Files | `business_context` | Notes and reference documents |
| Skills | `business_skill` | Reusable markdown playbooks — see [skills/README.md](../../skills/README.md) |

Anything ingested another way (chat uploads, Drive sync, …) carries its own tag
and defaults to `upload`. The tag affects listing and provenance only — every
document is chunked and embedded identically, so a Skill is reachable through
`/api/search` and the agent's `search_business_knowledge` tool like any other.

```bash
# Every skill GAK Properties can see (its own, plus the global ones)
curl "$BASE/api/documents?entity_id=gak&source_system=business_skill"

# Save a new skill under one business
curl -X POST "$BASE/api/documents" \
  -F entity_id=gak -F source_system=business_skill \
  -F files=@rent-roll-normalizer.md
```

### Memories

| Method | Path | Query | Notes |
|---|---|---|---|
| GET | `/api/memories` | `entity_id?` (default `"all"`), `scope_type?` | Owner/business/project memories |
| DELETE | `/api/memories/{memory_id}` | — | — |

### Search

| Method | Path | Query | Notes |
|---|---|---|---|
| GET | `/api/search` | `q` (required), `entity_id` (required), `max_sensitivity?` (default `internal`), `top_k?` (default `8`) | Meaning search scoped to one business |

### Meeting Copilot

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| POST | `/api/meetings/copilot` | `{business (required), latest_text?, notes_so_far?, top_k?, session_id?, comprehensive?, meeting_title?, attendees?}` | Live guidance bullets + sources; persists to history |
| GET | `/api/meetings/sessions` | `business?, statuses?` (csv) | History sidebar list |
| GET | `/api/meetings/upcoming` | `business` (required), `days?` (default 14) | Calendar events annotated with copilot status |
| POST | `/api/meetings/prepare` | `{business, event_id, title, start?, description?, attendees?}` | Pre-meeting AI brief |
| POST | `/api/meetings/sessions/{session_id}/ai-notes` | `{business, meeting_title}` | Deep notes via live Gmail/Drive search. **409** `{error, skipped: "Apps access is off"}` while Apps access is off: the agent isn't called and the saved notes are left unchanged |
| GET | `/api/meetings/sessions/{session_id}` | — | Full meeting replay |
| POST | `/api/meetings/sessions/{session_id}/end` | — | Generates post-meeting summary + Fathom link |
| POST | `/api/meetings/sessions/{session_id}/fathom` | `{url}` | Manually attach recording link |
| DELETE | `/api/meetings/sessions/{session_id}/turns` | — | Wipe turns, keep session |
| DELETE | `/api/meetings/sessions/{session_id}` | — | Delete session |

### Connections (Maton app connections)

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| GET | `/api/connections` | `app?, status?` | List connections |
| POST | `/api/connections` | `{app}` | Create a connection |

### Voice

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/voice/transcribe` | multipart: `audio` (required), `entity_id?`, `context?` (`chat`\|`meeting`) | Speech → text |
| POST | `/api/voice/speak` | `{text, voice?, entity_id?}` | Text → speech; returns raw audio bytes |
| GET | `/api/voice/voices` | — | `{provider, default, voices, enabled}` |

### Session Reset

| Method | Path | Body |
|---|---|---|
| POST | `/api/reset` | `{session_id?, entity_id?}` |

### Businesses

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/businesses` | — | Each tagged with `connected` (Maton key present) |
| POST | `/api/businesses` | `{name, slug, emoji?, color?, industry?, business_id?}` | Upsert |

### Bills & Invoices

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| GET | `/api/bills` | `business_id?, status?, source?` | Also returns `insights` |
| POST | `/api/bills/upload` | multipart: `files[]`, `business_id?` | NuExtract → DB; auto-guesses business if not given |
| PATCH | `/api/bills/{bill_id}` | `{business_id?, status?, vendor?, amount?, invoice_number?, due_date?, category?, notes?}` | — |
| DELETE | `/api/bills/{bill_id}` | — | Also removes S3 file |
| GET | `/api/bills/insights` | — | — |
| GET | `/api/bills/{bill_id}/view` | — | Presigned URL / Drive link / Gmail deep link |
| GET | `/api/bills/{bill_id}/email-body` | — | Decoded Gmail body for Gmail-sourced bills |
| POST | `/api/bills/scan` | `{business?, sources?, days?}` | Scans one business's Gmail/Drive for new bills |
| POST | `/api/bills/check-payments` | — | Auto-marks bills paid from Gmail confirmations |

---

## Ingestion Service — `http://localhost:5100`

All routes below except `/api/health` require the `X-Cron-Secret` or `Authorization: Bearer` header described above.

| Method | Path | Body/Query | Notes |
|---|---|---|---|
| GET | `/api/health` | — | Public; service status |
| POST | `/api/workers/sync-business` | `{business (required), sources?, lookback_days?, max_items?, trigger?, force?}` | Sync one business |
| POST | `/api/workers/sync-all` | `{lookback_days?, trigger?, force?}` | Sync every connected business |
| POST | `/api/workers/reindex-business` | `{business (required), trigger?, limit?}` | Rebuild embeddings |
| POST | `/api/workers/index-source-item` | `{business (required), text (required), source_system?, source_ref?, title?, doc_type?, sensitivity_level?, metadata?}` | Index one arbitrary item |
| POST | `/api/search` | `{business (required), query (required), top_k?, max_sensitivity?, source_systems?, presign?}` | RAG retrieval read path |
| GET | `/api/cron/sync-all` | `lookback_days?` | GET variant for schedulers (e.g. Vercel Cron) |
| POST | `/api/admin/migrate` | — | Apply pending SQL migrations |
| GET | `/api/workers/status` | `business?` | Recent sync runs (+ per-business state) |

---

## Files added

- [postman/Hermes_API.postman_collection.json](../../postman/Hermes_API.postman_collection.json) — full collection, both services, organized into folders
- [postman/Hermes_Local.postman_environment.json](../../postman/Hermes_Local.postman_environment.json) — `base_url`, `ingestion_url`, `entity_id`, `session_id`, `cron_secret`, etc.

Import both into Postman (Import → File), select the **Hermes Local** environment in the top-right dropdown, then fill in `cron_secret` / `internal_api_token` if you need the ingestion service's protected routes.
