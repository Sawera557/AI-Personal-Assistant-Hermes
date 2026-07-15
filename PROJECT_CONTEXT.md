# Hermes — Full Project Context

A single reference to *what this project is*, *every app it connects to through
Maton*, *everything it can do right now*, and *how to make it work for you* —
including scheduling recurring work by just telling Hermes in chat.

Keep this current when apps, tools, or capabilities change. For the deep dive on
recurring automation see [SCHEDULED_TASKS.md](SCHEDULED_TASKS.md).

---

## 1. What Hermes is

Hermes is an AI executive assistant for **Gandi**, a founder running a
multi-entity portfolio. It answers in chat and — through the **Maton** API
gateway — reads and acts across the connected accounts of each business (email,
calendar, drive, accounting, messaging, meetings, notes). The same agent can now
run **on a schedule, unattended**, via Scheduled Tasks.

**Dashboard tabs**
- **AI Agent** — the chat assistant (voice-capable).
- **Billing & Invoices** — invoice/billing views.
- **Meeting Copilot** — meeting prep & guidance from your data.
- **Business Context** — the per-business knowledge base (ingested context files).
- **Scheduled Tasks** (⏰) — recurring, unattended agent runs. See §5.

---

## 2. Businesses (entities)

Every request is scoped to one business (locked per conversation) or to **All
Businesses**. Each business has its own connected accounts via a per-business
Maton key, so one business can never see another's data.

| Slug   | Business                     | Notes |
|--------|------------------------------|-------|
| `afk`  | Afro Farm Holding (Kenya)    | Farm ops, ~54 staff payroll (KES), GLOBALG.A.P., market prices |
| `gak`  | GAK Properties LLC           | Landlord — leases, rent, tenants (720 E Lake) |
| `gif`  | GIF Properties LLC           | Landlord — leases, rent, tenants (2609 15th Ave S, 1229 E Lake, 1230 Logan) |
| `gakc` | GAK Commodities Ltd          | Trading / commodities |
| `nh`   | Nour Hammam LLC              | Spa pre-launch (buildout at 720 E Lake) |
| `per`  | Personal & Family            | Personal inbox, father's care, prayer/halal-aware scheduling |

`per` intentionally uses the shared/default Maton key (the original personal
inbox); the other five each have a dedicated business key.

---

## 3. Connected apps (via Maton)

Maton is the gateway: it holds each business's OAuth tokens and proxies calls to
each app's native REST API. Hermes reaches every app below either through a
**dedicated tool** (email, calendar, drive, …) or through the universal
`maton_request` tool (any endpoint of any connected app).

| App slug          | What it is | Typical use |
|-------------------|-----------|-------------|
| `google-mail`     | Gmail | Read/search/triage email; send; create drafts |
| `google-calendar` | Google Calendar | Read upcoming events; create/update/delete events |
| `google-drive`    | Google Drive | Read files (PDF/images as real content), search |
| `google-sheets`   | Google Sheets | Read/write spreadsheet ranges |
| `google-docs`     | Google Docs | Read/write documents |
| `google-forms`    | Google Forms | Read form responses |
| `google-contacts` | Google Contacts | Look up contacts |
| `google-meet`     | Google Meet | Meeting info |
| `quickbooks`      | QuickBooks (accounting) | AR/AP aging, P&L, balance sheet, invoices, customers, payroll data |
| `notion`          | Notion | Search workspace; read/create/update pages & database rows; comments |
| `zoom`            | Zoom (your account) | Your meetings, recordings, webinars, registrants |
| `zoom-admin`      | Zoom Admin | Account-wide: any user's meetings/recordings/settings |
| `wati`            | WATI (WhatsApp Business) | Contacts, messages, send text/file, assign operator, media |
| `fathom`          | Fathom | Meeting notes & recordings |
| `pdf-co`          | PDF.co | PDF parsing / OCR / conversion |

Plus non-Maton data sources: **World Bank** (public macro indicators) and, when
enabled, **Web search / fetch**.

> Connection status is live — `maton_list_connections` shows which apps are
> actually connected (ACTIVE/PENDING/FAILED) for the current business. A business
> without a configured key returns a clear "not connected" message rather than
> silently using another business's account.

---

## 4. Capabilities — what Hermes can do right now

Grouped by domain. Everything respects the locked business scope.

**Email (Gmail)**
- Read, search, and triage the inbox; summarize threads; rank by urgency.
- Send email (`send_email`) and create drafts (raw Gmail via `maton_request`).

**Calendar**
- List upcoming events (`calendar_list_upcoming`); create/update/delete events and
  check availability via `maton_request`.

