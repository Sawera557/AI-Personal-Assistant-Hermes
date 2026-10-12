# Hermes: An AI Command Center for a Six-Business Founder

**A detailed case study by Sawera Khadium**
June – October 2026 · Six businesses in two countries · 40+ apps connected to Claude · three custom MCP servers · $31,000/year saved

> **About this document.** This is the long-form case study. The shorter version is
> [CASE_STUDY_AI_COMMAND_CENTER.md](CASE_STUDY_AI_COMMAND_CENTER.md). I use real entity
> names because this is my working record; for public use, apply
> [Appendix A](#appendix-a--anonymization). Every claim about the code was checked against
> the repository on 2026-10-09. Where something is built but not switched on, or was
> reported by a document rather than measured, I say so. The status legend is in
> [Appendix B](#appendix-b--status-legend).

---

## Contents

1. [At a glance](#1-at-a-glance)
2. [Executive summary](#2-executive-summary)
3. [The client and his operating reality](#3-the-client-and-his-operating-reality)
4. [The problem: three walls](#4-the-problem-three-walls)
5. [Timeline](#5-timeline)
6. [Architecture](#6-architecture)
7. [Business isolation: six sealed compartments](#7-business-isolation-six-sealed-compartments)
8. [The agent core](#8-the-agent-core)
9. [The dashboard and Hermes UI v2](#9-the-dashboard-and-hermes-ui-v2)
10. [Claude Artifact dashboards and in-app Artefacts](#10-claude-artifact-dashboards-and-in-app-artefacts)
11. [Billing, invoices and the register](#11-billing-invoices-and-the-register)
12. [Meeting Copilot](#12-meeting-copilot)
13. [Scheduled Tasks: unattended work with a real ceiling](#13-scheduled-tasks-unattended-work-with-a-real-ceiling)
14. [Multi-agents](#14-multi-agents)
15. [Jarvis: the real-time voice agent](#15-jarvis-the-real-time-voice-agent)
16. [The knowledge and memory layer](#16-the-knowledge-and-memory-layer)
17. [Three custom MCP servers](#17-three-custom-mcp-servers)
18. [Integrations: WhatsApp, Twilio, QuickBooks, Jira](#18-integrations-whatsapp-twilio-quickbooks-jira)
19. [Trust engineering: from a fabricated payroll figure to four code-level guardrails](#19-trust-engineering)
20. [Auditing my own work](#20-auditing-my-own-work)
21. [Cost and context rot](#21-cost-and-context-rot)
22. [Production incidents and what they taught](#22-production-incidents-and-what-they-taught)
23. [Testing and quality](#23-testing-and-quality)
24. [Results](#24-results)
25. [Decisions I'd defend](#25-decisions-id-defend)
26. [What is still open](#26-what-is-still-open)
27. [Lessons](#27-lessons)
28. [Appendices](#appendix-a--anonymization)

---

## 1. At a glance

| | |
|---|---|
| **Client** | A founder running six businesses in Kenya and the US: farming, two property companies, commodities, a spa, and a personal/family office |
| **Role** | Lead engineer and AI systems architect: design, build, deploy, audit, documentation |
| **Team** | Me, plus Haider Ali (guardrails and artefacts, August 2026) |
| **Duration** | 22 June 2026 (first commit) to 8 October 2026 (latest release), ongoing |
| **Stack** | FastAPI · AWS RDS Postgres 17.7 + pgvector · Pinecone · S3 · Jinja2 + vanilla JS (no build step) · Vercel serverless · Claude, NVIDIA NIM, OpenAI and Gemini models · Maton API gateway · FastMCP |
| **Main app size** | ~26,500 lines of Python in `hermes/` + 2,645 in `app.py` · 10,151 lines of JS · 5,380 of CSS · 4,389 of templates |
| **Surface** | 95 HTTP routes · 31 agent tools (+2 server-side web tools, +2 agent-only tools) · 29 SQL migrations · 10+ dashboard views |
| **Satellite services** | Ingestion service · Pinecone MCP · NuExtract MCP · GitHub→Jira automation · demo-video pipeline |
| **Quality** | 523 test functions in 34 files; 2,390 tests passed on the most recent full run (8 Oct 2026) |
| **History** | 145 commits on the main line |

**Headline outcomes**

- **$31,000 a year saved** by the portfolio-wide money-in/money-out dashboard, with every figure checked against QuickBooks
- **40+ apps** reachable by Claude through one MCP gateway, with a separate credential for each business
- **The rent cycle automated**: invoices, late fees and tenant notices
- **Managers made self-sufficient**: farm attendance and payouts, tenancy, and spa outreach are handled in dashboards instead of routed through the founder
- **Scanned documents and floormaps made readable**: batch OCR runs **4.5× faster** than sequential
- **Input tokens on a "Hi" cut from 20,281 to 148** with the Business Context toggle
- **The anti-fabrication bar met in code**: approvals bound to exact arguments, autonomy enforced as a capability gate, and a secret and card-number egress filter

---

## 2. Executive summary

The client ran six legally separate businesses across two countries. Each had its own email, calendar, Drive, accounting, messaging and meeting tools. He had already tried to run them through Claude himself and hit three walls:

- **Cost.** More than $1,000 a week in usage credits on top of a $200/month plan, with one week above $3,500.
- **Quality.** The model got worse as he installed more into it.
- **Trust.** On 25 July 2026 the agent invented a payroll figure and stated it with full confidence.

I built **Hermes**, a command center that puts one AI agent in front of all six businesses. It runs as a seven-tab (now more than ten-view) web application plus a set of satellite services and three custom MCP servers. Each business is isolated by credential, not by prompt. Every money figure is checked against QuickBooks. Unattended runs carry a hard autonomy ceiling. Since October the agent also has a real-time voice front end and a team of specialist sub-agents.

The fabrication incident shaped everything that followed. I set a bar of **zero hallucination, zero assumptions, provenance first**, and then found that my own system didn't meet it. The anti-fabrication rules had been written but had never reached the prompt. The knowledge base could reach 0.3% of what had been ingested. The safety tiers were paragraphs, not controls. I documented each of these in the open with evidence, then closed them one by one, moving each control from the prompt into code.

The result is a system the founder and his managers use every day, with a measured financial return. It also comes with an honest record of what is live, what is built but switched off, and what is still open.

---

## 3. The client and his operating reality

### Six businesses, two countries

| Slug | Business | Country | Shape of the work |
|---|---|---|---|
| `afk` | **Afro Farm Holding** | Kenya | A million-dollar, 128-acre farm with ~54 staff paid in KES. GLOBALG.A.P. certification prep, market prices, three field reports a day |
| `gak` | **GAK Properties LLC** | US | The heaviest entity: 7 Minnesota commercial properties and ~10 active tenants |
| `gif` | **GIF Properties LLC** | US | 3 Minnesota properties, one vacant, plus a long-running billboard lease |
| `gakc` | **GAK Commodities Ltd** | Kenya-linked | Deliberately low activity, watched for anything new |
| `nh` | **Nour Hammam LLC** | US | A women-only hammam before launch, with buildout at a GAK property (a related-party lease) |
| `per` | **Personal & Family** | US | Family logistics, personal inbox, prayer- and halal-aware scheduling |

A seventh, Comfort PCA, handles protected health information. The owner explicitly authorized it for indexing, but it has no business slug or gateway key yet, so it cannot be scheduled or queried live. I kept it as a documented gap rather than wiring it in quietly.

### The rhythm the system has to fit

The founder dictates by voice and reads by scanning. His week has fixed anchors, and the system follows them:

| When | What |
|---|---|
| 6:00–9:00 AM CST | **Kenya Window**: the only slot for farm calls (Kenya is UTC+3) |
| Friday ~8:00 AM CT | **Commander's Review**: the cross-entity rollup |
| 1st of the month | Rent posts |
| From the 5th | The late-fee window opens under each lease's own terms. Gandi decides, never the agent |
| May 15 / Oct 15 | Minnesota property tax per parcel (GAK 7, GIF 3) |
| January | 1099-NEC per entity; books to the CPA by Feb 15 |
| Throughout | Prayer times, Ramadan, Eid and Hajj, halal venues |

This rhythm drove concrete design choices. The farm briefing moved from 06:30 to 05:45 so it is read *before* the Kenya Window. The Friday rollup moved from 16:00 to 07:30 so it arrives before the review it exists for, not eight hours after. Every report starts with a headline, then gives one fact per line in `Label: value` form, with no decorative emphasis. When there is nothing to report, it says so in one line and stops.

### Constraints that ruled out the obvious solution

- **Legal separation.** These are separate entities, and one handles protected health information. Pooling all the data into one assistant was never an option.
- **Shared mailboxes.** Some Google accounts serve more than one entity, so the separation also has to hold *inside* a mailbox.
- **People beyond the founder.** Farm and property managers need to act without asking him first.
- **Money that matters.** Payroll, rent and late fees are decided from these numbers.

---

## 4. The problem: three walls

**Wall 1 — Cost.** The founder was spending **$1,000+ per week in usage credits on top of a $200/month Max plan**, and one week passed **$3,500**. Later I measured the same problem inside Hermes: a bare "hi" cost **16,016 tokens** on one model and **17,738** on another. 95–98% of that was input: tool schemas, system prompt and memory, sent whether or not the turn needed them.

**Wall 2 — Degradation.** Claude got worse the more he installed. Unneeded skills, MCPs and plugins all claimed context on every turn. Memory markdown files contradicted each other. A local database's value had never been established. An oversized context-pack file loaded in full on every turn and was never reliably read to the end.

**Wall 3 — Trust.** On **25 July 2026** the agent said monthly payroll was roughly **KES 900,000**. It had never retrieved that number. In the same session it described email content that sat beyond a silent 20,000-character cut in a tool response. He runs payroll and tenancy decisions off these answers. One confident wrong number cost more trust than ten missing features would have.

The third wall set the bar for everything after it.

---

## 5. Timeline

| Date (2026) | Milestone |
|---|---|
| **22 Jun** | First commit: Hermes agent with Claude + Maton, deployed to Vercel the same day, mobile-friendly UI |
| **2 Jul** | Ingestion service started as a separate repo and deploy |
| **20 Jul** | Jira joins the app catalog |
| **24 Jul** | Reasoning-effort picker; upgrade plan (workstreams A–H) |
| **25 Jul** | **Fabrication incident**: invented KES payroll figure |
| **27 Jul** | Full production audit; [ADR-001](../architecture/ADR-001-fabrication-truncation-fix.md); "five things that are not what they look like" |
| **28 Jul** | Truncation fix (cap raised 20k → 120k with loud markers); context-pack loader; targeted Comfort PCA PHI exemption, authorized by the owner |
| **30 Jul – 1 Aug** | Pinecone MCP server: dual-mode auth, sensitivity ceiling |
| **7–10 Aug** | NuExtract MCP server: batch OCR, SSRF guard, branded OAuth |
| **10–12 Aug** | Guardrails (confirm-before-write, business isolation) and Artefacts, both with Haider Ali |
| **14 Aug** | Permanent public file links; fixed a production outage caused by a blank env var; GitHub→Jira automation |
| **Late Aug** | Context-rot cleanup of the Claude and Cowork surfaces |
| **14 Sep** | NVIDIA NIM models, credit and health awareness, Business Context toggle; billing repaired end to end and rebuilt on the Sheet register; scheduled tasks curated 32 → 25; autonomy made a capability gate; CI greened; Skills sub-tab |
| **7 Oct** | Hermes UI v2 (Home HUD, System view, top bar, per-tab URLs); retired-model recovery; Meeting Copilot routing fix; atomic uploads |
| **8 Oct** | **Jarvis voice agent, Multi-agents, turn router, guardrail hardening** (134 files, +49,746 lines); demo video produced |

---

## 6. Architecture

### System map

```
                         ┌──────────────────────────────────────────────┐
  Founder / managers ───►│  Hermes dashboard (Jinja2 + vanilla JS)      │◄── Jarvis voice orb
                         │  Home HUD · Agent · Multi-agents · Billing   │      (Gemini Live over
                         │  Meetings · Context · Tasks · Files · Jobs   │       WebSocket, browser-direct)
                         │  Artifacts · Settings · System               │
                         └───────────────┬──────────────────────────────┘
                                         │ SSE / JSON (95 routes)
                         ┌───────────────▼──────────────────────────────┐
                         │  FastAPI app — one Vercel Python function    │
                         │  agent loop · guardrails · autonomy gate     │
                         │  turn router · scheduler · jobs · billing    │
                         └──┬──────────┬───────────┬──────────┬─────────┘
                            │          │           │          │
          per-business keys │          │           │          │ model providers
               ┌────────────▼───┐  ┌───▼──────┐ ┌──▼─────┐ ┌──▼───────────────────────┐
               │ Maton gateway  │  │ RDS PG   │ │  S3    │ │ Anthropic · NVIDIA NIM · │
               │ 40+ apps, OAuth│  │ +pgvector│ │ files  │ │ OpenAI · Gemini          │
               └────────────────┘  └───▲──────┘ └──▲─────┘ └──────────────────────────┘
                                       │           │
                         ┌─────────────┴───────────┴──┐     ┌─────────────────────────┐
                         │ Ingestion service          │────►│ Pinecone index "hermes" │
                         │ Gmail · Drive · Fathom     │     │ entity__{slug} namespaces│
                         │ sensitivity gate ×2        │     └───────────▲─────────────┘
                         └────────────────────────────┘                 │
                                                          ┌─────────────┴────────────┐
   Claude (web, desktop, mobile, Code, API) ─────────────►│ Pinecone MCP · NuExtract │
                                                          │ MCP · Maton MCP          │
                                                          └──────────────────────────┘
```

### Stack decisions

| Choice | Why |
|---|---|
| **FastAPI on one Vercel serverless function** | One deploy and one URL, with free Swagger at `/docs`. The cost is that nothing in-process survives between requests. That shaped the scheduler (pull-based), the guardrail approval store (persisted), and voice (browser-direct WebSocket) |
| **Postgres + pgvector in the same RDS instance** | Business data and its embeddings sit in one transactional store, so an upload is all-or-nothing |
| **Pinecone for the bulk corpus** | The ingestion service writes 15,000+ emails and Drive files there under per-entity namespaces |
| **Vanilla JS, no build step** | Nothing to compile on deploy, and any file can be read and patched in place. It has scaled to 27 JS modules |
| **Maton as the integration layer** | One gateway holds every business's OAuth tokens, so one credential per business replaces forty separate connectors |
| **Provider-agnostic agent loop** | History is stored in one canonical block shape and translated for each call, so switching between Claude, NIM, OpenAI and Gemini mid-conversation keeps the history |

---

## 7. Business isolation: six sealed compartments

Isolation is the property everything else depends on. I enforced it at four layers so that no single mistake breaks it.

1. **Credential boundary.** Each business has its **own Maton API key** (`MATON_BUSINESS_KEYS`). A request for business X can only use X's OAuth tokens. A business with no configured key gets a clear "not connected" instead of silently falling through to a sibling entity's account.
2. **Request scoping.** Every conversation is locked to one business or explicitly scoped to "All". In "All" scope, every tool requires an explicit `business` argument.
3. **Retrieval guardrail.** Every vector match's metadata (`entity_id`, `entity_slug`, `business`, `entity`, `tenant`) is checked against the active business. A match from another entity is dropped and logged at ERROR. A query with an empty namespace is refused, because the bare `entity__` prefix would match every namespace.
4. **Relevance filtering on shared inboxes.** Where two entities share a Google account, filter terms derived from each business's context keep each entity's ingestion to its own mail.

**Two identity schemes, bridged instead of merged.** Separate features had grown two slug systems: `businesses` (`afk`, `gak`…) and `entities` (`afro_farm`, `gak_properties`…). Unifying them would have meant a risky migration across live financial data. Instead, one bridge module (`hermes/businesses.py`) makes the seam explicit and testable.

---

## 8. The agent core

### The loop

`stream_turn` runs one tool-use loop for every provider, and `run_turn` drains it for non-streaming callers. Each turn has a budget of 8,000 output tokens and 12 tool round-trips. If a reply hits the token cap without calling a tool, it is **resumed in place** with a doubled budget, up to a ceiling. Thinking blocks are stripped from the prefill.

### Models

The model picker is platform-wide and stored in Postgres, not per browser. On 14 September I added **six NVIDIA NIM models** (Nemotron 3.5, GPT-OSS 20B, Gemma 4 31B, DiffusionGemma 26B, Kimi K3, DeepSeek V4 Flash), each with its own tuning recipe. Adding them surfaced three real bugs:

- A stream that ended with no `finish_reason` was reported as a clean finish, so the resume path never ran. It now reports `max_tokens`.
- Unsigned reasoning stored as a thinking block caused a 400 on the next Claude turn. Reasoning now travels outside the shared history.
- DiffusionGemma's `reasoning` field was being dropped.

Background work (memory extraction, summaries, OCR fallbacks) runs on a separate background model. When the original one (`deepseek-v4-flash-0731`) was retired on 21 September with HTTP 410, memory extraction stopped silently. I replaced it with DiffusionGemma, which took 3–9 s and stayed grounded, against 24–35 s with invented facts on the alternative. Background calls now fall through to another model on 404 or 410.

### Credit and health awareness

- **Credits** (`hermes/credits.py`). Out-of-credit errors are detected for every provider and tracked per billing scope. A plain 429 is treated as throttling, not exhaustion. The user sees "API credits are out…" instead of a 500.
- **Health** (`hermes/health.py`). Every retrieval path that fails open records `ok`, `empty` or `failed`, and `/api/health` reports degradation. I built this because memory extraction had been dead for a month while the app reported itself healthy. A system that degrades gracefully also has to say that it is degraded.

### Reasoning effort

The options are Auto, Low, Medium and High. Auto decides per turn from attachments, depth keywords and message length. An audit found the effort setting only reached Claude, and that punctuation ("Hi!") broke the Low match. Both are recorded in the turn-cost plan.

---

## 9. The dashboard and Hermes UI v2

### Views

| View | What it does |
|---|---|
| **Home HUD** | A reactor-core status display. Knowledge, credit and the next scheduled runs on the left; connected apps and alerts on the right |
| **AI Agent** | Streaming chat over SSE. File attachments are uploaded once and reused. Conversations are saved and can be reopened. Each turn shows which tools and apps it used. Includes the model picker, effort picker, Business Context toggle and agent-mode picker |
| **Multi-agents** | Specialist agent cards with modes, a chat drawer and an editor ([§14](#14-multi-agents)) |
| **Billing & Invoices** | OCR'd bills, the Sheet register, propose-only amount verification ([§11](#11-billing-invoices-and-the-register)) |
| **Meeting Copilot** | Pre-meeting briefs, live grounded guidance, post-meeting summaries, session replay |
| **Business Context** | Files, Memories (everything the agent has retained, each item deletable) and Skills, per business |
| **Scheduled Tasks** | Recurring unattended agent runs with an autonomy ceiling |
| **File Explorer** | Read-only two-pane browser over each business's S3 objects |
| **Jobs** | Registered Python functions on a cron (for example, the Google Reviews scraper) |
| **Artifacts** | Live, LLM-free re-rendering canvases ([§10](#10-claude-artifact-dashboards-and-in-app-artefacts)) |
| **Settings** | Guardrail toggles, global and per business |
| **System** | A live architecture schematic of the running system |

### UI v2 (7 October)

- **Top bar.** A global business picker, a Holo/Stock skin switch, an API health light, a command palette and a Present mode.
- **Per-tab URLs** (`/agent`, `/billing`, …) through a client router, with matching page routes on the server, so any view can be bookmarked and shared.
- **First paint is no longer blocked** by the slow all-business document count.
- **Decluttering.** I removed the duplicate per-tab business pickers and collapsed empty sidebars on six tabs. Hidden controls stay in the DOM so command-palette actions keep working.
- **Honest System view.** A follow-up change makes a failed or timed-out fetch show as unknown instead of a false "0 apps". A dashboard that shows a plausible zero when the truth is "I don't know" repeats the fabrication incident in a smaller form.

---

## 10. Claude Artifact dashboards and in-app Artefacts

### The Artifact dashboards managers actually use

These are the dashboards his managers open every day. They live on the Claude Artifact and Cowork surface, not in the Hermes repository. I built one for each of the six businesses:

- **Farm operations (Afro Farm).** Years of operational history made queryable for the first time. **Attendance reports drive payouts** for ~54 staff paid in KES. They are built for manager review and sign-off, so every figure is shown next to the records underneath it, never as a bare total. Kenya-specific context sits alongside: GLOBALG.A.P., market prices, and maize-price forecasting from World Bank Open Data.
- **Spa (Nour Hammam).** Claude drafts suggested emails to contacts, and **the drafts send straight from the dashboard UI**. Draft, review and send happen in one place, with no copying into a mail client.
- **Rental properties (GAK and GIF).** Tenant tracking per unit, property and entity. **Rent invoices are generated and sent automatically**, and when rent passes its due date **the late fee is applied and the notice sent automatically**. This is where small portfolios quietly lose money: a late fee nobody checked, or an invoice that went out three days late.
- **Money in / money out.** Portfolio-wide cash across all six businesses, **verified against QuickBooks**. **This saves $31,000 a year.** It is the clearest financial return of the engagement. The saving comes from cash position being visible all the time across entities, instead of reconstructed one entity at a time.
- **GAK Commodities and Personal & Family** complete the six, each with the same isolation.

### In-app Artefacts: live data, no model on the render path

Inside Hermes, Haider Ali and I built **Artefacts**. Each one binds a title, a live source (Sheets, Docs, Drive or reviews) and a render recipe (table, key-value, raw, dashboard or reviews).

- **The AI is used only to create an artefact** (the `create_artefact` tool). After that, opening one calls `sync_and_render()`. That rebuilds the business's gateway client from the *stored* business slug, re-fetches the source, and renders self-contained, escaped HTML.
- **The render module imports no model SDK**, and a test asserts that it makes zero LLM calls. Re-opening a dashboard costs nothing and cannot hallucinate.
- **Security.** A 38-check harness covers XSS on every render path and tenant isolation. A live render against production Sheets produced 64 rows and 3 SVG charts.
- **Generated files get permanent links.** The original 24-hour presigned URLs were ~500 characters long, and the model reliably dropped the query string, leaving an unsigned URL that returned 403. Links now come from a permanent public URL on the `generated/` prefix only. The bucket policy allows anonymous `GetObject` there and nothing else, and `public_url()` refuses any key outside that prefix *in code*. Public objects use SSE-S3, because anonymous requests can't authenticate to KMS. Every other object keeps SSE-KMS, so this is not a bucket-wide encryption downgrade.

---

## 11. Billing, invoices and the register

### The original feature

Billing worked like this:

- **Upload a bill**, run OCR, and store a structured record (vendor, amount, invoice number, due date, category), with the business attributed automatically.
- **Proactive scan** of Gmail and Drive for new bills.
- **Auto-mark-paid** by finding payment confirmations in Gmail.
- **Portfolio-wide insights.**

### Repairing it end to end (14 September)

An audit found the feature was broken in ways that each looked like a minor glitch:

| Defect | Fix |
|---|---|
| Bills could never become *overdue* or *due soon*, because nothing wrote those statuses | Status is derived when the bill is read |
| Gateway error bodies were parsed as message data, producing **48 nameless production bills** | An explicit envelope check (`_maton_data()`) |
| One unparseable date ("Net 30") discarded the whole bill | Only the field is dropped, not the record |
| `1.234,56` was misread, and credit notes lost their sign | Locale-aware, sign-preserving parsing |
| No OCR fallback when NuExtract was down | A fallback ladder |
| Dedupe was check-then-insert with no constraint | Partial unique index + `ON CONFLICT` |
| Unbounded Gmail searches in `check_paid_bills` | Bounded queries |
| **Stored XSS** in the Billing tab; an email iframe with `allow-same-origin` | Escaping; sandbox tightened |
| No upload size or type limits | Limits enforced |
| "Total This Month" was actually an all-time sum, and treated every currency as USD | Correct window; currency kept |

Four more viewer faults followed: a modal with no height (iframes defaulting to 150 px), every S3 object treated as a PDF, and the wrong mailbox key. I verified each fix against production data.

### Building on the Sheet instead of competing with it

The Billing tab held **103 rows and $0 captured**. Meanwhile the owner's real system of record was a Google Sheet register: **79 recurring lines, $2,025.97/month, a 15-state status vocabulary and a change log**, maintained by a separate automation layer. A second register would only have produced two different totals.

So I rebuilt Billing on the Sheet:

- `hermes/register.py` reads the Register and DEADLINES tabs. It writes **only** an Amount cell plus a CHANGE LOG row.
- `parse_money` returns *None*, not 0, for "AMOUNT IN PDF - UNVERIFIED". An unknown amount is never shown as zero.
- `hermes/verify_amounts.py` matches unverified lines to stored PDFs and files **proposals only**. A human approves each one.
  - A match requires an identifier from the line to appear in the document.
  - Each document can be used once.
  - OCR noise is rejected.
- **On the live register this cut 6 confident-looking proposals to 1 real one plus 5 referrals to a human.** I count that as success: the other five would have been plausible errors written into the books.

278 tests passed at merge.

---

## 12. Meeting Copilot

The Meeting Copilot gives grounded guidance before, during and after a meeting. It has pre-meeting briefs built against a specific calendar event, live guidance, post-meeting summaries with the recording link, and full session replay.

**Five data layers** feed the guidance:

1. **Meeting context.** Prior decisions, open action items, attendees and roles, earlier conversations.
2. **Business strategy.** Quarterly goals, roadmap, constraints, budget and hiring plans.
3. **Team and organization.** Dependencies drawn from floormap data, blockers, capacity and ownership.
4. **History.** Email history with the attendees, past decisions and how they turned out.
5. **Risk and compliance.** Known risks, regulatory requirements, prior incidents.

**Guidance standards.** Every turn has to be **specific** (never generic), **grounded** (cites its source), **actionable** and **concise** (at most three bullets). It flags conflicts with past decisions and never repeats itself within a session.

**Latency design.** Data is prepared before the meeting, never during it. The ingestion service ([§16](#16-the-knowledge-and-memory-layer)) fills the knowledge layer ahead of time, so the copilot never scans an inbox mid-conversation. A faster model handles live guidance so the expensive model stays out of that path.

**A routing fix worth recording.** After the model migration, the copilot was calling the Anthropic SDK with an NVIDIA model ID, so it could never succeed. It now goes through a provider-agnostic JSON helper, and a regression test (`test_meeting_copilot_routing`) pins it.

---

## 13. Scheduled Tasks: unattended work with a real ceiling

### The model

I think of a Scheduled Task as **a saved chat turn plus a clock**: the same agent, tools and business scoping, fired automatically instead of typed. The founder can create one in conversation: *"Every morning at 7 give me a farm briefing from AFK Gmail and Calendar."* The agent works out the schedule, business, apps and autonomy, asks when anything is ambiguous, and never chooses `full` unless clearly told to.

**Engineering details**

- **Pull-based.** Vercel cron hits `/run-due` every 15 minutes, because serverless instances are frozen between requests.
- **Atomic claim before each run**, which stops the same task firing twice. Stale runs are reaped.
- **Schedules:** `daily`, `weekdays`, `weekly` and `hourly`, each in the task's own timezone. There is no monthly option, so date-specific work (the rent window, month close) runs as a frequent task with a **date gate as its first instruction**. Outside its window it replies in one line and calls no tools, so it costs almost nothing.
- **Date-window backfills.** A task can process one calendar day per run across a bounded range ("read each day's WhatsApp for a month, one batch per run"). A job too large for any context window becomes a series of small grounded runs with a saved cursor.

### Curation: 32 tasks down to 25

An audit on 14 September found that the scheduler was doing almost nothing useful:

- **Only 6 of 32 tasks were enabled, and all 6 were identical copies** of one unscoped "Daily attention report". They emailed `owner@hermes.local`, which is not a real address.
- **Every real business task was switched off.** Two more notified a third-party address.
- **The duplicates were being generated.** `ensure_digest_task()` ran on every boot and looked the task up by exact name. Concurrent Vercel cold starts raced each other, which is why copies appeared in bursts about twenty seconds apart. When the cleanup renamed the task, a fresh duplicate appeared within minutes, which proved the name was load-bearing. Fix: the name is pinned, and a **partial unique index** (`0021_unique_digest_task.sql`) makes the losing insert in a race fail at the storage layer.
- **The black-hole email** came from an unset `OWNER_EMAIL` environment variable falling back to a placeholder. One environment variable fixed delivery for everything.

**Decisions:** 18 tasks deleted, 14 kept and rewritten, and 12 created where real obligations had no coverage. The new ones include:

- **A lease renewal countdown** for two leases that both ended 16 days after the audit, with nothing watching them
- **A rent-posting and late-fee window** check, date-gated to the 1st–8th
- **A farm field-report check** that runs on Kenya time at 16:30 EAT, right after the last report is due
- **GLOBALG.A.P. compliance evidence** tracking
- **A formation tracker for the spa entity**, covering the related-party lease where Gandi is on both sides
- **A cross-entity urgent email triage** for the three businesses that had none

Every prompt is built as `body + RULES + FORMAT`, so the guardrails are identical across all 25 and can be changed in one place.

- **RULES:** stay inside this business; money comes from live QuickBooks only, and if it is unreachable, say so instead of estimating; never invent a tenant, date, amount or person (write `no indexed source found` + `UNVERIFIED`); tag related parties; send nothing.
- **FORMAT:** headline with a count, one fact per line, a `Decisions needed` footer, and *one line and stop* when there is nothing to report. That last rule keeps 25 tasks from turning into 25 sources of noise.

The week is spread out (Mon 3, Tue 4, Wed 3, Thu 3, Fri 3, Sun 1, plus 5 daily tasks), and morning slots are staggered from 05:45 to 09:00.

### Coexisting with the Sheets automation layer

A separate automation layer maintains Google Sheets registers for most of the portfolio, using a Verifier/Updater pattern. The risk was not that the two systems disagreed in philosophy. It was **two systems reporting the same facts from different sources without knowing about each other**, which produces two arrears numbers and no way to tell which is right.

18 tasks now carry a `REGISTERS` block:

- Read the register before reporting, and name the tab you read.
- **Never write to it**; the Updater owns every write.
- When a live figure disagrees with the register, **report both with sources and flag it**. The disagreement is the finding.
- QuickBooks stays the only source for money.

### Autonomy: from a paragraph to a capability gate

Autonomy tiers are `readonly`, `draft` and `full`. Until 14 September, the tier was **only a paragraph prepended to the prompt**. Every task listed Gmail, so **all 25 had a live `send_email` tool**. `maton_request` also accepted any HTTP method, so a "readonly" run could POST to Gmail's send endpoint directly. Nothing had actually sent an email, but for an unattended run an instruction is not a control.

The tier is now enforced in two layers:

1. **`build_tools(..., autonomy=…)`** withholds write tools entirely: send, attach-and-send, Zoom invites, WhatsApp sends, connection creation, Jira and task writers, and guardrail toggles. It narrows `maton_request` to `GET` (readonly) or `GET`/`POST` (draft).
2. **`dispatch(..., autonomy=…)`** refuses before anything reaches the network, because a tool call can still arrive from a replayed transcript or a hallucinated tool name. A `draft` run may POST **only to `/drafts` paths**. A later hardening pass tightened that check to a regex, so `/drafts/send` no longer passes.

Interactive chat is unchanged, because a human is there to approve. 28 assertions in `_test_autonomy_gate.py` cover tools withheld, methods narrowed, raw Gmail send refused at both tiers, drafts allowed only at `draft`, Sheets writes refused, and chat and `full` untouched.

### Run-history loss, and recovery

Run history was linked with `ON DELETE CASCADE`, so deleting 18 tasks had **deleted their run history too**. I built a recovery tool:

1. Restore the database to a point in time just before the deletion, **in a separate cluster**.
2. Harvest the lost runs.
3. Write them back under parent tasks recreated as **DISABLED** with a `[RECOVERED]` prefix.
4. Tear the clone down.

Backup retention went from 1 day to 14. A backup export now runs before any task deletion.

---

## 14. Multi-agents

### What it is

Shipped 8 October. A **Multi-agents** tab holds a grid of specialist agents. Each one has a business, a purpose, a tool whitelist, and **modes**. A mode is a named variant with its own instructions, autonomy tier, a narrower tool list and an app allowlist. "Find deals" on the House Scout is read-only. "Outreach" in the same agent can draft an email to a listing agent.

**Six built-in agents**

| Agent | Business | Example modes |
|---|---|---|
| Market Analyst | Nour Hammam | Full market report · Competitor scorecard · Complaint & praise themes · Pricing positioning · Launch promises |
| House Scout | GAK | Find deals · Underwrite · Outreach (draft) |
| Commercial Leasing & Acquisitions | GIF | Leasing pipeline · Acquisition screening |
| Research Agent | All | Sourced web research, with a URL for every claim |
| Farm Operations | Afro Farm | 14-day weather with rain bands, field operations |
| Markets & Buyers | Afro Farm | Buyer and price discovery |

### Design: no new engine, only constraints

Multi-agents adds **no new agent engine**. It reuses the existing turn loop and constrains it:

- The table is `hermes_agents`, because the shared database already has an unrelated `agents` table. Built-ins are seeded lazily from code. They can be edited and reset to default, but not deleted.
- **The whitelist is the capability boundary.** Only whitelisted tool schemas are offered, and `_dispatchable_tools` refuses anything else, even if the model calls it by name.
- Read-only and draft modes **reuse the scheduled-task autonomy gate**.
- Scheduling, guardrail toggles and connection creation are **excluded from every agent**.
- **Agent-only tools:** `reviews_data` (scraped Google reviews for the spa market) and `weather_forecast` (Open-Meteo).
- Shared rules are appended to every agent: never invent a number, listing, review, price, name or URL. Computed numbers must **show their inputs** (for example, `77 (= 20 + 18 + 39)`). Facts are kept separate from labelled judgement.

### Testing against real data and independent ground truth

There are **24 offline platform tests**, all passing. They cover the whitelist boundary, refusal of forced off-list tools, the write gates, preset validation, and delete and reset rules.

I then ran **owner-value tests**: real turns on real data, each graded against ground truth I computed independently from the raw review CSVs.

| Test | Claude Haiku 4.5 | Nemotron 3.5 Lightning |
|---|---|---|
| Rank 8 Minnesota competitors | ✅ every rating exact | ✅ exact (3 runs) |
| Complaint themes with quotes | ✅ counts exact, quotes verbatim | ❌ then ✅ (run 1 emitted literal `<tool_call>` text) |
| Launch promises | ✅ no unsourced number | ❌ invented a denominator; misquoted a share; "71 venues" instead of 77 |
| "Reviews from Tokyo?" (trap question) | ✅ no data, no invented count | ✅ |
| 14-day farm weather | ✅ matches a direct Open-Meteo call | ✅ after the rain-band fix |

| | Haiku 4.5 | Nemotron 3.5 |
|---|---|---|
| Time per turn | 20–110 s | 70–410 s |
| Cost per turn | ≈ $0.006–0.05 | ≈ $0.003–0.007 |

**Fixes that came from the tests:**

- `weather_forecast` returns rain-intensity bands. Before the fix, 3.9 mm was being called "heavy".
- The input-showing arithmetic rule was added.
- The Research Agent now treats knowledge-base hits as leads, not sources.
- The test runner records **the model that actually answered**. This mattered because when the Anthropic credit ran out mid-test, the credit fallback quietly moved turns to a model without web tools.

**Verdict:** scorecards, trap questions and weather are ready on any model. Synthesis with computed figures, and anything that needs the web, needs Claude. The web tools are Anthropic server-side tools and are dropped on every OpenAI-protocol model.

---

## 15. Jarvis: the real-time voice agent

The founder dictates by voice, so voice had to work as a first-class way to use the system, not an add-on. The first version chained speech-to-text, the agent, then text-to-speech. That works, but every reply makes three round-trips. On 8 October I shipped **Jarvis** (in code, "Hermes Live"): a hologram orb you click to talk to.

### Architecture

- **The browser talks to Gemini Live directly over a WebSocket.** Vercel functions can't hold a WebSocket open, so the server does one thing: it **mints a single-use ephemeral token** (`POST /api/voice/live/session`).
- **The full session setup is locked into the token**: model, system prompt and tools. A leaked token can't be pointed at anything else. The API key never reaches the browser. The token must open a session within 2 minutes, and a session lasts up to 30.
- **Gemini is the voice, not the brain.** Its only tool is `ask_hermes`, which the browser answers by calling the normal `/api/chat` with `voice: true`. Every spoken question therefore goes through the **same agent, retrieval, guardrails, business scoping and apps** as typed chat. The voice prompt forbids answering from Gemini's own knowledge.

### Audio and latency engineering

- An **AudioWorklet** downsamples the mic to 16 kHz Int16 in 100 ms (1,600-sample) chunks. Its box filter doubles as the low-pass filter.
- Replies arrive as **native 24 kHz audio** and are scheduled 40 ms ahead for gapless playback.
- **Barge-in:** Gemini's own voice-activity detection signals `interrupted`, which flushes the playback queue at once, so you can talk over Jarvis the way you would a person.
- Transcription is on for both input and output, so voice turns leave the same written record as typed ones.
- **The mic is requested before the token is minted**, so a denied mic permission doesn't waste a token.
- A voice style block keeps spoken replies short and free of markdown.

The older speech-to-text/text-to-speech path stays as a fallback. One correction for the record: a generated HTML report describes voice as "hard-locked to read-only in code". That is not accurate. Voice turns are protected by the same confirm-before-write gate as chat, not by a separate lock.

---

## 16. The knowledge and memory layer

### Four context layers

| Layer | What it is | How it reaches the model |
|---|---|---|
| **Standing rules / context packs** | Curated governance: hard rules, security, per-business facts | Pushed into every turn that offers a tool. Since 8 October a code fallback guarantees the hard rules arrive even if the packs table is missing |
| **Persistent memory** | Facts extracted by a model after each turn, stored in pgvector with provenance | Pushed every turn: an "always" tier plus semantic top-K |
| **Business knowledge base** | Hand-written notes, uploaded files, **21 seeded Skills** | Pulled through `search_business_knowledge` |
| **Ingested corpus** | 15,000+ emails and Drive files in Pinecone | Federated into search behind a feature flag |

**Skills** are ordinary document rows tagged `source_system="business_skill"`: three openly licensed skills for each business plus three global ones, each with a provenance header. Because they are documents, they were chunked, embedded and searchable without any API change.

**Uploads are all-or-nothing.** If embedding fails, the upload removes the row, its chunks and the S3 object. A half-ingested document is worse than none, because it looks searchable and isn't.

### The ingestion service

This is a separately deployable service with its own repository. It reads each business's Gmail, Drive and Fathom meeting notes through the gateway, using per-business keys. It runs OCR on PDFs and images, chunks and embeds the text, and upserts into per-entity Pinecone namespaces using **the same metadata shape the main app's guarded search expects**.

Three rules define it:

1. **Prepare before, never during.** The live copilot only reads.
2. **Idempotent by content hash.** A SHA-256 of normalized text (and a bytes hash for binaries) means unchanged content is never re-embedded. The service can run every few minutes over the same inbox and pay only for genuinely new material.
3. **The sensitivity gate sits below the model.** Before anything reaches object storage, a cloud OCR processor or the embedding API, a gate decides.
   - Protected health information, immigration material and attorney-client material are classified `restricted` and **recorded in Postgres only, never sent anywhere**.
   - For binaries **the gate runs twice**: on the filename and metadata before OCR, then on the OCR'd text. If the second pass blocks, the bytes are pulled back out of storage.
   - The README states the principle: *reachability is not locality of processing.*

Its endpoints **fail closed** with a 503 if no secret is configured, and every secret comparison is constant-time.

### Memory with provenance

After the fabrication incident ([§19](#19-trust-engineering)), recall became **retrieval, never generation**. Stored, timestamped, source-tagged records are injected verbatim. The model may say "you told me" only about text that literally appears in a recalled record. The Memories sub-tab shows the owner everything the agent has retained and lets him delete any of it.

---

## 17. Three custom MCP servers

Nothing off the shelf covered what he needed, so I built three MCP servers, each deployed on its own.

### Pinecone MCP: the knowledge connector

This exposes the vector index to every Claude surface as a custom connector, with two tools: `search_business_knowledge` and `list_entities`.

- **Guards copied from the main app, not trusted from the caller.**
  - One namespace per call, from an allowlist.
  - Active records only.
  - A **sensitivity ceiling hard-capped in code**: `restricted` is never served, and an unknown level fails closed to `public`.
  - The PHI namespace is excluded unless explicitly enabled.
- **Dual-mode authentication.** Claude's custom-connector dialog offers only a URL and OAuth credentials, with no header field, so a token-only server can't be added at all. The server runs:
  - **Google OAuth** (openid + email scopes only) through a transparent proxy with an explicit email allowlist, for claude.ai, Desktop and mobile.
  - A **static bearer token** for Claude Code and the API.

  Which modes are live depends only on which environment variables are set.
- **A serverless OAuth post-mortem** is in the README. Version one raised an error at import, which killed `/health`. Version two disabled OAuth, which caused 404s. The final version **degrades and reports the problem loudly**.
- **Documented gotchas:**
  - FastMCP's DNS-rebinding guard rejects every non-localhost `Host` header, which means *every* request from Anthropic's cloud.
  - Namespaces use a double underscore; a single underscore silently returns nothing.
  - Setting the ceiling to `internal` returns zero results, because every vector is `confidential`.
- **85 checks** across 8 groups in the test harness.

### NuExtract MCP: reading what PDF readers can't

**The problem.** Standard PDF readers only extract an existing text layer. Scanned invoices, phone photos of bills and architectural **floormaps** came back empty, and whatever text they did return still had to be re-typed by hand.

**What I built.** Real OCR on the page, returning **schema-locked JSON** with fields copied exactly as written. There are four tools: single and batch versions of read and structured extract. **Batches take up to 20 files**, run 10 at a time, and isolate failures per file, so one corrupt document returns its own error while the other nineteen succeed.

| Workload | Batched | Sequential |
|---|---|---|
| 20 PDFs | **12.5 s** | ~56 s |
| 12 PDFs | **7.6 s** | ~34 s |
| 6 PDFs | **5.0 s** | 17.0 s |

A batch costs about as long as its slowest file, not the sum of all of them.

**Security**

- An **SSRF guard**: http(s) only. Private, loopback, link-local, reserved, multicast and cloud-metadata addresses are refused, and the check is **repeated on every redirect hop**. A 20 MB cap is enforced while streaming.
- OAuth state is stored Fernet-encrypted in Redis. The allowlist is checked inside every tool.
- The server **refuses to boot** if OAuth is on without an allowlist.
- The OAuth consent screen is branded.

**Floormaps now come back as clean tables**, and the extracted structure feeds team and space planning in the Meeting Copilot.

**Limits of the client surface, documented instead of hidden.** On claude.ai, inline file payloads fail above **~30 KB** and tool calls stop at **~60 s** on the client side, whatever the server timeout. The workaround is in the README: pass a URL and let the server fetch up to 20 MB, or use Desktop or Cowork. Other gotchas are documented too: a hostname that doesn't resolve, an SSE stream with no timeout (now wrapped in a hard 60 s limit), Vercel's 4.5 MB body cap, and no HEIC support. There is deliberately **no vision-model fallback**: a guessed transcription of an invoice is exactly the failure this server exists to prevent.

### Maton MCP: 40+ apps on one connection

**One MCP server connects Claude to 40+ apps.** Maton holds each business's OAuth tokens and proxies to each app's native REST API. Claude reaches the whole estate through a single authenticated surface instead of forty connectors, each with its own auth, failure modes and context cost. Context cost was what had been driving the bills up, so consolidating forty connectors into one was as much a cost decision as an architectural one.

There are dedicated tools where the interaction is rich (Gmail, Calendar, Drive, Jira, Zoom, WhatsApp) and a **universal request tool** for any endpoint of any connected app. Connection status (active, pending or failed per business) shows in the dashboard, and the agent can start a new connection flow in conversation. Because each business has its own key, this single gateway is also the isolation boundary: **one connection point, six sealed compartments.**

---

## 18. Integrations: WhatsApp, Twilio, QuickBooks, Jira

**The app estate.** Gmail, Calendar, Drive, Sheets, Docs, Forms, Contacts and Meet; QuickBooks; Notion; Jira; Zoom and Zoom Admin; WATI (WhatsApp); Fathom; PDF.co; and **40+ apps in total** through the universal request tool. Non-gateway sources include World Bank Open Data (12-hour cache, prewarmed at startup), Open-Meteo, Google Reviews (via a scheduled scraper job) and, on Claude, web search and fetch.

**WhatsApp: Claude sends messages.** Eight WATI tools cover listing and creating contacts, updating attributes, message history, **sending text and files**, assigning operators and retrieving media. Media is archived to S3. I tested it at scale by pulling and analysing a full month of one business's WhatsApp history using date-window backfills.

**Twilio: managers get a text when the CEO emails.** Email alone doesn't reach a farm manager in a field. **When an email goes to a manager, Twilio sends that manager an SMS saying the CEO has emailed them.** The email carries the content; the SMS makes sure they know it arrived.

**QuickBooks: every number verified.** AR/AP aging, P&L, balance sheet, invoices, estimates, customers, product and sales summaries, and payroll. This is the check that makes the money dashboard trustworthy. It is also why the hard rule says money comes **from live QuickBooks only**, and if QuickBooks is unreachable the agent says so.

**Jira and GitHub automation.** A webhook service turns GitHub push, PR and review events into Jira comments and status moves: push → In Progress, PR → In Review, merge → Done. It never moves a ticket out of Done. Ticket resolution tries these in order:

1. An existing branch binding
2. The merged branch
3. An explicit key (branch name, PR title, PR body, commits)
4. **An AI match** with a 0.7 confidence floor, validated against the closed set of real issue keys
5. A new ticket

The webhook is verified by HMAC (503 if no secret is set), restricted to allowlisted repos and projects, and made idempotent by delivery ID. It has a dry-run mode and 71 tests. Against 31 open issues, one push matched the right ticket at 90% confidence, and a dependency-bump chore correctly produced no ticket. *Status: built and tested as a standalone package; not mounted in the current main app.*

---

## 19. Trust engineering

### The incident

On 25 July 2026 the agent stated a payroll figure of ~KES 900,000 a month that it had never retrieved. In the same session it described email content that lay past a silent 20,000-character cut.

### The diagnosis (ADR-001)

I wrote a formal architecture decision record. Its central finding changed the diagnosis completely:

> **The anti-fabrication rules the remediation asked for already existed as prompt text, in stronger language. They had never been loaded.**

The governance file said, in effect: *rent and money figures come from live QuickBooks only; if QuickBooks is unreachable, say so, do not estimate; never invent tenants, dates, amounts or people.* QuickBooks was in a PENDING state. The agent estimated anyway, because the migration that created the context-pack table had never been applied in production. The loader swallowed the error into a log warning and returned an empty string. **Every production turn had been running with an empty governance block.**

The deeper root cause was that **nothing in the pipeline knew where a fact came from**. A tool result was a JSON blob concatenated into context. Once the model had written a sentence, no code could tell whether a number in it was retrieved, derived, remembered or invented. The ADR's answer is to **make provenance a data structure, not an instruction**: record what entered context at the single dispatch choke point, and check the finished reply against that record at the single exit point.

### What shipped

**Truncation (28 July).** The 20,000-character cap used to serialize and then slice, so a truncated list came back as a broken *dict*. The new truncation **preserves structure**, puts a loud `<<TRUNCATED>>` marker where content was dropped, and moves a truncation report to the top of the response. The cap went from 20k to 120k characters. 20 unit and 13 integration checks.

**Recall is retrieval, never generation.**

- **Strict attribution.** "You told me" is allowed only about text that literally appears in a recalled record, with its date and source.
- **Threshold tuned toward silence.** Below the score threshold, *nothing* is injected. A marginal match invites a confident guess.
- **Conflict resolution.** The newest record wins. The superseded one is marked, not deleted.
- **Summaries stay extractive.** Model-written summaries are the one genuine hallucination risk in a retrieval system.
- **Dual provenance.** Every recalled item records *when* and *which session and business*.

**Standing rules that can't go missing (8 October).** `standing_rules()` carries a code-level fallback, so the hard rules and security sections reach every turn that offers a tool, whether or not the packs table exists. The failure mode behind the incident, governance silently absent, is closed at the source.

### Four guardrails, enforced at the dispatch choke point

| # | Guardrail | Mechanism |
|---|---|---|
| **1** | **Confirm before write/send** | Tools are classed SEND / WRITE / DESTRUCTIVE / INTERNAL. A guarded call without a valid approval returns a human-readable summary and a token **bound to a hash of the exact arguments**. If the arguments change, the call is blocked again. Tokens are HMAC-keyed, single-use, carry the turn number, and are **honoured only from the next turn**, so the model can't approve its own action in the same breath. Toggles live in Postgres, global or per business. Any error defaults to *guarded* |
| **2** | **Business isolation** | Cross-entity vector matches are dropped and logged at ERROR; empty-namespace queries are refused ([§7](#7-business-isolation-six-sealed-compartments)) |
| **3** | **Egress filter** | Refuses secrets and **Luhn-valid card numbers** in any send or external write |
| **4** | **Per-turn blast radius** | Caps sends and deletes per turn (default 10) |

Guardrails 3 and 4 **hold even with confirmations switched off, and in unattended runs**.

### A bypass hunt on my own guardrails

Before the 8 October release I attacked Guardrail 1 systematically. **97 tests** in `test_guardrail_bypasses.py` pin what I found:

- **B1/B2:** DESTRUCTIVE actions weren't in the guarded set, so a `DELETE` through the universal gateway and task deletion ran unconfirmed. Fixed.
- **B3:** Turning a guardrail *off* is now its own SAFETY class and is confirmed **even when confirmations are off**.
- **B4:** The attach-and-send tool could skip the draft step. Fixed.
- **B5:** The turn counter stalled at about 21 in long chats, which would have let old approvals linger. Fixed.
- **B6:** Approvals are carried across turns as explicit pending state, and a **strict whole-message parser** decides what counts as a "yes". "Thanks 👍" is not an approval.
- **B13:** Unattended runs lose the authoring and guardrail tools entirely.
- **B14:** Creating a task with `full` autonomy, or raising one to it, is class AUTHORIZE and **always** confirmed.

---

## 20. Auditing my own work

I was building a system whose whole purpose is never overstating what it knows. Documenting it any other way would have been incoherent. On 27 July I audited the live production system and opened the project context document with a table headed **"five things that are not what they look like,"** with evidence for every row. Here is that table, with where each item stands now:

| # | July finding | Status in October |
|---|---|---|
| 1 | **Context packs dead in production.** The tables didn't exist, and the loader returned "" silently. The anti-fabrication rules had never reached the prompt | **Closed at the source.** A code-level standing-rules fallback guarantees the hard rules on every turn that has tools. Whether the table now exists in production was not re-checked |
| 2 | **Knowledge base ~0.3% reachable.** Only 124 of 41,749 chunks had embeddings, and 15,380 ingested files were invisible | **Code path built, not yet on in production.** Pinecone federation exists behind a flag that defaults off; a Gemini re-embed script was added. No re-count yet |
| 3 | **Autonomy ceiling not enforced**: a paragraph, not a control | **Fixed.** Capability gate in `build_tools` + refusal in `dispatch` (14 Sep) |
| 4 | **Chat write-confirmation prompt-only** | **Fixed.** Guardrail 1 at the dispatch choke point (12 Aug), then hardened twice |
| 5 | **Schema drift.** A production migration existed in no branch | **Partly fixed.** The orphan migration is now in the repo and its column is read, but migration number collisions remain (two 0016s, three 0019s, two 0020s) |

**The status legend is part of the deliverable.** All the documentation distinguishes **LIVE** (verified in production), **SHIPPED** (committed, not verified), **WORKING TREE**, **AUTHORED, NOT LOADED** and **DOC-ONLY**. The project context document ends with a "which docs to trust" table that labels three older documents as misleading, instead of leaving them to mislead quietly.

---

## 21. Cost and context rot

### Cleaning the Claude and Cowork surfaces

I audited the founder's Claude installation the way I would audit infrastructure.

**What I found:** usage cost and answer quality had come apart. More installed capability had produced worse output and higher bills. The causes were cumulative:

- Unneeded skills, MCPs and plugins, each claiming context on every turn
- Several memory markdown files contradicting each other
- A local database whose value had never been established
- An oversized context-pack file loaded in full on every turn

**What I did:**

- Disabled Claude's memory features temporarily. This was deliberate and reversible, and I recorded it so that gaps in recall would read as expected behaviour, not a new fault.
- Removed access to the oversized pack file.
- Cleaned and simplified the Claude and Cowork instructions, **after saving the previous versions in Notion**. That step is what separates a cleanup from data loss, and I did it first.
- Disabled unneeded skills, connectors and plugins.
- Opened a triage of the local database in a Colab notebook.
- Triaged his Claude scheduled tasks into duplicate daily briefs, local-stack checks that produced nothing decision-relevant, and misconfigured tasks. One farm task had **two faults stacked**: the address used the wrong domain, *and* the farm account sits on a separate gateway key, so it would have failed on credentials even with the address spelled correctly.
- Built a centralized UI over the task output, bringing daily and weekly reports, deadlines, Kenya operations and payables into one view.

**The finding I'm proudest of:** the same context-pack content was, at the same moment, *too present* on one surface and *completely absent* on the other. It was bloating the chat context while never reaching the production prompt at all. Neither symptom can be diagnosed from the other's side. I only found both because I audited the product and the platform it ran on as one system.

### Cost inside Hermes

**The Business Context toggle (14 September)** was the first lever. On a "Hi", input dropped from **20,281 tokens to 148**. Tool definitions had been **65%** of that cost.

**The turn-cost plan (7 October)** went further. It is an 856-line design built from a seven-area read of the code, three candidate designs and three adversarial reviews (safety, code accuracy, cost maths). Every token count was measured offline with the network blocked, and every code claim has a `file:line` reference. The breakdown of a "hi" showed:

- **31 tool schemas** were 12,000–14,000 tokens on their own.
- WhatsApp tools were appended even when WhatsApp wasn't enabled.
- A **hidden memory-extraction model call** ran after every non-empty turn.
- **No prompt caching** was configured.

**The turn router** puts that design into code. `decide()` is pure and deterministic, taking about 10–100 µs. It returns one of three tiers:

- **CHAT.** The whole message is a greeting or closer of eight words or fewer. It runs on a 93-token prompt with **no tools at all**.
- **LIGHT.** A closed list of 9 general-knowledge patterns. Off by default.
- **FULL.** Everything else, *and every case of doubt or error*.

Two design choices matter here:

- **A closed allowlist, not a length rule.** In measurement, "short messages are cheap" let 6 of 24 business phrases through.
- **It reads the previous turn.** "Thanks 👍" after a reply that is waiting on an approval might *be* the approval, so it goes FULL.

Logs record only category codes, never message text. A "hi" on the CHAT path measures **121 input tokens**, against ~16,000 today.

*Status:* the router is built, documented and covered by 56 test functions, but it is **not yet wired into the chat path**. It is switched off by default and no runtime code calls it yet. The System view caption that says it "routes each turn" overstates this, and I've flagged it.

---

## 22. Production incidents and what they taught

| Incident | Root cause | Fix | Lesson |
|---|---|---|---|
| **Every route returned 500** (14 Aug) | `HERMES_MAX_TOKENS=""` in a newly imported Vercel project; `int("")` failed while the config module loaded | All 17 numeric env reads routed through safe parsers with documented defaults. A second crash on `EMBED_DIM` led to guarding the remaining 11. **Tests now import the app with all 27 numeric vars blank, and a guard test fails on any new unguarded `int(os.getenv(...))`** | Fix the class of bug, not the instance |
| **Memory extraction silently dead for a month** | Background model retired (HTTP 410); retrieval paths fail open | Health tracking on every fail-open path; fallthrough on 404/410 | Failing open has to be visible |
| **Meeting Copilot could never answer** | Anthropic SDK called with an NVIDIA model ID | Provider-agnostic helper + regression test | Model migrations need routing tests |
| **Six copies of one report sent nowhere** | Boot-time task creation racing across cold starts; unset `OWNER_EMAIL` | Pinned name, partial unique index, one env var | Serverless makes every "on boot" a race |
| **18 tasks' run history deleted** | `ON DELETE CASCADE` | Point-in-time restore into a separate cluster, harvest, re-insert; 14-day retention; export before delete | Deletion policy is data policy |
| **CI red on `main` regardless of changes** | Tests run as scripts under `set -e`; pytest not installed, so tests "passed" without running; a health test demanded credentials CI never has | pytest installed for tests only; the health test asserts *no degradation* instead of *ok* | A green CI that runs nothing is worse than a red one |
| **Generated-file links returned 403** | The model dropped the query string from 500-character presigned URLs | Permanent public prefix with a code-level guard | Design URLs for the model that will copy them |

---

## 23. Testing and quality

| | |
|---|---|
| Test functions (main app) | **523** in 34 files, plus 108 parametrized expansions |
| Most recent full run | **2,390 passed** (8 Oct). The 2 failures are untracked visual tests that were already failing and are unrelated |
| Largest suites | Guardrail bypasses (97), turn router (56), bills (31) |
| Golden payload harness | 19 fixtures snapshotting what each model is actually sent |
| CI | Import smoke test, then `pytest tests/` |
| Satellite suites | Pinecone MCP 85 checks · NuExtract MCP 32+ checks + OAuth flow · GitHub→Jira 71 tests |
| Live verification scripts | Autonomy gate (28 assertions), guardrails (45 checks), artefacts XSS/isolation (38), credits, model providers, RAG, memory, notifications |
| Owner-value tests | Real turns on real data, graded against independently computed ground truth ([§14](#14-multi-agents)) |

Two habits mattered more than the counts:

1. **Grade against ground truth, not plausibility.** The multi-agent tests compared every rating, count and quote to the raw CSVs. "Looks right" is exactly what a hallucination looks like.
2. **When the docs disagree with the code, the code wins, and the docs get corrected.** Several READMEs had drifted (claimed check counts, cron intervals, a "mounted" router that wasn't). This case study uses only the measured values.

---

## 24. Results

**Business outcomes**

- **$31,000 saved per year** from the portfolio money-in/money-out dashboard
- **Usage costs under control.** The context rot behind $1,000+/week (with a $3,500+ peak week) was removed, and inside Hermes a "Hi" went from 20,281 input tokens to 148
- **Managers made self-sufficient.** Farm attendance and payouts, tenancy and rent, and spa outreach are reviewable and actionable without the founder
- **The rent cycle automated.** Invoices issued, late fees applied and sent
- **Every financial figure verified against QuickBooks.** The billing register's amount verification cut 6 plausible proposals to 1 real one plus 5 human referrals
- **Managers alerted by SMS** when the CEO emails
- **25 curated unattended tasks** covering all six businesses, each anchored to a real obligation (lease expiries, rent windows, tax dates, compliance)

**Platform outcomes**

- **Six businesses** under one assistant, isolated by credential at four layers
- **40+ apps** through one custom MCP gateway
- **Three custom MCP servers** deployed, each with dual-mode auth, documented gotchas and test harnesses
- **Batch OCR 4.5× faster** than sequential, with per-file failure isolation
- **A real-time voice agent** with barge-in, locked-down ephemeral tokens, and the same guardrails as chat
- **Six specialist agents** with tool whitelists and per-mode autonomy, tested against ground truth
- **Four code-level guardrails** plus a 97-test bypass suite
- **Autonomy enforced as a capability gate**, not a prompt

---

## 25. Decisions I'd defend

**Run the sensitivity gate twice on binaries.** A filename check is cheap and catches most cases. Running the gate again on OCR'd text, and pulling the bytes back out of storage to do it, is real work for a case most systems would never notice they were missing. A scanned document's *contents* can be protected health information when its *filename* is completely innocent.

**Tune retrieval toward silence.** Injecting nothing beats injecting a weak match. Empty context produces "I don't know"; marginal context produces confident invention.

**Bridge the two identity schemes instead of merging them.** Merging would have been cleaner and riskier: a migration across live financial data. The bridge makes the seam explicit, testable and documented.

**Bind approvals to a hash of the exact arguments, and honour them only on the next turn.** A "yes" approves *this* email to *this* recipient. It doesn't approve whatever the model decides to send next, and the model can't approve its own action in the same turn.

**Build on the owner's Sheet instead of competing with it.** Two registers produce two totals and no way to tell which is right. Hermes reads the register, writes only through a narrow, logged path, and proposes rather than edits.

**Keep the model off the render path.** Artefacts use AI once, at creation. Re-opening one is deterministic: free, fast, and unable to hallucinate.

**Make Gemini the voice, not the brain.** A second brain would mean a second set of guardrails, a second retrieval path and a second chance to fabricate. Routing every spoken question through `/api/chat` keeps one source of truth.

**Process backfills one day per run.** This trades elapsed time for reliability, which is the right trade when the output drives decisions.

**Document the limits of the client surface instead of quietly working around them.** The 30 KB and 60-second ceilings could have hidden behind a retry. Writing them down means the next engineer meets a documented constraint instead of an unexplained failure.

**Send every doubtful turn to the full path.** The router saves money only on turns it is certain about. A cost optimisation that occasionally strips the guardrails from a business turn is not an optimisation.

---

## 26. What is still open

Being honest about this list is part of the bar:

1. **Turn the router on.** It is built and tested but not wired in. The next step is Phase 0 measurement, then enabling the CHAT tier behind its flag.
2. **Make the full corpus reachable.** Turn on Pinecone federation in production and re-count embedding coverage. The July figure was 0.3%.
3. **Clean up migration numbering.** Resolve the colliding prefixes and fold the separate bills migrations into one trusted ledger.
4. **Wire in Comfort PCA.** It needs a business slug and a gateway key before it can be scheduled or queried live.
5. **Finish ADR-001.** Pagination and the deterministic money-claim footer are still on the deferred list.
6. **Web tools on non-Claude models.** Web-based agents need Claude until web search is available on the OpenAI-protocol path. Anthropic credit was exhausted on 8 October, so the tightened Research Agent prompt still needs a re-test.
7. **Mount the GitHub→Jira automation** in the main app, or deploy it on its own.
8. **Link the register Sheets by ID** in the task prompts, so lookups are deterministic and cheaper.

---

## 27. Lessons

1. **An instruction is not a control.** Every safety property that lived only in a prompt — autonomy, write confirmation, governance rules — turned out to be missing, bypassable or silently unloaded. Every one that held was enforced at a choke point in code.
2. **Silent failure is the most expensive kind.** The empty governance block, the 0.3% search, the dead memory extraction and the CI that ran nothing all *looked* healthy. Health reporting is now a feature, not a log line.
3. **Cost and quality are the same problem.** The context that inflated the bill was the same context that degraded the answers. Removing it fixed both.
4. **Ground truth beats plausibility.** The tests that mattered compared output to independently computed numbers. The model that "looked fine" invented a denominator.
5. **Fit the system to how the person works.** Kenya-window timing, headline-first formatting, "one line and stop", voice as a first-class way in. Adoption came from fitting his week, not from adding features.
6. **Document honestly, especially your own gaps.** The status legend, the "five things" table and this open-items list are what make the rest of the document believable.

---

## Appendix A — Anonymization

For public use, substitute throughout:

| Real | Public |
|---|---|
| Afro Farm Holding | a million-dollar agricultural operation in East Africa |
| GAK Properties, GIF Properties | two US commercial property entities |
| GAK Commodities | a commodities entity |
| Nour Hammam | a spa and hospitality business before launch |
| Comfort PCA / Comfort Services | a care-services entity handling protected health information |
| Gandi, named staff, tenants and counterparties | "the founder", "the farm manager", "a tenant" |
| Property addresses, parcel counts, lease names | omit |
| Account addresses, connection identifiers, host names, URLs | omit |
| Haider Ali | keep, with his consent, or "a second engineer" |

Keep all architecture, all measured performance and cost figures, all financial outcomes and all audit findings. None of them identify the client, and they are the substance of the case.

## Appendix B — Status legend

| Label | Meaning |
|---|---|
| **LIVE** | Verified working in production |
| **SHIPPED** | Committed and wired, not production-verified |
| **BUILT, OFF** | Code and tests exist; disabled by default or not called at runtime (e.g. the turn router, Pinecone federation) |
| **WORKING TREE** | Uncommitted |
| **AUTHORED, NOT LOADED** | Content written that never reached the database |
| **DOC-ONLY** | Described in markdown; no code exists |

## Appendix C — Where the numbers come from

| Figure | Source |
|---|---|
| Code size, routes, tools, migrations, test functions | Measured from the repository on 2026-10-09 |
| 2,390 tests passed; multi-agent latency and cost | `docs/reports/MULTI_AGENTS_TEST_RESULTS.md` (2026-10-08) |
| Token costs of a "hi"; router figures | `docs/architecture/TURN_COST_PLAN.md` (offline tiktoken measurement) |
| 20,281 → 148 tokens | Commit message of the Business Context toggle (2026-09-14) |
| Task curation figures | `docs/features/SCHEDULED_TASKS_CATALOG.md` (audited 2026-09-14) |
| July audit findings | `docs/project/PROJECT_CONTEXT.md` (audited against production 2026-07-27) |
| NuExtract batch timings | NuExtract MCP README (measured in production) |
| $31,000/year, 40+ apps, Artifact dashboards, Twilio SMS | Confirmed by the client; these live outside this repository on the Claude Artifact/Cowork surface and in external services |
| Usage-credit spend before the engagement | Client's billing, as reported by the client |
