# AI Personal Assistant & Business Command Center

**A case study by Sawera Khadium**
Six businesses · 40+ apps connected to Claude · three custom MCP servers · $31,000/year saved

> **On names.** I've used the real entity names here because this is my working
> record. For public use, apply the substitution table in
> [Appendix A](#appendix-a--anonymization).

---

## What I was walking into

My client runs six businesses across three countries: a million-dollar farming
operation in Kenya, three US property and commodity entities, a spa, and his
personal and family affairs. Each one had its own email, calendar, drive,
accounting, messaging and meeting-notes accounts. There was no single place to see
money in and money out. His managers couldn't see their own operations without
asking him first. And the businesses have separate legal standing — one of them
handles protected health information — so nothing could be solved by simply
pooling the data.

He had already reached for Claude on his own, and hit three walls.

**Cost.** He was spending **$1,000+ per week in usage credits on top of a
$200/month Max plan.** One week the bill went past **$3,500**.

**Degradation.** Claude was getting dumber the more he installed. Unnecessary
skills, MCPs and plugins. Multiple memory markdown files contradicting each other.
A local database nobody had established the value of. Every one of them claiming
context on every turn.

**Trust.** On 25 July 2026 the agent invented a payroll figure — roughly
KES 900,000 — and stated it with complete confidence. He runs payroll and tenancy
decisions off these answers. One confident wrong number cost him more trust than
ten missing features would have.

That third wall set my bar for everything after it.

---

## What I built

### 1. Claude Artifact dashboards — one for every business

This is the work his managers actually touch. I built purpose-built Artifact
dashboards for all six businesses, designed so the people running each operation
could open them and work — not just the founder.

#### Farm operations (Afro Farm Holding, Kenya)

A million-dollar farming operation sitting on **years of accumulated operational
data** that had never been navigable in one place.

I brought the whole operation into a single manager-facing view:

- **The full operational history, made queryable.** Years of records that had been
  archived rather than usable. A manager can now look at a season, a crop cycle,
  or a cost line against its own history instead of against memory.
- **Attendance reports driving payouts.** This is the part with real money
  attached. Attendance data surfaces as structured reports feeding payroll for
  roughly 54 staff paid in Kenyan shillings — attendance in, payout figures out,
  visible to the manager approving them instead of reconstructed by hand every
  cycle.
- **Built for review and sign-off.** Because managers are approving numbers that
  become someone's wages, I presented every figure alongside the records
  underneath it rather than as a total on its own.

I wired the Kenya-specific operational context in alongside it: GLOBALG.A.P.
compliance, market prices, and maize-price forecasting pulled from World Bank Open
Data.

#### Spa (Nour Hammam) — drafted email, sent from the dashboard

I closed the loop between insight and action. Claude drafts suggested emails to
contacts, and **those drafts send to the contacts directly from the dashboard
UI.** No copying into a mail client, no context switch, no re-typing.

The founder or a manager reads what Claude proposes and sends it from the same
screen where the business context lives. Draft, review, send — one place.

#### Rental properties (GAK Properties, GIF Properties)

A landlord dashboard across four addresses — 720 E Lake, 2609 15th Ave S,
1229 E Lake and 1230 Logan — covering the full rent cycle:

- **Tenant tracking** — leases, tenancies and rent status per unit, per property,
  per entity.
- **Automatic invoicing** — rent invoices generated and sent to tenants on
  schedule instead of assembled by hand each month.
- **Automatic late fees** — when rent passes its due date, the fee is applied and
  the notice goes to the tenant automatically.

I prioritised this one because it's where small property portfolios quietly leak
money: a late fee nobody had time to check, an invoice that went out three days
late. Automating it removes the leak, and it removes the awkward conversation too.

#### Money in / money out — $31,000 saved per year

A portfolio-wide cash dashboard across all six businesses: what came in, what went
out, where it went, what's owed. Every figure verified against QuickBooks rather
than trusted from a spreadsheet.

**This dashboard saves him $31,000 a year.** It's the clearest financial return of
the engagement, and it came from making cash position continuously visible across
entities instead of reconstructed periodically, one entity at a time.

#### Commodities and Personal

GAK Commodities and Personal & Family complete the six, each scoped to its own
data with the same isolation guarantees as the rest.

---

### 2. The command center behind the Artifacts

Behind the dashboards I built a full production application.

**Stack:** FastAPI · AWS RDS Postgres 17.7 with pgvector · S3 · Jinja2 and vanilla
JavaScript with no build step · deployed as a single Vercel Python serverless
function. Claude drives the agent loop; I put a faster Haiku model on
latency-sensitive live meeting guidance so the expensive model isn't in that path.

**Seven tabs:**

| Tab | What it does |
|---|---|
| **AI Agent** | Streaming chat over SSE; voice mode (speech-to-text → agent → text-to-speech); file attachments uploaded once and reused across turns; conversation history persisted and reopenable; a per-turn trace of which tools and apps the agent actually used; a platform-wide model switcher |
| **Billing & Invoices** | Upload a bill → OCR → structured record (vendor, amount, invoice number, due date, category); automatic business attribution; proactive Gmail and Drive **scan** for new bills; **auto-mark-paid** by cross-referencing Gmail for payment confirmations; portfolio-wide insights |
| **Meeting Copilot** | Live grounded guidance during meetings; pre-meeting briefs generated against a specific calendar event; post-meeting summary with recording link; full session replay |
| **Business Context** | Per-business knowledge base — upload, edit, delete, with editing a document body triggering re-embedding; a Memories sub-tab showing exactly what the agent has retained, and letting him delete any of it |
| **Scheduled Tasks** | Recurring unattended agent runs — 21 live |
| **File Explorer** | Read-only two-pane browser over each business's raw S3 objects |
| **Jobs** | Registered Python functions on a cron |

**Separation that holds structurally.** Every request is locked to one business or
explicitly scoped to "All." I gave each business **its own Maton API key**, so
cross-business access is prevented by credential boundary, not by asking the model
nicely. A business with no configured key returns a clear "not connected" instead
of silently falling through to a sibling entity's account.

Two parallel business-identity schemes had grown up from separate features. I
bridged them with a single module rather than unifying them — cleaner on paper,
but a unification would have been a risky migration across live financial data.
The bridge makes the seam explicit and testable.

**Unattended runs with a real safety ceiling.** My mental model for Scheduled
Tasks is *a saved chat turn plus a clock* — same agent, same tools, same business
scoping, fired automatically instead of typed. He can create one conversationally:
*"Every morning at 7 give me a farm briefing from AFK Gmail and Calendar,"* and
the agent infers schedule, business, apps and autonomy, asking when it's ambiguous.

Since nobody's present to approve writes, I made autonomy a declared ceiling:
`readonly` (read, analyse, report), `draft` (may create Gmail drafts, never send),
and `full` (may send and modify — an explicit standing authorization). Tasks can
be locked to a subset of apps, narrowing the toolset accordingly.

I also built **date-window backfills**, where one task processes a single calendar
day per run across a bounded range — *"read each day's WhatsApp for a month, one
focused batch per fire."* It turns a job too large for any context window into a
sequence of small grounded ones.

---

### 3. Three custom MCP servers

Nothing off the shelf covered what he needed, so I built three: **Pinecone**,
**NuExtract** and **Maton**.

#### Pinecone MCP — the knowledge connector

Exposes his vector index to Claude as a custom connector. I reproduced the main
application's security guards inside it rather than trusting the caller: one
namespace per call, active records only, and a **sensitivity ceiling hard-capped
in code** so restricted material can never be served no matter what's asked for.

The hard part was authentication. Claude's custom-connector dialog offers only a
URL and optional OAuth credentials — there's no request-header field, so a
token-only server can't be added at all. I ran it **dual-mode**: Google OAuth for
claude.ai, Desktop and mobile, delegating identity to Google through a transparent
proxy with an explicit email allowlist; and a static bearer token for Claude Code
and the Messages API. Which modes are live depends purely on which environment
variables are set.

One thing I found and wrote into the README for whoever comes next: FastMCP's
DNS-rebinding guard rejects any request whose `Host` header isn't localhost —
which is *every* request from Anthropic's cloud. Miss the public-host variable and
the connector fails at handshake with no useful error.

#### NuExtract MCP — reading floormaps correctly

**The problem:** standard PDF readers only pull an existing text layer, so scanned
invoices, phone-photo bills and architectural **floormaps** come back empty.
Anything they do return is unstructured prose that still needs manual re-keying,
one file at a time. Floormaps in particular — the documents defining how a
physical business is laid out — were simply unreadable.

**What I built:** a connector that does real OCR on the page itself and returns
schema-locked JSON, with fields copied exactly as written rather than paraphrased.
Four tools: single-file read and structured extract, plus **batch variants taking
up to 20 files per call** with server-side concurrent fan-out.

Measured in production:

| Workload | Batched | Sequential |
|---|---|---|
| 20 PDFs | **12.5 s** | ~56 s |
| 12 PDFs | **7.6 s** | ~34 s |

A batch costs roughly its slowest file rather than the sum. I built per-file
failure isolation, so one corrupt document returns its own error while the other
nineteen still succeed. Every fetch goes through an SSRF guard — HTTP(S) only,
private, loopback and cloud-metadata addresses refused, **re-validated on every
redirect hop** — with a 20 MB cap. 31 of 31 tests passing, and I branded the OAuth
consent screen.

Floormaps now come back as clean tables. I wired the workflow end to end: a
floormap plus a question returns extracted structure formatted as an accurate
table, and that structure feeds team and space planning downstream in the Meeting
Copilot.

**On operating limits.** Testing from the claude.ai chat surface, I found two
undocumented ceilings: inline file payloads fail above roughly **30 KB**, and tool
calls die at about **60 seconds** — client-side, regardless of server timeout.
These are the *surface's* limits, not my connector's. I documented the workaround
in the README (pass a presigned URL and let the server fetch up to 20 MB itself;
use Desktop or Cowork to clear both ceilings) so the next engineer meets a
documented constraint instead of an unexplained failure.

#### Maton MCP — 40+ apps on one connection

**One MCP server connecting Claude to 40+ apps.** Maton holds each business's
OAuth tokens and proxies to every app's native REST API, so Claude reaches the
entire tool estate through a single authenticated surface instead of forty
separate connectors — each with its own auth, its own failure mode, and its own
context cost. Given that context cost was the thing bankrupting him, consolidating
forty connectors into one was as much a cost decision as an architectural one.

I built dedicated tools where the interaction is rich — Gmail, Calendar, Drive,
Jira, Zoom, WhatsApp — and a universal request tool that reaches any endpoint of
any connected app for everything else. Connection management is surfaced in the
dashboard with live active/pending/failed status per business, and the agent can
start a new connection flow conversationally.

The per-business key architecture means this single gateway is also the isolation
boundary: **one connection point, six sealed compartments.**

---

### 4. One context knowledge layer for decision-making

I built a **separately deployable** service that reads each business's Gmail,
Drive and meeting notes on a schedule and fills the knowledge layer ahead of time,
so the live meeting copilot never scans an inbox mid-conversation. It shares his
database, vector index and object storage purely through environment variables,
and writes vectors in the exact metadata shape the main app's guarded search
expects.

Three rules define it:

1. **Prep before, never during.** The copilot reads pre-computed vectors; this
   service fills them in on autopilot.
2. **Idempotent by content hash.** Unchanged content is never re-embedded, so it
   can run every ten minutes over the same inbox and bill only for genuinely new
   material.
3. **The sensitivity gate sits below the model.** Before anything reaches object
   storage, a cloud OCR processor or the embedding API, a gate decides. Protected
   health information, immigration material and anything marked restricted are
   recorded in the database only and never transmitted. For binaries the gate runs
   **twice** — once on filename and metadata before OCR, then again on the OCR'd
   text, pulling the bytes back out of storage if the second pass blocks. The
   principle I wrote into its README: *reachability is not locality of
   processing.*

Underneath sits an account architecture I mapped and verified: one shared gateway
key holding four Google accounts, a second key isolating the farm operations
account, and **per-business relevance filtering on shared inboxes** — filter terms
derived automatically from each business's context — so two entities sharing a
mailbox still can't read each other's mail.

**What the decision layer feeds.** I built the Meeting Copilot to draw on **five
concurrent data layers** so guidance during a meeting is specific and sourced
rather than generic:

1. **Meeting context** — prior decisions, open action items, attendees and roles,
   earlier conversations in the same business
2. **Business strategy** — quarterly goals, roadmap and timeline, resource
   constraints, budget and hiring plans, milestones
3. **Team and organizational structure** — dependencies drawn from floormap data,
   current blockers, capacity and commitments, ownership
4. **Historical context** — email history with the attendees, past decisions and
   how they turned out, recurring themes, document evolution
5. **Risk and compliance** — known technical and business risks, regulatory
   requirements, prior incidents, dependency chains

I held the guidance to four standards: **specific** (never generic advice),
**grounded** (cites the data it came from), **actionable** (clear what to do next)
and **concise** (three bullets maximum per turn). It also detects conflicts against
past decisions and never repeats itself within a session.

---

### 5. Integrations — 40+ apps, messaging, verified books

**The app estate.** I connected **40+ applications to Claude**, all through the
single Maton gateway with per-business OAuth: Gmail, Google Calendar, Drive,
Sheets, Docs, Forms, Contacts and Meet; QuickBooks; Notion; Jira; Zoom and Zoom
Admin; WATI; Fathom; PDF.co; and the wider estate reachable through the universal
request tool.

**WhatsApp — Claude sends messages.** I connected WhatsApp to Claude through the
Maton–WATI integration with full business operations: contact listing and
creation, attribute updates, message history, **sending messages**, sending files,
operator assignment and media retrieval — with media archived to S3 alongside
everything else. I proved it at scale by pulling and analysing a complete month of
one business's WhatsApp history in a single run.

**Twilio — managers get a text when the CEO emails.** Email alone doesn't reach a
farm manager standing in a field or a property manager between showings. So I
doubled the notification path: **when an email goes to a manager, Twilio
automatically sends a text message to that manager's phone telling them the CEO
has sent an email.** The email carries the content; the SMS guarantees they know
it arrived. No more messages sitting unread for a day because nobody was at a
desk.

**QuickBooks — every number verified.** I connected QuickBooks specifically to
**verify all the numbers in the backend sheets**: AR/AP aging, P&L, balance sheet,
invoices, estimates, customers, product and sales summaries, and payroll data.
This is the check that makes the money-in/money-out dashboard trustworthy —
figures aren't carried forward from a spreadsheet on faith, they're reconciled
against the books. It's also what makes rent-arrears analysis across the property
entities reliable enough to act on.

---

## How I responded to the fabrication incident

After the invented payroll figure, my client set the bar: **zero hallucination,
zero assumptions, provenance-first.** I wrote a formal architecture decision record
and enforced a set of rules:

- **Recall is retrieval, never generation.** Stored, timestamped, source-tagged
  records get injected verbatim as citable evidence.
- **Strict attribution.** The model may say "you told me" or "we discussed" only
  about text that literally appears in a recalled record, with its date and
  source. Otherwise the answer is "I don't have that" — never an inference, never
  an estimate.
- **Threshold tuned toward silence.** Below the score threshold I inject
  *nothing*. Weak matches invite guessing; an empty context produces an honest "I
  don't know" while a marginal one produces a confident fabrication.
- **Conflict resolution.** Newest record wins, the superseded one is marked rather
  than deleted. Stale facts are never presented as current.
- **Summaries kept extractive.** LLM-generated summaries are the one genuine
  hallucination surface in a retrieval system, so I kept them navigational, with
  every factual claim tracing back to a verbatim turn.
- **Dual provenance.** Every recalled item carries both *when* and *which session
  and business*.

This is shipped and live.

### Then I turned it on my own work

The project's context document opens with a table I wrote, headed **"five things
that are not what they look like,"** each row carrying its own evidence citation:

1. **Context packs were dead in production.** The backing tables didn't exist — a
   migration had never been applied — and the loader swallowed the error into a
   log warning and returned an empty string. Every production turn had been
   running with an empty context block. The packs I'd authored, *including the
   non-negotiable anti-fabrication rules*, had never once reached the prompt.
2. **The knowledge base was ~9 reachable documents, not 15,000.** Of 41,749 stored
   chunks, only 124 carried an embedding — and the search filtered on exactly
   that. The agent could reach **0.3%** of ingested content. 15,380 ingested emails
   and Drive files were invisible to it.
3. **The autonomy ceiling wasn't enforced.** The agent entry points took no
   autonomy argument, so a `readonly` scheduled task with Gmail enabled was still
   being handed the send-email tool.
4. **Chat write-confirmation was prompt-only too** — a single system-prompt
   bullet. Exactly one write gate in the codebase was structurally enforced: the
   email-with-attachment flow, a deterministic prepare-then-send sequence that
   only sends on explicit confirmation.
5. **Schema drift.** Production carried a migration present in no branch of the
   repository, adding a column no code reads, while a migration on an unmerged
   branch was absent. The migrations ledger wasn't trustworthy.

I ran a status legend through all the documentation distinguishing **LIVE**
(verified in production) from **SHIPPED** (committed, not verified), **WORKING
TREE**, **AUTHORED, NOT LOADED** (content written that never reached the database)
and **DOC-ONLY** (described in markdown, no code exists).

I consider that legend part of the deliverable. I was building a system whose
whole purpose is never overstating what it knows. It would have been incoherent to
document it any other way.

---

## Fixing the context rot

Alongside the build, I audited and cleaned his Claude and Cowork surfaces —
treating an AI installation the way I'd audit infrastructure.

**What I found.** Usage cost and answer quality had decoupled: more installed
capability, worse output, higher bills. The causes were cumulative.

- Unnecessary **skills, MCPs and plugins**, each claiming context on every turn
- **Multiple memory markdown files** competing and contradicting each other
- A **local database** in the mix whose value had never been established
- An **oversized context-pack file** loaded in full on every turn and never
  reliably read end to end

The result was **$1,000+ per week in usage credits on top of the $200 Max plan,
one week past $3,500** — and a model getting dumber as it got more expensive.

**What I did.**

- **Temporarily disabled Claude's memory features** to cut context noise — a
  deliberate, reversible choice, and I recorded it so recall gaps would read as
  expected behavior rather than a new fault.
- **Removed Claude's access to the oversized context-pack file.**
- **Cleaned and simplified the Claude and Cowork instructions** — after preserving
  the previous versions in Notion. That step is what separates a cleanup from data
  loss, and I did it first.
- **Disabled unnecessary skills, connectors and plugins**, and removed extra
  skills outright.
- **Opened the local database triage** — reviewing it through a Colab notebook to
  work out what's worth keeping long-term versus discarding.

**The finding I'm proudest of:** the same context-pack content was, at the same
moment, *too present* on one surface and *entirely absent* on the other — bloating
the chat context while never reaching the production prompt at all. Neither
symptom is diagnosable from the other's vantage point. I only found both because I
was auditing the product and the platform it ran on as one system.

**Scheduled-task audit.** I reviewed his existing Claude scheduled tasks and
triaged them into three buckets: **duplicate daily briefs** doing overlapping
work, **unnecessary local-stack checks** producing nothing decision-relevant, and
**misconfigured tasks** — notably a farm task that couldn't reach its operations
mailbox. Tracing that one, I found two faults stacked in a single task: the
configured address used the wrong domain, *and* the farm is the one business whose
account sits on a separate gateway API key, so it would have failed on credentials
even with the address spelled right. Reconfiguration is my current open item.

**A centralized task dashboard.** I built a UI over the scheduled-task data,
bringing daily and weekly reports, deadlines, Kenya operations, and bills and
payables into one place — separate from the seven-tab command center, aimed
specifically at the recurring-automation output.

---

## Results

- **Six businesses** running under one assistant with credential-level data
  isolation
- **40+ apps connected to Claude** through a single custom MCP gateway
- **$31,000 saved per year** by the portfolio money-in/money-out dashboard
- **Usage costs brought under control** — from $1,000+/week over a $200 plan with
  a $3,500+ peak week, by eliminating the context rot causing it
- **Managers made self-sufficient** — farm operations, attendance and payouts,
  tenancy and rent, and spa contact outreach all reviewable and actionable in
  dashboards instead of routed through the founder
- **The rent cycle automated** — invoices issued, late fees applied and sent,
  without manual intervention
- **Every financial figure verified against QuickBooks**
- **Floormaps and scanned documents made readable** — batch processing **4.5×
  faster** than sequential, with per-file failure isolation
- **Three custom MCP servers deployed**, dual-mode authenticated, one at 31/31
  tests passing
- **WhatsApp, Twilio, Pinecone and Jira live and tested** — Jira verified on both
  read and write
- **Anti-fabrication hardening shipped** — the recall path is retrieval-only with
  dual provenance on every item
- **21 unattended automations** live with a graduated autonomy ceiling

---

## Five decisions I'd defend

**I run the sensitivity gate twice on binaries.** A filename check is cheap and
catches most cases. Running the gate again on OCR'd text — and pulling the bytes
back out of storage to do it — costs real work for a case most systems would never
notice they were missing. I built it because a scanned document's *contents* can
be protected health information when its *filename* is completely innocuous.

**I tuned the retrieval threshold toward silence.** Injecting nothing beats
injecting a weak match. An empty context produces "I don't know"; a marginal one
produces confident invention. Most retrieval systems tune the other way, and after
the payroll incident I wasn't willing to.

**I bridged the two identity schemes instead of merging them.** Unifying would
have been cleaner and riskier — a migration across live financial data. The bridge
module makes the seam explicit, testable and visible in documentation rather than
buried where the next person finds it the hard way.

**I made backfills process one day per run.** Rather than fight the context window
on a month of message history, the task becomes a sequence of small grounded runs
with a persisted cursor. I traded elapsed time for reliability, which is the right
trade when the output drives decisions.

**I documented the surface limits instead of quietly working around them.** The
30 KB and 60-second ceilings could have disappeared behind a retry. Writing them
into the README means the next engineer meets a documented constraint instead of
an unexplained failure.

---

## Appendix A — Anonymization

For public use, substitute throughout:

| Real | Public |
|---|---|
| Afro Farm Holding | a million-dollar agricultural operation in East Africa |
| GAK Properties, GIF Properties | two US residential property entities |
| GAK Commodities | a commodities trading entity |
| Nour Hammam | a spa and hospitality business |
| Comfort Services | a care-services entity handling protected health information |
| Named individuals | "the founder," "the owner," "the managers" |
| Specific property addresses | omit entirely |
| Account addresses, connection identifiers, host names | omit entirely |

Keep all architecture, all measured performance figures, all financial outcomes
and all audit findings. None of them identify the client, and they're the
substance of the case.