**Drive & documents**
- Open a Drive file as real content Hermes can read — PDFs and images
  (`drive_read_file`) — and search Drive; read/write Sheets, Docs, Forms.

**Accounting (QuickBooks)**
- AR/AP aging, P&L, balance sheet, invoices, estimates, customers, product/sales
  summaries, and payroll data — via `maton_request` to the QuickBooks API.

**Notion**
- Search the workspace; read/create/update pages and database rows; read/post
  comments.

**Meetings (Zoom / Zoom-admin / Fathom / Google Meet)**
- List/create/update meetings, pull recordings and webinars, manage registrants;
  send a Zoom invite (`zoom_send_invite`); list meetings (`zoom_list_meetings`);
  read meeting notes.

**WhatsApp (WATI)**
- List/add/update contacts, read and send messages, send files, assign an
  operator, fetch media.

**Files**
- Generate a downloadable document/report/CSV/PDF from text Hermes writes
  (`create_file`) and return a working link.

**Knowledge base**
- Semantic search over each business's ingested context files and documents
  (`search_business_knowledge`) — the "Business Context" tab's content.

**Web** (when enabled)
- `web_search` and `web_fetch` for live external information.

**Data**
- `worldbank_data` — World Bank development indicators (e.g. Kenya maize, macro
  trends) for forecasting.

**Scheduled Tasks** (see §5)
- Create, list, and run recurring unattended jobs — now also **directly from a
  chat command**.

**Safety model.** In chat, before any *write* (send email, modify QuickBooks,
delete, etc.) Hermes summarizes the change and waits for your confirmation. For
unattended scheduled runs the same protection is enforced by a per-task
**autonomy** ceiling (see §5).

---

## 5. Scheduled Tasks — and creating them by chat

A Scheduled Task is *a saved chat turn plus a clock*: a locked business, a chosen
set of apps, a prompt, a schedule, and an autonomy ceiling. It runs the **same
agent** the chat uses, automatically, and stores each run's report + tool trace
in the Tasks tab. Full design: [SCHEDULED_TASKS.md](SCHEDULED_TASKS.md).

**Schedule shapes:** `daily`, `weekdays` (Mon–Fri), `weekly` (chosen days,
0=Mon…6=Sun), `hourly`. Times are wall-clock in the task's timezone.

**Autonomy (the unattended safety ceiling):**
- `readonly` (default) — read / analyze / report / generate a file. No sends or changes.
- `draft` — the above + create **Gmail drafts** (never send).
- `full` — may actually send / modify. A standing authorization you set on purpose.

**Where results show:** Tasks tab → click a task → **Run history** → click a run
to read its full report and tool trace. `Run now` fires one immediately.

### Create a task just by telling Hermes

You can now ask Hermes in chat to set up a recurring task and it will create it in
the dashboard for you (tools: `create_scheduled_task`, `list_scheduled_tasks`).
Hermes infers the schedule, business, apps, and autonomy from your wording, and
confirms if anything is ambiguous.

**Examples**
- "Every morning at 7 give me a farm briefing from AFK Gmail and Calendar."
  → daily 07:00, business `afk`, read-only.
- "Each Monday at 9, pull GAK rent arrears from QuickBooks and cross-check email."
  → weekly Mon 09:00, business `gak`, read-only.
- "Every weekday at 8:30, triage my GAK email and draft replies to anything urgent."
  → weekdays 08:30, business `gak`, **draft** autonomy (inferred from "draft").
- "What do I have scheduled?" → Hermes lists your tasks.

Hermes will **not** pick `full` autonomy (auto-send) unless you clearly ask it to
act/send automatically, and it confirms first. Unattended scheduled runs cannot
create new tasks — only a live chat turn can.

---

## 6. Where things live (code map)

| Area | File(s) |
|------|---------|
| Agent loop | `hermes/agent.py` (`run_turn`, `stream_turn`) |
| Tools & dispatch | `hermes/tools.py` (`build_tools`, `dispatch`, `create_scheduled_task`/`list_scheduled_tasks` handlers) |
| Maton gateway | `hermes/maton.py`, per-business keys in `hermes/config.py` |
| Businesses ↔ entities | `hermes/businesses.py` |
| Scheduled Tasks | `hermes/scheduler.py`, `hermes/db/scheduled_tasks.py`, `SCHEDULED_TASKS.md` |
| HTTP API | `app.py` (`/api/chat`, `/api/scheduled-tasks/*`, …) |
| Dashboard UI | `templates/index.html`, `static/*.js`, `static/style.css` |
