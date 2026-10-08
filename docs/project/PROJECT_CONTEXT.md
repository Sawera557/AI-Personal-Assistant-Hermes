# Hermes — Full Project Context

A single reference to *what this project is*, *what it can actually do right now*,
and *what only looks like it works*. Last full audit: **2026-07-27** (verified
against the live production database and a running app instance).

For recurring automation see [SCHEDULED_TASKS.md](../features/SCHEDULED_TASKS.md).
For the 25 Jul fabrication incident see [ADR-001](../architecture/ADR-001-fabrication-truncation-fix.md).

> **Status legend used throughout**
> **LIVE** — verified working in production · **SHIPPED** — committed and wired, not
> production-verified · **WORKING TREE** — uncommitted, not deployed ·
> **AUTHORED, NOT LOADED** — content written, never reached the database ·
> **DOC-ONLY** — described in a markdown file, no code exists

---

## 0. Read this first — five things that are not what they look like

| # | Finding | Evidence |
|---|---|---|
| 1 | **Context packs and playbooks are dead in production.** The `context_packs`, `playbooks` and `task_playbooks` tables **do not exist**. Migration `0017` was never applied. `packs_for()` swallows the error into a `log.warning` and returns `""`, so **every turn today runs with an empty BUSINESS CONTEXT PACK block.** The seven packs authored in `HERMES_CONTEXT_PACKS.md` — including the "Hard rules (non-negotiable)" anti-fabrication rules — have never been in the prompt. | Verified: `information_schema.tables` → `[]`; `hermes/context_packs.py:78-80` |
| 2 | **The knowledge base is ~9 documents, not 15,000.** `document_chunks` holds **41,749** rows but only **124** have a non-NULL `embedding`, and `hermes/vector_store.py:177` filters on exactly that. So `search_business_knowledge` reaches **0.3%** of ingested content — essentially only the hand-written Business Context notes. The 15,380 ingested emails/Drive files are invisible to the agent. | Verified: live counts |
| 3 | **The autonomy ceiling is not enforced.** `stream_turn`, `run_turn` and `dispatch()` take **no autonomy argument**. A `readonly` scheduled task with `google-mail` allowed is still handed `send_email`. Autonomy is a *prompt string*. | `hermes/agent.py:526,743`; `hermes/tools.py:2011`; `hermes/scheduler.py:277-301` |
| 4 | **Chat write-confirmation is also prompt-only** — one system-prompt bullet. The single structurally-enforced write gate in the codebase is `email_with_attachment`'s draft-then-confirm flow. | `hermes/agent.py:115-119`; `hermes/email_attachments.py:208-286` |
| 5 | **Schema drift.** Production has `0016_scheduled_task_notify_to.sql` applied — a file that **exists in no branch of this repo**, adding a `notify_to` column **no code reads**. Meanwhile `0016_auth.sql` (on unmerged `feature/auth`) is *not* applied. `schema_migrations` is not a reliable ledger. | Verified: applied-migrations list |

---

## 1. What Hermes is

An AI executive assistant for **Gandi**, a founder running a multi-entity
portfolio. It answers in chat and — through the **Maton** API gateway — reads and
acts across each business's connected accounts (email, calendar, drive,
accounting, messaging, meetings, notes). The same agent also runs **unattended on
a schedule**.

**Stack:** FastAPI (*not* Flask — zero `@app.route` decorators; `app = FastAPI(...)`
at `app.py:141`), Jinja2 + vanilla JS frontend, AWS RDS Postgres + pgvector, S3
for file bytes, deployed as a single Vercel Python serverless function.

### Dashboard — 7 tabs

| Tab | What it does | Status |
|---|---|---|
| **AI Agent** | The chat assistant, voice-capable. Model + reasoning-effort pickers in the sidebar. | LIVE |
| **Billing & Invoices** | Upload a bill → OCR extraction → structured invoice records. | LIVE |
| **Meeting Copilot** | Meeting prep and guidance assembled from your data. | LIVE |
| **Business Context** | Per-business knowledge base — hand-written notes and uploaded files. | LIVE |
| **Scheduled Tasks** ⏰ | Recurring unattended agent runs. See §5. | LIVE (21 tasks) |
| **File Explorer** | Read-only two-pane browser over the business's raw S3 objects. | LIVE |
| **Jobs** | Registered Python functions on a cron. See §5. | LIVE (1 job) |

