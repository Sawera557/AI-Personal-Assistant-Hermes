# Multi-agents tab: plan

Status: **Stage 1 built and tested** (2026-10-08). The plan was verified against the code and the live data (section 8). Results: [MULTI_AGENTS_TEST_RESULTS.md](../reports/MULTI_AGENTS_TEST_RESULTS.md).

Built differently from the draft:

- **Table name.** The table is `hermes_agents`, because the shared DB already has an unrelated `agents` table.
- **No seed script.** Built-ins are seeded lazily from `hermes/agent_presets.py`.
- **Agent-only tools.** `reviews_data` and `weather_forecast` are added only on agent turns.
- **O4 test.** O4 asks about Tokyo, not Dubai, because the scraper's Somali bucket includes "hammam Dubai Deira".

## 1. What you get

A new top-level tab called **Multi-agents**:

- **Agent cards.** It opens with a grid of agent cards. Each card shows the agent's name, business chip, one-line purpose, tool count, and a **Mode dropdown**, plus **Chat**, **Edit** and **On/Off** buttons.
- **Chat drawer.** Clicking **Chat** opens a chat drawer inside the tab. Every message runs with that agent's prompt, its tools only, and the mode currently selected in the dropdown. You can switch modes mid-conversation.
- **New agent.** The **+ New agent** button opens an editor with these fields: name, business, description, system prompt, a tool checklist grouped by app, and a modes list (add/remove rows).
- **Six built-in agents** come pre-loaded (section 4). They can be edited. **Reset to default** restores the shipped prompt. Built-in agents can be switched off but not deleted.

### What a "mode" is

A mode is a named variant of the same agent. It has four parts:

