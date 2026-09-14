# Scheduled Tasks — owner profile, curation decisions, and the task catalog

**Audited:** 2026-09-14 · **Owner:** Gandi Abdi Kediye
**Applied by:** `scripts/curate_scheduled_tasks.py` (dry run by default, `--apply` to write)
**Facts drawn from:** `HERMES_CONTEXT_PACKS.md` (derived from Gandi's canonical governance sources). No fact in any task prompt was invented here — where the packs flag something as uncertain, the prompt carries the flag.

---

## 1. Who the tasks are for

Gandi Abdi Kediye (formerly Gandi Yusuf Mohamed). Minneapolis. Somali-born, U.S. Air Force veteran, Muslim. He dictates by voice and reads by scanning — he is partially colorblind, so every report leads with a headline, then one fact per line, `Label: value` for numbers, and no decorative emphasis.

He is not a founder with one company and spare time. He runs six wired entities plus a seventh business, and he is the **daily caregiver for his 97-year-old father, Yusuf Mohamed Hared**, and has been since 2020. That is the first constraint on his day, not a footnote.

**His week, as the tasks now assume it:**

| When | What it is |
|---|---|
| 6:00–9:00 AM CST | **Kenya Window** — the only slot for Afro Farm / Kenya calls (Kenya = UTC+3) |
| Friday ~8:00 AM CT | **Commander's Review** — the cross-entity rollup |
| 1st of the month | Rent posts |
| From the 5th | Late-fee window opens, per each lease's own terms — Gandi decides, never the agent |
| May 15 / Oct 15 | MN property tax, per parcel (GAK has 7, GIF has 3) |
| January | 1099-NEC per entity; books closed Jan 31, to the CPA (USFAAF, Edina) by Feb 15 |
| Throughout | Prayer times (seasonal, computed per date), Ramadan / Eid / Hajj, halal venues |

**The six wired businesses** (`hermes/businesses.py` → `BUSINESS_TO_ENTITY_SLUG`):

| Slug | Entity | Shape of the work |
|---|---|---|
| `gak` | GAK Properties LLC | 7 MN commercial properties, ~10 active tenants — the heaviest entity |
| `gif` | GIF Properties LLC | 3 MN properties, one vacant, one billboard lease |
| `afk` | Afro Farm Holding Ltd | 128-acre farm, Loitokitok Kenya, ~54 staff, KES, GLOBALG.A.P. prep |
| `gakc` | GAK Commodities Ltd | Kenya real estate — deliberately low activity |
| `nh` | Nour Hammam LLC | Women-only hammam, pre-formation, buildout at 720 E Lake |
| `per` | Personal & Family | Father's care, family, personal inbox |

**Comfort PCA is a seventh business and has no tasks — it cannot have any yet.** It is an active, Gandi-authorized business whose data is searchable (`search_business_knowledge`, Pinecone namespace `comfort_services`), and the `comfort_services` entity row exists in the database. But it has **no business slug** in `BUSINESS_TO_ENTITY_SLUG` and **no Maton key** in `MATON_BUSINESS_KEYS`, so a scheduled task cannot be scoped to it or reach its Gmail/Drive. Wiring it is a small code + env change; it is called out as a gap rather than faked.

---

## 2. What was there before

32 tasks. Only **6 were enabled — and all 6 were identical copies of the same "Daily attention report"**, unscoped, with no app restriction, firing daily at 07:00 and emailing `owner@hermes.local`, which is not a deliverable address. Every real business task was switched off. Two more tasks notified `haiderali333222@gmail.com`, a third-party address.

So in practice the scheduler was sending six copies of one report into a black hole, and doing nothing for any business.

Underneath that: a dozen duplicates, four test rows, a completed one-off backfill, and thirteen genuine business tasks whose prompts were one or two lines each — none of which carried the hard rules (entity separation, QuickBooks-only money, never invent, related-party tagging, never send), so an unattended run had nothing stopping it from blending entities or quoting a rent figure out of an old document.

### Why there were twelve copies — the duplicates were being generated

This was not sloppy clicking. `app.py`'s lifespan calls `notify.ensure_digest_task()` on **every process boot**. That function looks the task up **by exact name** (`notify._DIGEST_TASK_NAME = "Daily attention report"`) and creates a fresh one if the lookup misses. It is idempotent for one sequential boot — but not against concurrent cold starts, and Vercel has many. The original twelve rows were created in bursts roughly twenty seconds apart, which is what repeated boots racing each other looks like.

Two consequences:

1. **The task's name is load-bearing.** Renaming it — which this catalog originally did, to "Daily attention report (all entities)" — breaks the lookup and guarantees a brand-new duplicate on *every* cold start. Caught when a fresh copy appeared minutes after the first cleanup. The name is now pinned byte-identical to `_DIGEST_TASK_NAME`, with a comment in the script saying why.
2. **A backstop was added:** `hermes/db/migrations/0021_unique_digest_task.sql` creates a partial unique index on `(user_id) WHERE name = 'Daily attention report'`. The losing INSERT in a race now fails at the storage layer; `create_task` already catches that and returns `None`, and `ensure_digest_task`'s caller is already wrapped in try/except. Scoped to this one name so ordinary user-created tasks may still share names.

Verified after the fix: calling `ensure_digest_task()` returns the existing curated task and the count stays at 25.

### And why the email went nowhere

`owner@hermes.local` is not a misconfiguration of the tasks — it is `Config.owner_email()`'s fallback when the **`OWNER_EMAIL` environment variable is unset**. Setting `OWNER_EMAIL` in `.env` and in the Vercel project fixes delivery for the attention report in one move, without touching any task's `notify_to`. That is the correct lever; per-task `notify_to` is for routing a specific task somewhere else.

---

## 3. Decisions

### Delete — 18

| Task | Why |
|---|---|
| `Daily attention report` ×11 | Eleven duplicates, machine-generated on boot (see above). One copy kept (`8153a4b5`, the most recently run), under its load-bearing original name. |
| `Daily Todo Report` | Same job as the attention report, and notified a third-party address. |
| `Urgent email triage & draft replies` | Unscoped **and hourly** — an all-businesses inbox sweep is exactly the cross-entity blending the hard rules forbid. Replaced by per-business triage (GAK, GIF, personal). |
| `Check Recent Calendar Events` | Test row. |
| `Concurrency Test Ping` | Test row ("Log a simple heartbeat message"). |
| `Daily Report Sync` (afk) | "Run the task to sync the daily report and check for updates" — never ran, nothing actionable. Replaced by the AFK field-report verification. |
| `Daily morning briefing` (gif) | Unedited quick-start template. GIF is covered by its lead triage and deadline watch. |
| `Nour Hammam WhatsApp backfill` | One-off, completed July 2026. WATI outreach to tenants is barred anyway. |

### Keep — 14, every prompt rewritten

Kept because each one maps to a real, recurring obligation. Rewritten because each one-line prompt had to become a self-contained brief: unattended runs start with no conversation memory, so the roster facts, the guardrails, and the output format all have to live in the prompt.

Three of them also changed shape:

- **`Friday portfolio review` → `Friday Commander's Review`, moved 16:00 → Fri 07:30.** The canonical cadence is a Friday ~8:00 AM CT Commander's Review. A rollup landing at 4 PM arrives eight hours after the meeting it exists for.
- **`Daily farm briefing (AFK)` → `Kenya Window morning brief`, moved 06:30 → 05:45.** The Kenya Window opens at 6:00 AM CST. The brief has to be read *before* the calls, not during them.
- **`Leasing lead triage` moved 08:30 → 09:00**, off the same slot as GAK triage.

### Create — 11

| Business | New task | Why it was missing |
|---|---|---|
| `gak` | **A Caring Living renewal countdown** (daily 12:00) | The single most time-critical item in the portfolio. Two separate leases — 6733 Bloomington and 7601 5th Ave — **both end 09/30/2026**, which is 16 days after this audit. Nothing was watching them. Runs daily until both are resolved; reports "No change since yesterday" on quiet days. |
| `gak` | **Rent posting & late-fee window** (daily 07:15, date-gated to the 1st–8th) | Rent posts on the 1st, the late-fee window opens the 5th, and the arrears sweep only ran on Mondays — so a Tuesday-the-5th could pass unseen. Gated: outside the window it replies one line and calls no tools. |
| `gak` | **Lease, COI & renewal watch** (Tue 09:00) | GIF had a deadline watch; GAK — seven properties and ten tenants — had none. Also carries the two known lease template errors (Caremark and Khadijah bodies wrongly reference 1229) forward so they get fixed by amendment at renewal, and keeps Trusted Therapy's OCR-uncertain end date flagged. |
| `gif` | **2609 15th Ave S vacancy & leasing pipeline** (Thu 14:00) | The daily task triages inbound leads; nothing asked the strategic question. Every week vacant is lost income, so this one reports pipeline health and ends with one concrete thing to change. |
| `gif` | **Clear Channel billboard & signage income** (Wed 11:00) | A 2006 lease auto-renewing year-to-year since 03/2021, flagged as a renegotiation candidate, with a notice period nobody had checked. Also audits the standing accounting rule that signage income stays on its own line and never folds into base rent. Says one line and stops on a quiet week. |
| `afk` | **Daily field-report verification** (daily 16:30 **Africa/Nairobi**) | The farm reports at 9:00 / 12:00 / 16:00 EAT, verified by Abdirahman and Ahmed Hussein. Nothing confirmed the cadence held. Runs on Kenya time, at 16:30 EAT, right after the last report is due. |
| `afk` | **GLOBALG.A.P. compliance evidence** (Thu 15:00) | Certification prep is a standing workstream, not a project. Tracks open corrective actions and — the part that matters — control points with *no* evidence at all. |
| `gakc` | **GAK Commodities activity check** (Fri 10:00) | GAKC had **zero** tasks. Built to stay quiet: if there is no fresh activity it says exactly that and stops. Carries the two identity guards — GAKC is not Kediye Holdings, and Abdikadir Kadeye is not Abdirahman Abdiaziz. |
| `nh` | **NH formation & related-party lease status** (Tue 10:00) | The pre-launch tracker covered buildout and marketing. Nothing tracked whether the entity legally exists (MN filing, EIN, bank, insurance, operating agreement) or whether the GAK↔NH lease — Gandi on both sides — is actually signed and documented on both books. That is the compliance-critical item. |
| `per` | **Week ahead: father's care & schedule conflicts** (Sun 18:00) | The daily brief is same-day. Appointments, refills and home-care gaps need a week of lead time. Leads with father's care, then prayer-time and religious-calendar collisions for the coming 7 days. |
| all | **Month-close prep** (Wed 13:00, date-gated to month-end ±6 days) | The scheduler has no monthly frequency, so this runs weekly and gates on the date. Per-entity close checklist, plus the related-party memos (1st Attentive at three GAK sites, NH at 720) that the CPA needs documented. Never writes to QuickBooks. |

**Result: 32 tasks → 25**, zero duplicates, zero test rows, every one of the six wired businesses covered.

---

## 4. Patterns the prompts follow

Every prompt is built as `body + RULES + FORMAT` in the script, so the guardrails are identical across all 25 and can be changed in one place.

**`RULES`** — stay inside this business; money from live QuickBooks only, never from a document or memory, and say so rather than estimating if it is unreachable; never invent a tenant, date, amount or person (`no indexed source found` + `UNVERIFIED` instead); a company is an active tenant only if the pack lists it; tag 1st Attentive and Nour Hammam `RELATED PARTY`; send nothing, draft only where told; no credentials or account numbers in output.

**`FORMAT`** — headline first line with a count, one fact per line, `Label: value` with the currency named, short blocks, end with `Decisions needed` (or `Decisions needed: none`), and **say it in one line and stop if there is nothing to report**. That last rule is what keeps 25 tasks from becoming 25 pieces of noise.

**Two scheduler constraints worth knowing** (`hermes/scheduler.py`):

1. Frequencies are `daily` / `weekdays` / `weekly` / `hourly` — **there is no monthly**. Date-specific work (rent window, month close) is therefore a frequent task with a date gate as the first instruction in its prompt, so it costs nothing outside its window.
2. `days_of_week` is `0 = Mon … 6 = Sun`.

**Autonomy** stays `readonly` everywhere except the three triage tasks (GAK, GIF leads, personal), which are `draft` — they create Gmail drafts threaded to the original and never send. Nothing is set to `full`.

**Load:** the week is deliberately spread — Mon 3, Tue 4, Wed 3, Thu 3, Fri 3, Sun 1 — plus 5 daily tasks, of which 2 are date-gated and usually silent. Morning slots are staggered 05:45 → 09:00 so nothing lands on the same minute.

---

---

## 5. The other automation layer — and the boundary between them

Hermes is **not** the only automation running against these businesses. A separate layer on the Claude Artifact / Cowork surface maintains Google Sheets workbooks as the operational register for most of the portfolio, on a **Verifier / Updater** pattern:

| Business | Register | Automations |
|---|---|---|
| GAK Properties | 15-tab workbook | Weekly Verifier (Fridays, mails numbered findings from `gak-maintainer@agentmail.to`) + Updater driven by YES / CORRECT / OK replies |
| GIF Properties | own workbook | Weekly Verifier + Updater |
| Nour Hammam | 6-tab workbook (`05 REGISTER`, `06 PEOPLE`, `02 DECISIONS`, `03 GATES`, `08 MONEY`, `12 CHANGE LOG`) | Daily registrar; unsupported changes held as `NEEDS RULING` |
| Afro Farm | sheet fed by the **MCP AFK OS** system | Daily dashboard update — the sheet is a data sink, not hand-maintained |
| Bills & subscriptions | ~71 recurring bills across six entities, with a `DEADLINES` tab | Fortnightly sweep of five mailboxes + urgent alerts from `urgent-billing@agentmail.to` |

That layer's design principles — source-backed writes, verification separated from updating, change logs with before/after and evidence, deduplication, entity isolation, no guessing — are the same ones the Hermes prompts enforce. The risk was never a clash of philosophy; it was **two systems reporting on the same facts from different sources without knowing about each other**, which produces two arrears numbers and no way to tell which is right.

So 18 of the 25 tasks now carry a `REGISTERS` block that sets the boundary explicitly:

- **Never write to those workbooks, and never propose a cell edit.** The Updater owns every write; writing around it breaks the audit chain. (All 25 tasks are `readonly` or `draft`-to-Gmail, so this is belt and braces — but an unattended run should be told, not trusted to infer it.)
- **Read the relevant register before reporting, and name the register and tab you read.**
- **When the live figure disagrees with the register, report both with their sources and flag it.** Never silently pick one, never average them — the disagreement *is* the finding, and it is worth more than either number alone.
- **The register holds operational state; live QuickBooks remains the only source for a money figure.** This resolves what would otherwise be a contradiction between the two systems' rules.
- **Don't restate what the register's own Verifier already reports.** Add the live cross-check and the decision, not a second copy of the audit.

Those 18 tasks also get `google-sheets` in `allowed_apps` so they can actually read the workbooks. The seven that don't — personal brief, personal triage, week-ahead, GAKC, GLOBALG.A.P., and the two Gmail triage tasks — have no corresponding register.

---

## 6. Open items

1. **Set `OWNER_EMAIL`.** This is the one-line fix for the undeliverable `owner@hermes.local` (see §2). Per-task `notify_to` is left unset by default and is available via `--notify-to`.
2. **Sheet IDs are described, not linked.** The `REGISTERS` block identifies each workbook by name, owner and tab structure, so the agent has to find it via Drive/Sheets search. Pasting the actual spreadsheet IDs into that block would make the lookup deterministic and cheaper.
3. **Comfort PCA cannot be scheduled yet** — needs a slug in `BUSINESS_TO_ENTITY_SLUG` and a Maton key. See §1. It is one of the six entities in the billing register and carries real spend, so it is a live gap, not a theoretical one.
4. **The A Caring Living countdown is designed to be retired.** When both leases resolve it says so on its first line. Delete it then.
5. **Notice periods are unverified across the board.** Several prompts instruct the agent to search the indexed lease for the actual notice period and mark it `UNVERIFIED` when it cannot find one. Expect that to be the common answer until the lease PDFs are fully ingested and extractable.
