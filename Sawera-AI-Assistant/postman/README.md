# Hermes API — Postman handoff (for frontend engineers)

This folder has everything you need to call the Hermes backend from your frontend. No backend code required to explore it — import into Postman and start calling.

## Files

| File | What it is |
|---|---|
| `Hermes_API.postman_collection.json` | All 58 API requests, organized by feature, each with inline docs (method, body fields, response shape). |
| `Hermes_Production.postman_environment.json` | Points at the **live deployed backend** — use this one. |
| `Hermes_Local.postman_environment.json` | Points at `localhost:5000` — only relevant if you're running the backend yourself. |

## Setup (2 minutes)

1. Open Postman → **Import** → drag in `Hermes_API.postman_collection.json` and `Hermes_Production.postman_environment.json`.
2. Top-right corner → environment dropdown → select **Hermes Production**.
3. Send **Main API → Health & Config → Health check**. You should get `{"status":"ok","problems":[]}`. That confirms you're connected.

Base URL is already set: `https://hermes-agent-mvp.vercel.app`.

## The one concept you need: `entity_id`

Almost every endpoint is scoped to a **business**, passed as `entity_id` (or `business` on a few endpoints — same values). These are the live, confirmed slugs:

| slug | business |
|---|---|
| `afk` | Afro Farm Holding |
| `gak` | GAK Properties LLC |
| `gif` | GIF Properties LLC |
| `gakc` | GAK Commodities Ltd |
| `nh` | Nour Hammam LLC |
| `per` | Personal & Family |

Don't hardcode this table in your app — call **Main API → Businesses → List businesses** (`GET /api/businesses`) at startup and build your business switcher from that response. It also tells you `connected: true/false` per business.

The environment's `entity_id` variable defaults to `afk` — change it in the environment editor to test a different business, or just override it per-request.

## The one gotcha: business selection isn't shared across apps

There's no server-side "currently selected business." Every request has to say which business it means via `entity_id`. If your frontend has its own business switcher, it needs to:
1. Track the selected business itself (state/localStorage — your call).
2. Send `entity_id` explicitly on every relevant call.

The one exception: once a chat `session_id` has messages, the server locks *that conversation* to whichever business it started with — `GET /api/conversations/{session_id}` returns the authoritative locked value if you need to resume one.

## Auth

- **Main API** (everything you'll use) — no auth header required today.
- **Ingestion Service** folder in the collection — you almost certainly don't need this. It's the internal background-sync worker (Gmail/Drive → search index), protected by a secret your team hasn't shared here. Ignore that folder unless someone specifically asks you to hit it.

## Typical flow for a chat feature

1. `GET /api/businesses` → populate your business selector.
2. `POST /api/chat` with `{entity_id: "<chosen slug>", message: "..."}` (omit `session_id` on the first call).
3. Save the `session_id` from the response — send it on every follow-up in the same conversation.
4. `GET /api/conversations?entity_id=...` to list past chats; `GET /api/conversations/{session_id}` to reopen one.

Full field-by-field reference for every endpoint is inline in each Postman request's Documentation tab, and also in [`../API_ENDPOINTS.md`](../docs/architecture/API_ENDPOINTS.md) at the repo root if you'd rather read it as one doc.