⚠️ Non-functional UI present today: the Billing sidebar's "Coming soon" chip and
static "Recent Bills" panel, and the "Set Reminder" button on bill insights.

---

## 2. Businesses (entities)

Every request is scoped to one business (locked per conversation) or **All
Businesses**. Each has its own Maton key, so one business can never read
another's data.

| Slug | Business | Notes |
|---|---|---|
| `afk` | Afro Farm Holding (Kenya) | Farm ops, ~54 staff payroll (KES), GLOBALG.A.P., market prices |
| `gak` | GAK Properties LLC | Landlord — leases, rent, tenants (720 E Lake) |
| `gif` | GIF Properties LLC | Landlord — 2609 15th Ave S, 1229 E Lake, 1230 Logan |
| `gakc` | GAK Commodities Ltd | Trading / commodities |
| `nh` | Nour Hammam LLC | Spa pre-launch (buildout at 720 E Lake) |
| `per` | Personal & Family | Personal inbox, father's care, prayer/halal-aware scheduling |

`per` uses the shared/default Maton key; the other five each have a dedicated one
(`hermes/businesses.py:15-22`, `hermes/config.py:50-57`). Two `entities` rows —
`comfort_services`, `kenya_ventures` — are deliberately archived and unmapped.
Unchanged since the last audit.

---

## 3. Connected apps (via Maton)

Maton holds each business's OAuth tokens and proxies calls to each app's native
REST API. Reached either through a **dedicated tool** or the universal
`maton_request`. The catalog is **16 slugs** (`hermes/config.py:217-222`):

`google-mail` · `google-calendar` · `google-drive` · `google-sheets` ·
`google-docs` · `google-forms` · `google-contacts` · `google-meet` ·
`quickbooks` · `notion` · `jira` · `zoom` · `zoom-admin` · `wati` · `fathom` ·
`pdf-co`

Jira was the last addition (commit `0e4c412`, 20 Jul). Non-Maton sources:
**World Bank** (public macro indicators, no key) and **web search/fetch** when
`HERMES_WEB_SEARCH` is on.

> ⚠️ **`.env.example:45` ships only 14 slugs**, omitting `notion` and `zoom-admin`.
> Copying it verbatim silently drops two apps.

> ⚠️ **Connector status is inert.** `ACTIVE|PENDING|FAILED` is a *request filter*
> on one tool schema (`hermes/tools.py:104-108`), read by no code. The only
> code-enforced connector check is the missing-key refusal in `_resolve_client`.
> A PENDING connector does not stop the agent asserting facts in that domain —
> this is half of the 25 Jul incident.

---

## 4. Capabilities

**30 tools total** — 28 local + 2 Anthropic server-side.

| Domain | Tools | Notes |
|---|---|---|
| Universal gateway | `maton_request`, `maton_list_connections`, `maton_create_connection` | Any endpoint of any connected app |
| Email | `send_email`, `email_with_attachment` | `email_with_attachment` is the only structural write gate (draft → approve → send) |
| Calendar | `calendar_list_upcoming` | Read-only; writes go through `maton_request` |
| Drive & docs | `drive_read_file`, `read_document` | PDFs/images returned as real content |
| Meetings | `zoom_send_invite`, `zoom_list_meetings` | Fathom is best-effort — see below |
| Jira | `jira_search_issues`, `jira_get_issue`, `jira_create_issue`, `jira_assign_issue` | |
| WhatsApp | 8 × `wati_*` | Contacts, messages, send text/file, assign operator, media |
| Knowledge | `search_business_knowledge` | ⚠️ Reaches only 124 of 41,749 chunks — see §0.2 |
| Files | `create_file` | DOCX/XLSX/PDF. **Markdown is not interpreted — it renders literally** |
| Data | `worldbank_data` | Authoritative agriculture/livestock indicators, 12h cache, prewarmed at startup |
| Scheduling | `create_scheduled_task`, `list_scheduled_tasks`, `update_scheduled_task`, `delete_scheduled_task` | |
| Web | `web_search`, `web_fetch` | Server-side, gated on `HERMES_WEB_SEARCH` |