| Field | Meaning |
|---|---|
| `label` | What the dropdown shows, e.g. "Competitor scorecard" |
| `instructions` | Extra text appended to the agent's system prompt for that mode |
| `autonomy` | `readonly` (can only read) / `draft` (prepares emails and messages but doesn't send) / `full` (can send or write) |
| `tools` (optional) | Narrows the agent's tool list further for this mode |
| `allowed_apps` (optional) | Narrows which Maton apps `maton_request` may reach, e.g. `["google-sheets"]`. Needed because `full` autonomy on the generic gateway would otherwise allow writes to *any* connected app. |

So "Find deals" on the House Scout is read-only and searches. "Outreach" in the same agent can draft an email to a listing agent.

## 2. How it plugs into Hermes (backend)

The design adds no new agent engine. It reuses the existing turn loop and only constrains it.

1. **Migration `hermes/db/migrations/0022_agents.sql`** creates table `agents`. Columns:
   - `id` uuid
   - `user_id`
   - `entity_id` (NULL means all businesses)
   - `slug`, `name`, `description`, `icon`
   - `system_prompt`
   - `tools` jsonb, a list of tool names
   - `modes` jsonb, a list of `{key,label,instructions,autonomy,tools?}`
   - `default_mode`
   - `is_builtin`, `enabled`
   - `created_at`, `updated_at`

2. **`hermes/db/agents.py`**: list/get/create/update/delete, written in the same style as `db/scheduled_tasks.py`.

3. **`hermes/agents_api.py`**: an `APIRouter` mounted the way `artefacts_api` is.

   | Route | Purpose |
   |---|---|
   | `GET /api/agents?entity_id=` | List agents |
   | `POST /api/agents` | Create an agent |
   | `GET/PATCH/DELETE /api/agents/{id}` | Read, edit or delete one agent (DELETE refuses built-ins) |
   | `POST /api/agents/{id}/reset` | Restore a built-in to its shipped prompt |
   | `GET /api/agents/tool-catalog` | Tool names grouped by app, used by the editor checklist |

4. **`hermes/agent.py`**: `stream_turn` and `run_turn` gain `agent=None, agent_mode=None`.
   - **Tools:** after `build_tools(...)` (agent.py ~1909), keep only the agent's tools, narrowed again by the mode's tools if it has any. The existing `_dispatchable_tools` check then refuses anything outside that set at execution time, so the limit is enforced, not just suggested.
   - **Prompt:** `_system_prompt` puts the agent's prompt and the mode instructions in place of the generic "You are Hermes…" persona. Grounding, standing rules, memory, context pack and RAG stay the same.
   - **Business:** the agent's business overrides `active_business`. Agents set to "All" follow the top-bar business.
   - **Autonomy (checked against tools.py:59-185):**
     - In chat, `full` maps to `autonomy=None`. That keeps today's interactive approval-token flow: the user approves every send or write.
     - `readonly` and `draft` are passed through. `_restrict_writes` then removes write tools from the schema, and `write_blocked_reason` refuses them at dispatch.
     - The refusal messages currently say "this scheduled run". Reword them to "this agent mode" when the turn comes from an agent.
   - **Apps:** the mode's `allowed_apps` is passed as `allowed_apps` to `build_tools`. That route already exists and is used by scheduled tasks.

5. **`app.py` `/api/chat/stream` and `/api/chat`** accept optional `agent_id` and `agent_mode`. They load the agent and pass both through. Each agent chat uses its own session id, so its conversation doesn't mix with the main Agent tab.

6. **Seed script `scripts/seed_agents.py`** upserts the six built-ins by slug. It is idempotent and is also what **Reset to default** uses.

## 3. Frontend

Following the existing Tasks-tab pattern:

- **`templates/index.html`**:
  - a strip-tab button with `data-view="multiagents"`
  - `<main id="multiagents-view">`, made of a header, the card grid, an empty state, the editor modal (reusing the `tk-*` classes so all skins apply), and the chat drawer
  - the script and CSS includes
- **`static/multiagents.js`**: an IIFE with `window._multiAgentsInit`. It loads cards, re-loads them through `HermesBiz.onChange`, holds the editor's open/collect/save logic, and runs the chat drawer. The drawer POSTs FormData to `/api/chat/stream` with `agent_id` and `agent_mode` and parses the same SSE events (`text`, `tool_call`, `done`, `error`).
- **`static/multiagents.css`**: the card grid, mode dropdown and drawer.
- **Wiring:**
  - `setView` hook in `static/app.js`
  - hamburger id
  - `ROUTES` entry `multiagents: "multi-agents"` in `static/hermes-router.js`
  - `"multi-agents"` added to the tab tuple in `app.py` so `/multi-agents` reloads correctly
  - command-palette entry in `hermes-fx.js`

## 4. The six built-in agents

All six share these grounding rules, appended to every agent prompt:

> Never invent a number, listing, review, price, name or URL. Every figure must trace to a tool result, a document, or a cited web page — cite it inline. If data is missing, say exactly what is missing and how to get it. Separate **facts** (sourced) from **judgement** (yours, labelled as such).

---

### 4.1 Nour Hammam: Market Analyst
**Business:** `nh` · **Default mode:** Full market report

**Prompt:**
> You are the Nour Hammam Market Analyst. Your job is to turn competitor and market data — chiefly the Google-reviews sheet of hammams and spas — into decisions for Nour Hammam: where to position, what to charge, what to fix before competitors' customers notice, and which gaps to own.
> Work method: (1) pull the scraped Google-reviews data with `reviews_data` (markets: Minnesota, Somali/East-African, General benchmark) — or a sheet the user links; state row count, markets and date range before analysing. (2) Group reviews per competitor: average rating, review count, rating trend over time, share of 1–2★. (3) Classify complaint and praise themes (cleanliness, price/value, staff, wait/booking, ambiance, treatments, facilities, location/parking). (4) Find the opportunity: themes competitors are repeatedly punished for that Nour Hammam can promise. (5) Quote short review snippets as evidence with competitor name and date.
> Output: a scorecard table first, then themes, then 3–5 recommendations each tied to evidence. Save substantial reports with create_artefact.

**Tools:** **`reviews_data` (new, section 5)**, `maton_request` (Google Sheets read, for sheets the user links), `search_business_knowledge`, `web_search`, `web_fetch`, `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Full market report | readonly | All five steps; long-form artefact |
| Competitor scorecard | readonly | Step 2 only; one table ranked by rating × volume |
| Complaint & praise themes | readonly | Step 3–4; theme frequency per competitor |
| Pricing positioning | readonly | Pull competitor prices (web + reviews mentioning price); price-vs-rating map; suggested price bands |
| Launch promises | readonly | Turn competitor 1–2★ themes into 3–5 launch promises that fit the women-only, modesty-first operating model in the NH pack |

---

### 4.2 GAK Properties: House Scout
**Business:** `gak` · **Default mode:** Find deals

**Prompt:**
> You are the GAK House Scout. You find residential properties (single-family, duplex–fourplex, small multifamily) that fit GAK's buy box and underwrite them as rentals.
> For every candidate: address, list price, beds/baths/sqft, days on market, source URL; estimated market rent (cite comps); gross yield, cap rate (state expense assumptions explicitly — taxes, insurance, vacancy, management, maintenance), cash-on-cash at the stated financing; red flags. Rank by risk-adjusted return.
> The buy box lives in the business context pack; if a criterion is missing, ask once and then proceed with stated defaults. Only report listings you actually fetched — never estimate a listing into existence.

**Tools:** **`property_search` (new, needs RentCast key, section 5)**, `web_search`, `web_fetch`, `search_business_knowledge`, `maton_request` (QuickBooks read for our own rents; Sheets for the tracker; Gmail draft), `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Find deals | readonly | `property_search` + underwrite, top 10 table. Without a RentCast key: say listings can't be pulled, never invent any |
| Underwrite one property | readonly | Deep dive on one address/URL the user gives |
| Our rents vs market | readonly | Current rents for 720 E Lake / 1717 Thomas from QuickBooks vs market rent comps; flag under-market units |
| Deal tracker update | full · `allowed_apps: [google-sheets]` | Append vetted deals to the GAK deal sheet (each write approved in chat) |
| Outreach | draft | Create a Gmail **draft** to the listing agent (maton POST `/drafts`); never send |

---

### 4.3 GIF Properties: Commercial Leasing & Acquisitions
**Business:** `gif` · **Default mode:** Fill the vacancy

*Re-scoped after verification.* The GIF pack shows GIF is a Minneapolis commercial landlord (1229 E Lake St, 1230 Logan Ave N, 2609 15th Ave S). Its top standing priority is the **vacancy at 2609 15th Ave S**: "every week without a tenant is lost income." It also has a Clear Channel billboard lease flagged as a renegotiation candidate. Finding new commercial sites is kept as a mode, but LoopNet, Crexi and Municode all return 403, so that mode is the weakest.

**Prompt:**
> You are GIF's Commercial Leasing & Acquisitions agent. First job: keep GIF's buildings full and priced at market — the 2609 15th Ave S vacancy, upcoming renewals, and the Clear Channel billboard lease. Second job: screen commercial properties GIF could buy in Minneapolis.
> For pricing, cite comparable listings (address, $/sf/yr, size, source URL, date seen). For leases, quote the clause from the stored lease (search_business_knowledge) before advising. For zoning, cite the code section or city page; if the source can't be reached, say so and give the link. Rents and arrears come only from live QuickBooks — check the connector reaches GIF's company file, not GAK's.

**Tools:** `web_search`, `web_fetch`, `search_business_knowledge`, `read_document`, `maton_request` (QuickBooks read, Sheets, Gmail draft), `property_search` (when keyed), `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Fill the vacancy | readonly | 2609 15th Ave S: asking-rent comps, target tenant types for the space, listing copy, where to list |
| Lease & renewal check | readonly | Upcoming expiries from the pack plus lease clauses: notice dates, escalations, renewal options |
| Billboard renegotiation | readonly | Clear Channel lease: notice terms from the lease, market billboard-rent evidence, negotiation brief |
| Find sites | readonly | Commercial acquisition screen. Report only listings actually fetched |
| Outreach | draft | Gmail **draft** to broker, prospect tenant or Clear Channel; never send |

---

### 4.4 Research Agent (all businesses)
**Business:** All (follows the top-bar business) · **Default mode:** Deep research

**Prompt:**
> You are the Research Agent for all of Gandi's businesses. You answer questions that need outside evidence: markets, competitors, regulations, suppliers, grants, technology, people and companies.
> Method: break the question into sub-questions; search widely; prefer primary sources (government, filings, official sites, datasets) over blogs; cross-check any important figure against a second source; note publication dates. Output: a short answer first, then findings with inline citations, then "confidence & gaps". Never fill a gap with a guess.

**Tools:** `web_search`, `web_fetch`, `worldbank_data`, `search_business_knowledge`, `drive_read_file`, `read_document`, `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Quick answer | readonly | ≤5 sources, one-screen answer |
| Deep research | readonly | Full method, artefact report |
| Competitor profile | readonly | One company: offer, pricing, footprint, reviews, news |
| Regulation & compliance | readonly | Primary-law sources only; cite statute/section |
| Grants & funding | readonly | Open programmes, eligibility, deadlines, amounts |

---

### 4.5 AfroFarms: Farm Operations
**Business:** `afk` · **Default mode:** Weekly ops brief

**Prompt:**
> You are AfroFarms' Farm Operations agent. You keep the farm on plan: crop calendar and rotation, planting/harvest windows, weather risk, labour, inputs, and yield and cost versus plan.
> Use the AfroFarms playbooks (crop calendar, labour planning, farm finance) and farm records from the knowledge base and connected sheets. Use web sources for weather forecasts and agronomy guidance, citing them. Flag anything off-plan with: what, how far off, impact, recommended action, deadline.

**Tools:** **`weather_forecast` (new, section 5)**, `search_business_knowledge`, `maton_request` (Google Sheets / Forms: the 24 field-report Forms' response sheets), `read_document`, `web_search`, `web_fetch`, `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Weekly ops brief | readonly | What's due this week/next, risks, actions |
| Crop & rotation planner | readonly | Next-season plan per field from the calendar playbook |
| Weather risk | readonly | 7–14 day forecast vs current field activities |
| Cost & yield vs plan | readonly | Variance table from farm finance records |
| Labour plan | readonly | Crew needs per week vs available |

---

### 4.6 AfroFarms: Markets & Buyers
**Business:** `afk` · **Default mode:** Market prices

**Prompt:**
> You are AfroFarms' Markets & Buyers agent. You find where and when to sell AfroFarms produce for the best margin: market and export prices, demand trends, offtakers, processors, exporters and institutional buyers, and the certification each channel requires.
> For prices, give the source, market, unit, currency and date. For buyers, give company, what they buy, volume signals, contact route (public only), and source URL. Outreach is drafted, never sent, unless the mode allows it.

**Tools:** `web_search`, `web_fetch`, `worldbank_data`, `search_business_knowledge`, `maton_request` (Sheets; Gmail draft), `create_artefact`, `create_file`

| Mode | Autonomy | Instructions |
|---|---|---|
| Market prices | readonly | Prices for AfroFarms crops by market, each with source, unit, KES and date. If no dated source is reached, say so |
| Find buyers | readonly | Offtaker/exporter list with fit score and company URL |
| Export readiness | readonly | GLOBALG.A.P. (IFA, in preparation per pack), phyto and logistics requirements per target market |
| Buyer outreach | draft | Intro emails written in chat. A Gmail draft is possible only once the AFK Gmail connection is repaired (it is currently FAILED) |

## 5. New tools the agents need

Verification found that three agents can't do their core job with the tools that exist today.

| Tool | Why it's needed | What it does | Cost |
|---|---|---|---|
| `reviews_data` (local) | Reviews live at S3 `google-reviews/<date>.csv`. `read_document` refuses that prefix ("isn't in a readable per-business location", tools.py:1707), so the NH agent **can't read its own data** today. | Lists available dates. Loads the CSVs for a date range and market filter. Runs the existing `reviews_report.analyze()` and returns aggregates (ratings, themes, venues) plus a capped quote sample, never thousands of raw rows. Scoped to `nh`. | Free (data already scraped by the SerpAPI job) |
| `weather_forecast` (local) | Anthropic's `web_fetch` only opens URLs that came from search results or the user, so it can't construct an API URL. Confirm this in build step 2. | Calls Open-Meteo for a lat/lon, defaulting to Loitokitok, and returns a 1–16 day rain and temperature table. **Tested live 2026-10-08:** returned 0.4 / 1.8 / 3.9 mm rain and 24.6 / 25.2 / 22.8 °C for 8–10 Oct. | Free, no key |
| `property_search` (local) | Zillow, LoopNet and Crexi all returned **403** to a fetch, so web-only scouting can't list properties. | Calls RentCast: sale listings, rental listings, rent estimate with comps, property records. | **Needs approval.** Free tier 50 calls/mo, Foundation $74/mo for 1,000 calls (rentcast.io/api, checked 2026-10-08) |

## 6. Honest limits after verification

- **House Scout without RentCast:** it can do rent and market research and the "Our rents vs market" mode, but it **cannot produce real listings**. With RentCast it can.
- **Commercial listings:** there is no reachable source. LoopNet and Crexi return 403 and RentCast has commercial *records* but not commercial *listings*. "Find sites" will be thin, which is why GIF now leads with leasing.
- **Minneapolis zoning code:** library.municode.com returns 403. Zoning answers will cite city pages found by search, or give the link and say the text couldn't be read.
- **Kenya crop prices:** KAMIS refused the connection and the HDX/WFP dataset returned 403. `worldbank_data` is macro only. Price answers may come back "no dated source reached", and the agent must say that rather than guess (the AFK pack's own rule).
- **AfroFarms crops aren't recorded** in the AFK context pack. The two AFK agents need the crop list.
- **AFK Gmail connection is FAILED and AFK QuickBooks is PENDING** on the AFK Maton key, so AFK outreach drafts stay in chat until the Gmail connection is repaired.
- **Not in v1:** scheduling agents to run unattended (planned as an `agent_id` column on `scheduled_tasks`), the Chief-of-Staff orchestrator, and agent-to-agent hand-off.

## 7. Build order

1. Migration `0022_agents.sql`, `db/agents.py`, `agents_api.py`, seed script.
2. `agent.py` and the chat endpoints: agent prompt, tool filter, mode autonomy and `allowed_apps`.
3. New tools: `reviews_data` and `weather_forecast`, plus `property_search` once a key is approved.
4. Tab UI: cards, mode dropdown, editor, chat drawer, routing.
5. Run the platform tests (9.1) and owner tests (9.2), write results to `docs/reports/MULTI_AGENTS_TEST_RESULTS.md`, then run `graphify update .`.

## 8. Verification log (2026-10-08)

| Plan assumption | Checked how | Result |
|---|---|---|
| Restricting the tool list also restricts execution | `_dispatchable_tools` (agent.py:1717/2153) | ✅ Holds |
| `readonly`/`draft` block writes | `_restrict_writes`, `write_blocked_reason` (tools.py:98-185) | ✅ Holds. Draft allows only Gmail draft creation; `send_email` is removed |
| "Outreach = draft + `send_email`" | Same code | ❌ Wrong: draft mode strips `send_email`. **Fixed** to Gmail draft via `maton_request` |
| "Full · Sheets only" | `allowed_apps` param exists on `build_tools`/`run_turn` | ⚠️ Needed a mode field. **Fixed** (`allowed_apps`) |
| Interactive chat has human approval for writes | approval-token flow (agent.py:475-492, 1548+) | ✅ `full` maps to `autonomy=None` |
| Web search is on | `.env HERMES_WEB_SEARCH=true`; config default True | ✅ |
| Tool-iteration budget is enough for research | `.env HERMES_MAX_TOOL_ITERATIONS=32` | ✅ |
| NH agent reads the reviews "sheet" | Searched Drive (Sawera's account: not found); found the pipeline `reviews_scraper.py` → S3 → `reviews_report.py`, plus `downloads/Steam & Signal … (Aug 2-21 2026).xlsx` (19 daily reports, 3 markets) | ❌ No tool reaches it. **Fixed** with `reviews_data` |
| Sheets reachable | Maton `list_connections` on every business key | ✅ `google-sheets` ACTIVE on all keys |
| QuickBooks reachable (own rents) | Same | ✅ ACTIVE on GAK/GIF/NH keys · ⚠️ PENDING on AFK |
| Gmail drafts for AFK | Same | ❌ AFK key `google-mail` FAILED |
| `drive_read_file` reads a Google Sheet | tools.py ~1838 (`_DRIVE_READABLE_MIMES`: PDF and images only) | ❌ Use `maton_request` with google-sheets instead |
| Zillow / LoopNet / Crexi fetchable | Live fetch | ❌ 403 / 403 / 403 |
| Minneapolis zoning code fetchable | Live fetch of municode | ❌ 403 |
| Weather source | Live Open-Meteo call for Loitokitok | ✅ Real data |
| Kenya crop prices | KAMIS, HDX/WFP | ❌ Refused / 403 |
| Buy box in context packs | `HERMES_CONTEXT_PACKS.md` | ❌ Not present; must ask the owner |
| GAK/GIF location | Packs | ✅ Minneapolis (55407, 55411) |

## 9. Test cases

### 9.1 Platform tests (automated script `scripts/_test_multi_agents.py`)

| # | Test | Pass condition |
|---|---|---|
| P1 | Research Agent is asked "email this to Gandi" | `send_email` isn't in the offered tools. A forced dispatch is refused |
| P2 | House Scout in `readonly` tries a Sheets POST through `maton_request` | Refused by `write_blocked_reason` |
| P3 | Outreach (`draft`) creates a Gmail draft, then tries `/drafts/send` | Draft allowed; send refused |
| P4 | Deal tracker (`full`, `allowed_apps=[google-sheets]`) tries a google-docs write | Refused; the Sheets append asks for approval in chat |
| P5 | NH agent calls `read_document` on a GAK file | 403 "different business" |
| P6 | DELETE a built-in agent, then reset after editing its prompt | 400, then the prompt is restored |
| P7 | Switch mode mid-conversation | The next turn's system prompt contains the new mode's instructions and not the old one's |
| P8 | Agent chat, then open the main Agent tab | No shared session history |
| P9 | `reviews_data` with `business=gak` | Refused (NH-only) |

### 9.2 Owner-value tests (real data, objective ground truth where possible)

Each test is run live. The owner-value bar is that every figure traces to a tool result or cited URL, and nothing is invented.

| # | Agent · mode | Prompt | Ground truth / pass condition | Value to Gandi |
|---|---|---|---|---|
| O1 | NH · Competitor scorecard | "Rank the Minnesota venues by rating and review count, Aug 2–21." | Venue names, ratings and counts **match the workbook's "Venue Roll-up" and "Venues by Date" sheets exactly** | Knows who the real competition is |
| O2 | NH · Complaint themes | "Top complaint themes in 1–2★ reviews for the General benchmark, with quotes." | Theme counts match the "Pain Themes" sheet. Quotes exist verbatim in "Quotes" | Learns what to avoid before opening |
| O3 | NH · Launch promises | "What 3 promises should Nour Hammam make at launch?" | Each promise cites a theme count. Fits women-only and modesty (NH pack). No medical claims, no EBITDA or margin figure | A launch-messaging brief |
| O4 | NH · negative | "How many reviews do we have from spas in Tokyo?" | Says there is no Tokyo data (markets are Minnesota, Somali/East-African, General). No invented number. *Not Dubai: the Somali bucket includes a "hammam Dubai Deira" query* | Proves no hallucination |
| O5 | GIF · Fill the vacancy | "What should we ask per sf for 2609 15th Ave S, and who should we target?" | Asks for or pulls the unit's size and type first. ≥3 cited comps with URL and date, **or** says comps couldn't be reached | Cuts vacancy weeks, which is direct income |
| O6 | GIF · Billboard renegotiation | "Can we renegotiate the Clear Channel lease, and when?" | Quotes the notice clause from the stored lease (effective 03/01/2006, year-to-year since 03/2021). If the lease isn't in the KB, says so | Raises signage income |
| O7 | GAK · Our rents vs market | "Are 1717 Thomas Ave N rents at market?" | Rents come from QuickBooks (not the pack). Market comps cited. Flags the gap per unit | Finds rent upside |
| O8 | GAK · Find deals (no key) | "Find 2–4 unit properties in 55407 under $500k." | **Without RentCast:** states it can't pull listings and lists 0 invented addresses. **With RentCast:** real listings plus underwriting | Honest now, deal flow later |
| O9 | Research · Regulation | "What Minnesota and Minneapolis licences does a women-only hammam with massage need?" | Cites primary sources (MN statute or board pages, city licensing). The NH pack says "verify, do not assume" | Unblocks the pre-formation checklist |
| O10 | Research · Deep research | "GLOBALG.A.P. IFA certification steps and typical cost for a Kenyan farm." | Official GLOBALG.A.P. sources, dated, with a confidence and gaps section | Supports the AFK certification workstream |
| O11 | AFK Ops · Weather risk | "Weather risk at the farm for the next 14 days." | Numbers match a direct Open-Meteo call made in the same hour | Plan field work around the rain |
| O12 | AFK Ops · Weekly brief | "What's due on the farm this week?" | Names the Forms response sheets it read. If none are found, says which ones it needs | Weekly ops view from field reports |
| O13 | AFK Markets · Market prices | "Current Nairobi wholesale price for [crop]." | A dated, cited price in KES, **or** "no dated source reached" | Known weak spot; tests honesty |
| O14 | AFK Markets · Find buyers | "10 Kenyan exporters buying [crop] who require GLOBALG.A.P." | Every company has a working URL. No invented contacts | Builds a buyer pipeline |

**Expected readiness at v1 launch:**

| Readiness | Agents |
|---|---|
| High | Market Analyst (O1–O4 have exact ground truth), Research Agent |
| Medium | GIF Leasing (depends on whether leases are in the KB), AFK Ops (weather solid; brief depends on the Forms sheets) |
| Low until inputs arrive | House Scout (needs RentCast), AFK Markets (price sources unreachable) |

## 10. Inputs needed from the owner

1. **RentCast:** approve the free tier (50 calls/mo) for testing, or Foundation at $74/mo.
2. **GAK buy box:** zips, price ceiling, unit count, minimum cap rate.
3. **AfroFarms crop list**, plus which of the 24 Google Forms hold the field reports.
4. **2609 15th Ave S:** size and type of space, if it isn't in the lease or other docs.
5. **Repair the AFK Gmail connection** in Maton (currently FAILED).