**Agent core** (`hermes/agent.py`): one provider-agnostic tool-use loop
(`stream_turn:526`, drained by `run_turn:743`) running on Anthropic **or** OpenAI.
History is always stored in Anthropic block shape and translated per-call, so
switching models mid-conversation keeps history. Budget: 8,000 output tokens per
turn, 12 tool round-trips.

**Model picker** — platform-wide (not per-browser), persisted in Postgres
(`hermes/models.py:68`). Note the default `claude-sonnet-5` is *not* in
`MODEL_OPTIONS`, so it appears as "(current default)" until someone switches.

**Reasoning effort** (commit `34f80a8`, 24 Jul) — Auto | Low | Medium | High,
saved to localStorage, honored by both chat endpoints. Auto infers per turn from
attachments, depth keywords and length.
⚠️ Effort reaches **Claude only** — skipped for Haiku and continuations, and
`_openai_turn` takes no effort argument at all, so **the picker is a no-op on GPT
models.**

**Auto-grow output tokens** — a reply stopped by the token cap with no tool call
is resumed *in place* with a doubled budget up to `MAX_TOKENS_CEILING`, thinking
blocks stripped for the prefill. Zero test coverage.

**Fathom is speculative.** Its own docstring says the Maton catalog path "isn't
confirmed"; it probes 4 candidate paths and 5 link field names and returns `None`
on failure. Always offer a paste-your-own-URL fallback (`hermes/fathom.py:1-9`).

### The four context layers (easily confused)

| Layer | What | Reaches the prompt | Status |
|---|---|---|---|
| **Context packs** | Curated markdown, global + per business, labelled authoritative over everything else | Injected every turn | ❌ **AUTHORED, NOT LOADED** — table absent |
| **Playbooks** | Written procedures for scheduled runs | Injected into a scheduled run via `task_playbooks` | ❌ **AUTHORED, NOT LOADED** — and no API/UI to link one; only a manual `python -c` |
| **Persistent memory** | LLM-extracted after each turn into pgvector | Pushed in every turn | ✅ **LIVE — 170 rows** |
| **Business knowledge base** | `documents`/`document_chunks`, the Context tab | Pull-only — model must call the tool | ⚠️ **LIVE but 0.3% reachable** |

---

## 5. Scheduled Tasks vs Jobs — two different systems

| | Scheduled Task | Job |
|---|---|---|
| Runs | An agent **prompt** | A registered Python **function** |
| Scope | Locked to one business | System-wide, no business scope |
| Config | Prompt + apps + schedule + autonomy | Registry entry + cron |
| Today | **21 tasks** (13 never run, 6 ok, 2 error) | **1 job** — Google Reviews scraper (11 ok / 5 error) |
| Fires via | `/api/scheduled-tasks/run-due` | `/api/jobs/run-due` |

Both are **pull-based**. Vercel cron hits the `/run-due` endpoints **every 15
minutes**, so a task set for "07:00" actually runs within a 15-minute window. The
in-process poller auto-disables on Vercel (frozen instances between requests).

Scheduled Tasks also has, undocumented in the old version of this file: atomic
claim-before-run (stops multi-fire), stale-run reaping, and a one-day-per-run
date-window backfill (migration 0013).

**Schedule shapes:** `daily`, `weekdays`, `weekly` (0=Mon…6=Sun), `hourly`, in the
task's own timezone.

**Autonomy levels** — `readonly` (default) / `draft` / `full`.
🚨 **Not enforced in code.** See §0.3. Treat these as documentation of intent, not
a safety boundary.

> 🚨 If neither `SCHEDULER_SECRET` nor `CRON_SECRET` is set, both `/run-due`
> endpoints are **fully open** and anyone can trigger every due task — including
> `full`-autonomy ones (`app.py:1787-1800`).

You can create tasks conversationally ("Every morning at 7 give me a farm briefing
from AFK Gmail and Calendar"). Hermes infers schedule, business, apps and autonomy,
and confirms when ambiguous. It will not pick `full` unless clearly asked.
Unattended runs cannot create tasks — only a live chat turn can.

---

## 6. Running it locally

```bash
pip install -r requirements.txt   # note: pytest is NOT in here
cp .env.example .env              # then fill keys — see below
python app.py                     # uvicorn on 0.0.0.0:$PORT (default 5000)
```

Dashboard at `/`, Swagger at `/docs`.

**Env vars that actually gate features** (the README's table omits all of these):

| Var | Without it |
|---|---|
| `DBRDS_HOST` (+`_PORT`/`_USER`/`DB_NAME`) | `connection.py:35` raises; every persistence feature dies |
| `S3_BUCKET` | No File Explorer, no bill originals, no generated-file downloads |
| `NUEXTRACT_API_KEY` | Bill extraction fails — **no fallback on this path** |
| `SERPAPI_API_KEY` | The only registered job can't run |
| `SCHEDULER_SECRET` / `CRON_SECRET` | `/run-due` endpoints are open to the world |

`HERMES_DISABLE_DB=1` forces the in-memory conversation store but does **not**
stop jobs/bills/tasks hitting Postgres. Undocumented in `.env.example` entirely:
`CONTEXT_PACK_MAX_CHARS`, `CONTEXT_PACK_CACHE_TTL`, `MEMORY_BLOCK_MAX_CHARS`,
`DB_POOL*`.

### Landmines, ranked

1. **A fresh DB has no Billing schema.** `migrations/001_bills.sql` (top-level) is
   run by *no* runner — both target `hermes/db/migrations/*.sql`. Production got
   those tables out-of-band.
2. **`graphify` does not exist.** Both `CLAUDE.md` files mandate it and a
   `PreToolUse` hook enforces the mandate on every bash call. No binary, no
   module, empty `graphify-out/wiki/`. Fix or remove the instruction.
3. **A fresh clone has a broken import** — committed `hermes/agent.py:551` imports
   untracked `hermes.context_packs`. It degrades silently via `try/except`.
4. **`pytest tests/` is RED** — 2 failed, 23 passed. Both failures are untracked
   visual tests asserting against a "Command Center" UI that doesn't exist on this
   branch. Delete them first.
5. **Chat attachments are a per-process dict** (cap 500, FIFO). On Vercel an
   `attachment_id` may not resolve on the next request.
6. **`hermes/memory.py` is the conversation store, not persistent memory.**
   Persistent memory is `memory_extract.py` / `memory_retrieve.py` /
   `db/memories.py`. The name collision confuses everyone.
7. **`hermes/maton.py:209-226`** truncates every response at 20k chars by
   serialise-then-slice — a truncated list comes back as a *dict*, and nothing
   anywhere reads `_truncated`. Half of the 25 Jul incident, still live.
8. **File Explorer ships the whole bucket to every browser** — `static/files.js:309`
   always requests `entity_id=all`, and there is no auth layer.
9. **73 branches**, many stale. Current: `truncation-issue-fix`.
10. `hermes_upgrade_patch.py` edits live files and leaves `*.bak_*` siblings.
    `.gitignore` covers `.env.*` but **not** `hermes/*.bak_*` — three are one
    `git add .` from being committed.

---

## 7. Code map

| Area | Files |
|---|---|
| Agent loop | `hermes/agent.py` (`stream_turn`, `run_turn`, `_system_prompt`) |
| Tools & dispatch | `hermes/tools.py` (`build_tools`, `dispatch` — the single choke point) |
| Models & effort | `hermes/models.py`, `static/model.js` |
| Maton gateway | `hermes/maton.py`, per-business keys in `hermes/config.py` |
| Businesses | `hermes/businesses.py` |
| Context packs / playbooks | `hermes/context_packs.py`, `hermes/db/context_packs.py`, `hermes/db/playbooks.py`, `load_packs.py`, `load_playbooks.py` |
| Memory | `hermes/memory_extract.py`, `hermes/memory_retrieve.py`, `hermes/db/memories.py` |
| Retrieval | `hermes/vector_store.py`, `hermes/search.py`, `hermes/ingest.py` |
| Scheduled Tasks | `hermes/scheduler.py`, `hermes/db/scheduled_tasks.py` |
| Jobs | `hermes/job_runner.py`, `hermes/jobs_api.py`, `hermes/db/jobs.py` |
| Reviews | `hermes/reviews_scraper.py`, `hermes/reviews_report.py`, `hermes/reviews_api.py` |
| Bills | `hermes/bills.py`, `hermes/extract.py` (OCR ladder + Claude-vision fallback) |
| Meetings | `hermes/meeting_copilot.py`, `hermes/calendar.py`, `hermes/fathom.py` |
| Messaging | `hermes/wati.py` |
| Files | `hermes/storage.py`, `hermes/files_tree.py`, `hermes/documents.py` |
| Voice | `hermes/voice.py`, `static/voice.js` |
| HTTP API | `app.py` (~67 routes incl. mounted routers) |
| UI | `templates/index.html`, `static/*.js`, `static/style.css` |

**Not project infrastructure:** `scripts/_*.py` (untracked one-off Nour Hammam
scrapers), `.agents/skills/*` (Claude Code design skills, unrelated to the
runtime), the three `*-report.html` files (generated artifacts).

**`ingestion-service/`** is a separate git repo and separate Vercel deploy sharing
the same RDS. No commit since 2026-07-03; nothing in the main app calls it. It
writes vectors to Pinecone and leaves `document_chunks.embedding` NULL — which is
why its 15,380 documents are unsearchable (§0.2). `pinecone>=5.0` is in
`requirements.txt` but **imported nowhere** in the main app.

---

## 8. Which docs to trust

| File | Verdict |
|---|---|
| **PROJECT_CONTEXT.md** (this) | Current — audited 2026-07-27 against prod |
| **SCHEDULED_TASKS.md** | ✅ Current and trustworthy — the best doc in the repo |
| **ADR-001-fabrication-truncation-fix.md** | ✅ Current diagnosis, **zero implementation** |
| **PROJECT_SCOPE.md** | Mostly current, overlaps this file heavily. Stale on one point (claims no connection pooling; a pool exists at `db/connection.py:147-151`). ⚠️ Decide whether it survives — two overlapping 400-line docs is worse than one |
| **HERMES_UPGRADE_PLAN.md** | Aspirational plan, self-labelled "the code is truth" |
| **HERMES_CONTEXT_PACKS.md** / **HERMES_PLAYBOOKS.md** | Authored content, **never loaded** (§0.1) |
| **HERMES_*_TASK_PROMPTS.md** | Prompt libraries, not documentation. Specify `/api/context-packs/*` routes that were never built |
| **API_ENDPOINTS.md** | ~30% incomplete — omits all scheduled-task, job and review routes |
| **DATABASE.md** | ❌ Actively misleading — presents Pinecone as the live search brain, references `scripts/init_pinecone.py` and `pinecone_store.py`, neither of which exists. Delete or rewrite |
| **DATA_ARCHITECTURE_REPORT.md** | Superseded — cites a module that no longer exists |
| **MEETINGS_COPILOT_IMPLEMENTATION.md** | Partially true — `get_guidance_comprehensive()` exists and the API accepts it, but "comprehensive" appears nowhere in `static/meeting.js`, so the UI can never request it |
| **README.md** | ❌ Oldest and most wrong — says "backend-only, there is no UI" (`:8-10`). There is a full 7-tab dashboard |

### Known-false statements in shipped files

- `README.md:8-10` — "This branch is backend-only. There is no UI."
- `.env.example:19-21` — claims blank `NUEXTRACT_API_KEY` falls back to Claude
  vision. True for the chat path, **false for the Billing upload path**, which
  raises and drops the bill.
- `.env.example:45` — app catalog is missing `notion` and `zoom-admin`.
