"""Curate the Scheduled Tasks list: delete test/duplicate tasks, harden the
prompts of the ones worth keeping, and add the per-business tasks that are
missing.

Dry run (prints the plan, touches nothing):
    python scripts/curate_scheduled_tasks.py

Apply it:
    python scripts/curate_scheduled_tasks.py --apply

Optional flags:
    --notify-to a@b.com   set notify_to on every kept/created task
    --enable-all          enable every task in the catalog, not just the
                          ones marked for the first rollout

Every decision below is data, not code — see SCHEDULED_TASKS_CATALOG.md for the
reasoning. Deletions are matched on (task_id, name): if the name on the row no
longer matches what this file expects, the delete is skipped and reported, so a
stale ID can never take out the wrong task.

DELETING A TASK DESTROYS ITS RUN HISTORY. scheduled_task_runs.task_id is
`REFERENCES scheduled_tasks (task_id) ON DELETE CASCADE`, so every past run goes
with the task — `reply` (the generated report text) and `tool_calls` (the trace)
included, with no undo. `--apply` therefore writes a full JSON export via
scripts/backup_scheduled_tasks.py before the first delete. Do not remove that.

Scheduler constraints this file works within (hermes/scheduler.py):
  frequency ∈ {daily, weekdays, weekly, hourly} — there is NO monthly. Anything
  date-specific (rent window, month close) is a frequent task with a date gate
  written into its prompt, so it stays silent outside its window.
  days_of_week is 0=Mon … 6=Sun.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from hermes.businesses import BUSINESS_TO_ENTITY_SLUG  # noqa: E402
from hermes.db import scheduled_tasks as repo  # noqa: E402
from hermes.scheduler import compute_next_run  # noqa: E402

CT = "America/Chicago"
EAT = "Africa/Nairobi"

# --------------------------------------------------------------------------
# Shared prompt blocks. Every run starts with no memory of any conversation, so
# each prompt has to carry its own rules. These two blocks are appended to every
# task body below.
# --------------------------------------------------------------------------

RULES = """
STANDING RULES (non-negotiable)
- Stay inside this task's business. Never read, quote, or blend another entity's data into this report.
- Money comes from live QuickBooks only. Never state a rent, arrears, deposit or balance figure from a document, from memory, or from an old file. If QuickBooks is unreachable, say so plainly — do not estimate.
- Never invent tenants, dates, amounts, vendors or people. If a fact is not in a live tool result or an indexed document, write "no indexed source found" and mark the point UNVERIFIED.
- Treat a company as an ACTIVE tenant only if the business context pack lists it. Old documents name former tenants; when a document conflicts with the pack, say so explicitly instead of repeating the document.
- Tag 1st Attentive Services LLC (Samsam Mohamed, the owner's wife) and Nour Hammam LLC as RELATED PARTY anywhere they appear in a financial or AR output.
- Send nothing. Create drafts only where this task explicitly says to draft. Tenant outreach is email-only and never by WhatsApp/WATI or SMS.
- Never output credentials, API keys, bank account numbers, SSNs or EINs.
""".strip()

FORMAT = """
OUTPUT FORMAT (Gandi is partially colorblind and reads scannable text; this is emailed to him)
- First line is a headline: the count plus the single most important thing, e.g. "3 decisions needed — A Caring Living renewal is 16 days out".
- Then one fact per line. Use "Label: value" for anything numeric, with the currency named (USD vs KES).
- Short blocks, blank line between them. No decorative emphasis. Keep any table to 4 columns or fewer.
- End with a "Decisions needed" list, or the single line "Decisions needed: none".
- If there is genuinely nothing to report, say that in one line and stop. Do not pad.
""".strip()


REGISTERS = """
OPERATIONAL REGISTERS — READ THEM, NEVER WRITE THEM
A separate automation layer maintains Google Sheets workbooks as the operational register for several of these businesses, on a Verifier/Updater pattern: a Verifier audits the workbook without changing anything, and an Updater applies only source-backed changes, logging each write with its previous value, new value and supporting evidence.

- GAK Properties — a 15-tab workbook. Its Verifier runs Fridays and mails numbered findings (proposals, conflicts, notices) from gak-maintainer@agentmail.to; replies of YES / CORRECT / OK drive the Updater's writes.
- GIF Properties — its own workbook, with a weekly Verifier and Updater.
- Nour Hammam — a 6-tab operations workbook (05 REGISTER, 06 PEOPLE, 02 DECISIONS, 03 GATES, 08 MONEY, 12 CHANGE LOG), updated daily; unsupported changes are held as NEEDS RULING rather than guessed.
- Afro Farm — dashboard data is supplied by the MCP AFK OS system. Its connected sheet is a data sink, NOT a hand-maintained document.
- Bills & subscriptions — a register of roughly 71 recurring bills across six entities, refreshed fortnightly from five mailboxes, with its own DEADLINES tab. Urgent items go out separately from urgent-billing@agentmail.to.

How to treat them:
- NEVER write to these workbooks and never propose a specific cell edit. The Updater owns every write; writing around it breaks the audit chain.
- Read the register relevant to this task before reporting, and name which register and which tab you read.
- When your live figure disagrees with the register, report BOTH with their sources and flag the disagreement plainly. Do not silently pick one, and never average them — the disagreement IS the finding, and it is more useful to Gandi than either number alone.
- The register holds operational state. Live QuickBooks remains the only source for a money figure. If the register carries a money figure that QuickBooks does not support, say so.
- Do not repeat what the register's own Verifier already reports. Add the live cross-check and the decision, not a second copy of the audit.
""".strip()


# Tasks whose subject matter is also tracked in a Sheets register maintained by
# the separate Verifier/Updater automation layer. These get the REGISTERS block
# so they cross-check against it instead of quietly competing with it.
REGISTER_TASKS = {
    "Daily attention report",
    "Kenya Window morning brief (AFK)",
    "Rent arrears sweep (GAK)",
    "Nour Hammam pre-launch tracker",
    "Tax & compliance deadline monitor (all entities)",
    "AFK payroll cash check",
    "AFK Friday Commander Brief",
    "30-day cash & obligations look-ahead (all entities)",
    "Friday Commander's Review (all entities)",
    "Lease, COI & renewal watch (GIF)",
    "A Caring Living renewal countdown (GAK)",
    "Rent posting & late-fee window (GAK)",
    "Lease, COI & renewal watch (GAK)",
    "2609 15th Ave S vacancy & leasing pipeline (GIF)",
    "Clear Channel billboard & signage income (GIF)",
    "Daily field-report verification (AFK)",
    "NH formation & related-party lease status",
    "Month-close prep (all entities)",
}


def body(text: str) -> str:
    """The task-specific half of a prompt. Guardrails are appended at write time."""
    return text.strip()


def compose(spec: dict[str, Any]) -> str:
    """Task body + the shared guardrails, assembled in reading order."""
    blocks = [spec["prompt"], RULES]
    if spec["name"] in REGISTER_TASKS:
        blocks.append(REGISTERS)
    blocks.append(FORMAT)
    return "\n\n".join(blocks) + "\n"


def apps_for(spec: dict[str, Any]) -> list[str]:
    """A register task needs Sheets access to read the workbook it cross-checks."""
    apps = list(spec["apps"])
    if spec["name"] in REGISTER_TASKS and "google-sheets" not in apps:
        apps.append("google-sheets")
    return apps


def sched(frequency: str, times: list[str], days: list[int] | None = None) -> dict[str, Any]:
    return {"frequency": frequency, "times": times, "days_of_week": days or []}


# --------------------------------------------------------------------------
# 1. DELETE — test rows, duplicates, and tasks superseded by a better one.
# --------------------------------------------------------------------------

DELETE: list[tuple[str, str, str]] = [
    # (task_id, expected name, why)
    ("03b380ae-6509-4f7b-abfa-5c3d1289e9d9", "Daily attention report", "duplicate — 12 copies of one task; keeping 8153a4b5 only"),
    ("8b599753-d3ad-491b-9861-849f6c3204d2", "Daily attention report", "duplicate"),
    ("e1e52d1e-5e52-4b77-a94d-b5184326f285", "Daily attention report", "duplicate"),
    ("767c57ac-25c2-4596-b985-fa5e2449243f", "Daily attention report", "duplicate"),
    ("f2c142fe-067a-4ba2-aed4-5cd8a53545e7", "Daily attention report", "duplicate"),
    ("e2d96ebe-f6a6-40a9-9881-253f3afc983d", "Daily attention report", "duplicate"),
    ("a1a227ce-2d02-4576-bc1c-572eed74a61b", "Daily attention report", "duplicate; also notified a third-party address"),
    ("9ab7479e-c397-4ac1-876e-da098490a2ef", "Daily attention report", "duplicate"),
    ("2942ad99-510c-48e5-8265-946f827083dc", "Daily attention report", "duplicate"),
    ("6398ebe4-7232-431b-bd74-dd908015e8ad", "Daily attention report", "duplicate"),
    ("6d82ca51-0299-4550-86be-1b37e33f216a", "Daily attention report", "duplicate"),
    ("4646fc98-900b-40ef-87c3-d524947ac308", "Daily Todo Report", "superseded by the attention report; notified a third-party address"),
    ("a1ce59e3-81e5-47ce-a0b3-c4931d13bcb3", "Urgent email triage & draft replies", "unscoped hourly inbox triage — breaks entity separation; replaced by per-business triage"),
    ("e92aa150-f972-4031-a96e-0d9fa6cf2376", "Check Recent Calendar Events", "test row"),
    ("0dbf2b65-90f5-4269-b6cb-cd9f00e6fd78", "Daily morning briefing", "unedited template; GIF is covered by its lead triage + deadline watch"),
    ("895de640-01e8-4daf-ba6f-41fb6e3d9b51", "Concurrency Test Ping", "test row"),
    ("b96a3d73-c8b9-40a6-9578-c098d8ed5e39", "Daily Report Sync", "vague one-liner, never ran; superseded by the AFK field-report verification"),
    ("9d797bbe-5104-400f-93ea-7355acbd3c9a", "Nour Hammam WhatsApp backfill (Jun 17 -> Jul 17)", "one-off backfill, completed Jul 2026"),
]

# --------------------------------------------------------------------------
# 2. KEEP — worth having, but every prompt is rewritten to carry the standing
#    rules, the real roster facts, and the scannable output format.
# --------------------------------------------------------------------------

KEEP: list[dict[str, Any]] = [
    {
        "task_id": "8153a4b5-9f59-44e4-b5d4-ff9823123d67",
        # LOAD-BEARING NAME — must stay byte-identical to notify._DIGEST_TASK_NAME.
        # app.py calls notify.ensure_digest_task() on every boot, which finds this
        # task *by name* and creates a fresh one if the lookup misses. Rename it and
        # every cold start mints another duplicate. Migration 0021 adds a partial
        # unique index as a backstop, but the name is the first line of defence.
        "name": "Daily attention report",
        "business": None,
        "apps": ["google-mail", "google-calendar", "quickbooks"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["07:00"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Produce today's attention report for Gandi: everything across the portfolio that needs his eyes or a decision right now.

Work entity by entity (GAK Properties, GIF Properties, Afro Farm, GAK Commodities, Nour Hammam, Personal) and surface only what is genuinely actionable today:
- items waiting on his approval or a go/no-go decision
- anything blocked on him
- overdue or failing items, including any lease or compliance date now inside 30 days
- anything a tenant, staff member or vendor asked for and has not received

Label every section with its entity. Never blend figures across entities — a portfolio total is only ever a labelled sum of per-entity lines, never a single mixed number.
"""),
    },
    {
        "task_id": "ea4c706c-b23d-47fe-b931-ebf4dffe503b",
        "name": "Kenya Window morning brief (AFK)",
        "business": "afk",
        "apps": ["google-mail", "google-calendar", "google-drive", "google-forms", "web"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["05:45"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Afro Farm morning brief, delivered before the Kenya Window (6:00–9:00 AM CST) so Gandi can walk into his Kenya calls prepared. Kenya is EAT (UTC+3); Minneapolis is CST/CDT.

Cover, from the AFK lane (admin@afrofarmke.com) Gmail, Calendar, Drive and the operational Google Forms:
- what came in overnight Kenya time that needs an answer today
- staffing and security flags (the farm runs ~54 staff: 9 security, 6 supervisors, 28 monthly, the rest weekly/seasonal)
- anything Abdirahman Abdiaziz (Manager & Finance, the primary contact) has escalated
- irrigation, nursery, crop-protection or inventory issues raised by the field team
- weather and any market/price context you can cite from a live source — cite the source; never guess a price

State every time as both EAT and CST. Label every figure KES or USD.

STALE-RECORD GUARD: Mohamed Hassan Hussein (Gandi's half-brother) has not been AFK Logistics & Security since 2026-05-26. Do not list him as staff or route anything to him. If a document names him as active, flag the document.
"""),
    },
    {
        "task_id": "de08e3c1-4f2d-4317-bce6-8ac363aaf047",
        "name": "GAK weekday email triage (draft)",
        "business": "gak",
        "apps": ["google-mail"],
        "autonomy": "draft",
        "schedule": sched("weekdays", ["08:30"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Triage the GAK Properties email lane for the last 24 hours.

For each message that genuinely needs Gandi (time-sensitive, from a real person, asking something of him), report: sender, subject, one line on why it is urgent, and the property it concerns.

For the urgent ones, create a Gmail DRAFT reply in Gandi's voice, threaded to that specific message. Never send. Gandi reviews and sends from Gmail himself.

Gandi's active GAK tenants: Lake Street Academy, Safia 2 Restaurant, Trusted Therapy Center, Caremark Home Health Care, Khadijah Model Center (all at 720 E Lake); Helpful Hands (8224 18th Ave S); A Caring Living (6733 Bloomington Ave S and 7601 5th Ave S, two separate leases); 1st Attentive Services (1108 Pearson, 1717 Thomas, 8101 Vincent — RELATED PARTY).

A sender who is not on that roster is not a current tenant — say so rather than assuming. Never quote a rent or balance figure in a draft; if a tenant asks about their balance, draft a reply that says Gandi will confirm the figure, and flag it for him.

Finish with a ranked list of what he should look at first.
"""),
    },
    {
        "task_id": "94d4c634-7b81-4e31-87bd-3c7f5bb26364",
        "name": "Rent arrears sweep (GAK)",
        "business": "gak",
        "apps": ["quickbooks", "google-mail"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["09:00"], [0]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly arrears sweep for GAK Properties.

1. Pull AR aging from the GAK QuickBooks company file. Confirm you are on the GAK file before reporting anything — if the connector only reaches another entity's books, say so and stop rather than reporting another entity's numbers as GAK's.
2. For each overdue tenant: amount, days late, and which property the ledger belongs to. A Caring Living has TWO separate leases and ledgers (6733 Bloomington and 7601 5th Ave) — never merge them. 1st Attentive has THREE (Pearson, Thomas, Vincent).
3. Cross-check the last 30 days of GAK email for payment promises, disputes or hardship notices, and note the last contact date per tenant.
4. Rank by amount × age.
5. Tag 1st Attentive Services rows RELATED PARTY.

Report as: tenant | property | amount | days late | last contact | suggested next step.

Do not draft or send anything. Do not assess or calculate a late fee — the late-fee window opens on the 5th per each lease's own terms, and only Gandi decides whether to enforce it.
"""),
    },
    {
        "task_id": "61cbd54d-6b98-4c38-8cdb-90855a6d8f7d",
        "name": "Leasing lead triage — 2609 15th Ave S (GIF)",
        "business": "gif",
        "apps": ["google-mail", "google-calendar"],
        "autonomy": "draft",
        "schedule": sched("weekdays", ["09:00"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
2609 15th Ave S, Minneapolis is GIF Properties' only vacant building. Every week without a tenant is lost income, so leasing it is a standing priority.

Scan the GIF email lane for new leasing inquiries, broker messages, listing-portal notifications and showing requests about 2609.

Score each lead on urgency, fit and readiness (is it a real operator, do they have a use that fits the space, can they move).

For strong leads, create a Gmail DRAFT follow-up in Gandi's voice with a concrete next step and two proposed showing windows drawn from his actual GIF calendar availability. Never send. Do not quote an asking rent — leave it for Gandi to set.

Finish with the pipeline: who to call today, who is waiting on us, and anything that went cold.
"""),
    },
    {
        "task_id": "2a8e7c83-b348-46b8-a514-f8f9d2c884f3",
        "name": "Nour Hammam pre-launch tracker",
        "business": "nh",
        "apps": ["google-mail", "google-calendar", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["10:00"], [2]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly build status for Nour Hammam — a women-only hammam spa, pre-formation, being built out in the west wing of 720 E Lake St (a GAK Properties building).

From the NH lane's Gmail, Calendar and Drive, report on:
- buildout and permit progress: architectural review, city code (occupancy, plumbing, ventilation for steam and wet rooms), ADA, fire
- vendor and contractor items — who owes us what, and what we owe them
- licensing: MN cosmetology / establishment licences — verify what is actually required, never assume
- hiring pipeline (staff must be vetted for a women-only setting)
- waitlist and marketing momentum

NH service details are confidential by default — keep specifics out of anything that could be forwarded.

End with the top 3 things to push this week.
"""),
    },
    {
        "task_id": "7a6632cf-52a7-4b42-9df3-cc4516c7ecc8",
        "name": "Tax & compliance deadline monitor (all entities)",
        "business": None,
        "apps": ["google-mail", "google-calendar", "quickbooks"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["08:30"], [0]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly compliance sweep across every entity. Report anything due in the next 90 days, entity by entity, with its date.

The fixed calendar:
- MN real-estate tax, per parcel: May 15 (1st half) and Oct 15 (2nd half) — GAK has 7 parcels, GIF has 3
- 1099-NEC: January, per entity, for US vendors paid over $600 — flag missing W-9s from now, not in January
- Year-end: books closed by Jan 31, delivered to the CPA (USFAAF, Edina MN) by Feb 15
- Quarterly estimates
- LLC annual renewals per entity
- Insurance: one COI per property, plus each tenant's COI renewed annually — flag any lapse
- Auto-renewals, notably the Clear Channel billboard at 1229 E Lake (year-to-year since 03/2021)

For each item give: entity, item, date, what is actually needed, and who has to do it. Say explicitly when an item's status could not be verified from a live source.
"""),
    },
    {
        "task_id": "2cf6f1ea-2d37-435d-ab36-ccffa03fbd2f",
        "name": "Personal & family daily brief",
        "business": "per",
        "apps": ["google-mail", "google-calendar"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["06:30"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Gandi's personal morning brief. This lane is strictly personal — never pull in or reference any business's books.

From the personal Gmail and Calendar:
- Father's care first. Yusuf Mohamed Hared is 97 and Gandi has been his daily caregiver since 2020. Surface today's appointments, transport needs, prescription refills, home-care visits and anything from a clinic or pharmacy. This outranks everything else in this brief.
- Family coordination, including schedule overlaps with Samsam (his wife, who runs her own business) and Fardowsa (his sister).
- Today's personal commitments and any conflict with prayer times for the Twin Cities — compute them for today's actual date, do not assume fixed clock times.
- Flag any religious-calendar conflict (Ramadan, Eid al-Fitr, Eid al-Adha, Hajj) coming up.
- Note anything scheduled over the Kenya Window (6:00–9:00 AM CST), which is reserved for business.

Read-only. Flag conflicts; never move or create a calendar entry.
"""),
    },
    {
        "task_id": "3a777a6b-9bd1-4bbc-b352-371dd59937ae",
        "name": "AFK payroll cash check",
        "business": "afk",
        "apps": ["quickbooks", "google-mail"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["09:00"], [3]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Payroll readiness check for Afro Farm, which pays roughly 54 staff in KES (9 security, 6 supervisors, 28 monthly, the rest weekly/seasonal) through Middle East Bank Kenya Ltd.

From QuickBooks and the AFK email lane:
- cash available between now and the next payday
- committed outflows before then
- receivables worth chasing to cover the gap, ranked by size and collectability
- any shortfall risk, stated as a number and a date

Label every figure KES or USD — never mix them in one line, and give the rate and its date if you convert.

Read-only: surface the numbers. Do not move money, do not send anything, do not contact a debtor.
"""),
    },
    {
        "task_id": "7db05c27-4486-4fd3-8e80-06032004e4e6",
        "name": "AFK Friday Commander Brief",
        "business": "afk",
        "apps": ["google-mail", "google-calendar", "google-drive", "google-forms", "quickbooks"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["06:00"], [4]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
The Friday owner rollup for Afro Farm, delivered before the Kenya Window (6:00–9:00 AM CST) so Gandi can raise anything on his Friday Kenya calls.

From the AFK lane's Gmail, Calendar, Drive, the operational Google Forms and QuickBooks:
- production status for the week
- sales against projection
- cash position and payroll runway
- staffing and attendance flags; note the open vacancies (General Manager, Agronomist, Salesperson, Marketing) if there is movement
- GLOBALG.A.P. (IFA) compliance gaps and open corrective actions
- infrastructure issues: the 14 field tablets, Starlink (office Gen-2, residence Gen-3 — the fields are NOT covered), the ~5 kW solar
- the farm web app (afrofarmholding.com) and public site (afrofarmke.com)

Every figure labelled KES or USD. Every time stated as both EAT and CST.

End with a short "Decisions needed" list — that is the part Gandi acts on.
"""),
    },
    {
        "task_id": "0f7e01e8-d92e-40fb-922f-31232c54845d",
        "name": "30-day cash & obligations look-ahead (all entities)",
        "business": None,
        "apps": ["quickbooks", "google-calendar", "google-mail"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["07:30"], [1]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Look 30 days ahead across the portfolio, entity by entity. Never produce a blended number — a portfolio line is only ever a labelled sum of per-entity figures.

For each entity (GAK Properties, GIF Properties, Afro Farm, GAK Commodities, Nour Hammam):
- cash on hand today, from that entity's own QuickBooks file
- rent and receivables expected in the next 30 days (rent posts on the 1st)
- committed outflows: debt service, the 720 E Lake contract-for-deed payment, property tax if a May 15 or Oct 15 date falls in the window, insurance, payroll (AFK in KES), known vendor and contractor commitments
- the resulting position, and any date where it goes tight

If an entity's QuickBooks file is not reachable, say so for that entity and leave its lines UNVERIFIED. Never fill a gap with an estimate.

End with: which entity needs attention first, and why.
"""),
    },
    {
        "task_id": "d90c960b-358b-45bf-af75-3bb9944ece80",
        "name": "Friday Commander's Review (all entities)",
        "business": None,
        "apps": ["google-mail", "google-calendar", "quickbooks"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["07:30"], [4]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
The cross-entity Friday rollup, delivered ahead of Gandi's 8:00 AM CT Commander's Review.

One labelled section per entity — GAK Properties, GIF Properties, Afro Farm, GAK Commodities, Nour Hammam, Personal — each with:
- the cash and arrears headline from that entity's own books
- what moved this week
- open items from email still waiting on someone
- anything on next week's calendar that needs preparation
- any lease, permit, insurance or tax date now inside 90 days

Never blend entities into a single figure. Label any related-party item (1st Attentive Services at three GAK properties; Nour Hammam at 720 E Lake).

Close with "Decisions needed" — the three things Gandi should settle before Monday, each with the decision stated as a question he can answer yes or no.
"""),
    },
    {
        "task_id": "572135e9-0666-42ee-8fa6-f7f8167b5dd5",
        "name": "Personal inbox triage (draft)",
        "business": "per",
        "apps": ["google-mail"],
        "autonomy": "draft",
        "schedule": sched("weekdays", ["08:00"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Triage Gandi's personal inbox from the last 24 hours. Personal lane only — never touch or reference a business's books.

Group by urgency. Put anything about his father's care (appointments, pharmacy, clinic, home care, insurance) at the top regardless of how it reads.

For anything that clearly needs a reply, create a Gmail DRAFT in Gandi's voice, threaded to the original. Never send.

Report what you drafted and what is still waiting on his decision.
"""),
    },
    {
        "task_id": "96f34a64-f624-4fd2-9359-00d262b9d1fd",
        "name": "Lease, COI & renewal watch (GIF)",
        "business": "gif",
        "apps": ["google-calendar", "google-mail", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["08:00"], [0]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly deadline watch for GIF Properties: 1229 E Lake St, 1230 Logan Ave N, and 2609 15th Ave S (vacant).

Active GIF tenants: Adult Daycare LLC (Upper, 1229) — route all formal or legal communication to Sudi Egal, the signer and owner, not to John, the day-to-day contact; Starlight Daycare LLC (Main, 1229); Rattil Center LLC (Lower, 1229); Clear Channel Outdoor (billboard at 1229); Love & Care Childcare LLC (1230 Logan).

Report anything due in the next 90 days:
- lease expirations and their notice deadlines — the 2027 cluster is Adult Daycare 12/31/2027 and Rattil 12/31/2027; renewal work starts Q2 2027
- the Clear Channel billboard, auto-renewing year-to-year since 03/2021 — check the notice period before any move
- property COIs (one per property) and each tenant's annual COI — flag any lapse
- MN property tax: May 15 and Oct 15, per parcel

For each lease, search the indexed documents for the actual notice period and escalation clause rather than assuming a standard term, and say when you could not find one.

Comfort Services LLC is NOT an active 1229 tenant — that lease is terminated-final and occupancy ended Dec 2025. If any record calls it current, flag the record.
"""),
    },
]

# --------------------------------------------------------------------------
# 3. NEW — the gaps. GAK's 7 properties had two tasks, GAK Commodities had none.
# --------------------------------------------------------------------------

NEW: list[dict[str, Any]] = [
    {
        "name": "A Caring Living renewal countdown (GAK)",
        "business": "gak",
        "apps": ["google-mail", "google-calendar", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["12:00"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
A Caring Living LLC holds TWO separate GAK leases — 6733 Bloomington Ave S, Richfield and 7601 5th Ave S, Richfield — both running 10/01/2024 to 09/30/2026, both 24-month terms, both with the same contact (Amal, acaringliving2@gmail.com, 763-273-6689). Two leases, two ledgers, two renewal decisions.

This is the portfolio's most time-critical open item. Run this every day until both are resolved.

Each run, report:
- days remaining until 09/30/2026, stated as a number on the first line
- the notice deadline for each lease — search the indexed lease documents for the actual notice period and holdover clause; if you cannot find it, say so and mark it UNVERIFIED rather than assuming 30 or 60 days
- any email from or to A Caring Living in the last 7 days about renewal, rent, or vacating
- what changed since yesterday — if nothing changed, say "No change since yesterday" and give only the day count and the next deadline

End every run with the open decision stated plainly: renew, renegotiate, or let it expire — separately for each of the two buildings.

Do not draft or send anything to the tenant. Do not quote a rent figure.

When both leases are resolved, say so clearly on the first line so Gandi knows to retire this task.
"""),
    },
    {
        "name": "Rent posting & late-fee window (GAK)",
        "business": "gak",
        "apps": ["quickbooks", "google-mail"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["07:15"]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
DATE GATE — check this first: if today's date is not between the 1st and the 8th of the month, reply with the single line "Not in the rent-collection window." and stop. Do not call any tool.

Inside the window, track rent collection for GAK Properties. Rent posts on the 1st. The late-fee window opens on the 5th, per each lease's own terms.

From the GAK QuickBooks file (confirm you are on the GAK file before reporting a figure):
- who has paid this month and who has not, per property and per ledger — A Caring Living has two ledgers, 1st Attentive has three
- for anyone unpaid on or after the 5th: how many days past, and what that lease's own late-fee terms say (search the indexed lease; if you cannot find the clause, mark it UNVERIFIED)
- any payment promise or hardship note in the GAK email lane this month

Tag 1st Attentive Services rows RELATED PARTY.

Never assess, calculate or apply a late fee, and never contact a tenant. Present the position and let Gandi decide whether to enforce.
"""),
    },
    {
        "name": "Lease, COI & renewal watch (GAK)",
        "business": "gak",
        "apps": ["google-calendar", "google-mail", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["09:00"], [1]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly deadline watch across GAK Properties' 7 buildings: 720 E Lake St (multi-tenant), 8224 18th Ave S, 6733 Bloomington Ave S, 7601 5th Ave S, 1108 Pearson Pkwy, 1717 Thomas Ave N, 8101 Vincent Ave N.

Report anything due in the next 120 days:
- lease expirations and notice deadlines. The 2027 cluster: Caremark Home Health Care 12/31/2027 and Khadijah Model Center 12/31/2027 (both dates implied, not confirmed on a signed copy — verify), Helpful Hands 10/01/2027. Renewal work starts Q2 2027.
- Trusted Therapy Center's end date is OCR-uncertain in the indexed lease — flag it as needing verification against the signed original until that is resolved.
- property COIs (one per building) and each tenant's annual COI — flag any lapse
- MN property tax per parcel: May 15 and Oct 15
- the 720 E Lake contract for deed (403 Union St LLC, Dec 2020) — any payment or term date in the window

TEMPLATE ERRORS to fix at renewal, not before: the Caremark lease body wrongly references "1229 E Lake" and the Khadijah lease body wrongly references "1229 basement". Both are template errors on 720 E Lake leases. Carry them forward in every run as open items so they get corrected by amendment when those leases are renewed.

Long-tail leases (1st Attentive 2030/2031/2032, Safia 2 2032, Lake Street Academy 2032, Trusted Therapy 2033) need one line only, and only when something changes.

A Caring Living's two 09/30/2026 leases have their own daily task — mention them in one line here and point to it rather than duplicating the detail.
"""),
    },
    {
        "name": "2609 15th Ave S vacancy & leasing pipeline (GIF)",
        "business": "gif",
        "apps": ["google-mail", "google-calendar", "google-drive", "web"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["14:00"], [3]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly read on the 2609 15th Ave S vacancy — GIF Properties' only empty building, and a standing leasing priority.

This is the strategy view; the daily lead triage handles individual inquiries.

Report:
- how long the building has been vacant, and the cost of that vacancy so far using only figures you can source from QuickBooks — if you cannot source it, say so rather than estimating
- the pipeline: every live lead, its stage, and how long it has sat at that stage
- leads that went cold this week and why
- how the listing is being marketed today (portals, broker, signage) and what is not working
- comparable space nearby, from cited live sources only — cite each one; never guess an asking rent

End with one concrete recommendation for what to change this week: price, presentation, channel, or target tenant type.
"""),
    },
    {
        "name": "Clear Channel billboard & signage income (GIF)",
        "business": "gif",
        "apps": ["google-mail", "google-drive", "quickbooks"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["11:00"], [2]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
The Clear Channel Outdoor billboard at 1229 E Lake St is a renegotiation candidate. The lease took effect 03/01/2006 on a 15-year term and has been auto-renewing year to year since 03/2021. Contact: Michael Wall, michaelwall@clearchannel.com, office 612-605-5123, mobile 401-529-7751. Billing address: 3225 Spring St NE, Minneapolis 55413.

If nothing has changed since last week, say so in one line with the next renewal anniversary and stop. Do not pad a quiet week.

When there is something to report, cover:
- the notice period required to renegotiate or terminate, taken from the indexed lease — quote the clause; if you cannot find it, mark it UNVERIFIED and say the signed original needs to be checked
- days remaining until the next 03/01 auto-renewal
- any Clear Channel correspondence in the GIF lane
- ACCOUNTING CHECK: signage income at 1229 must sit on its own income line in the GIF QuickBooks file and must never be mixed into base rent. Verify it is still booked that way and flag it immediately if it is not.

Read-only. Do not contact Clear Channel.
"""),
    },
    {
        "name": "Daily field-report verification (AFK)",
        "business": "afk",
        "apps": ["google-mail", "google-forms", "google-sheets", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("daily", ["16:30"]),
        "timezone": EAT,
        "rollout": True,
        "prompt": body("""
Afro Farm reports three times a day — 9:00 AM, 12:00 PM and 4:00 PM EAT — verified by Abdirahman Abdiaziz (Manager & Finance) and Ahmed Hussein (Admin). This task runs at 4:30 PM EAT to confirm the day's cadence actually held.

Check the AFK lane's Gmail, the operational Google Forms and Drive, and report:
- which of the three reports landed today and which did not — name the missing ones explicitly
- whether each was verified, and by whom
- any contradiction between the three reports
- anything reported that needs Gandi's decision rather than Abdirahman's
- field-connectivity context: the 14 Samsung Galaxy Tab A9+ tablets and Starlink coverage (office Gen-2 and residence Gen-3 only — the fields are NOT covered), since a missing report is often a connectivity problem rather than a compliance one

If all three landed and were verified, say so in one line with any decision items, and stop.

State times as both EAT and CST. Read-only — do not chase anyone; report the gap and let Gandi decide.
"""),
    },
    {
        "name": "GLOBALG.A.P. compliance evidence (AFK)",
        "business": "afk",
        "apps": ["google-drive", "google-mail", "google-forms", "google-docs"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["15:00"], [3]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Afro Farm is preparing for GLOBALG.A.P. (IFA) certification. Compliance evidence and corrective actions are a standing workstream, not a one-off project.

From the AFK lane's Drive, Gmail, Docs and the operational Google Forms, report weekly:
- open corrective actions: what, who owns it, how long it has been open
- evidence gathered this week, and which control points it covers
- control points with no evidence at all yet — these are the real risk
- anything from an auditor, certifier or consultant awaiting a response
- records the certification needs that the farm's current process does not produce (spray records, traceability, worker welfare, water testing) — name the gap; do not assume a record exists because it should

Never claim a control point is satisfied unless you can name the document that satisfies it. Where evidence is missing, write "no indexed source found".

End with the single control point most likely to fail an audit today.
"""),
    },
    {
        "name": "GAK Commodities activity check",
        "business": "gakc",
        "apps": ["google-mail", "google-drive", "google-calendar"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["10:00"], [4]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Weekly check on GAK Commodities Ltd — Gandi's Kenya real-estate entity. Activity here is low by design.

If there is no fresh GAKC email, document or calendar item this week, reply with one line saying exactly that and stop. Do not pad, do not repeat last week, do not go looking for something to say.

When there IS activity, report it plainly: what happened, what it obliges GAKC to do, and any date it creates.

Two things never to confuse:
- GAK Commodities Ltd is NOT Kediye Holdings Ltd. Kediye Holdings is a planned Kenya holding company managed by Abdikadir Kadeye. It holds no ownership over AFK or GAKC today; when formed it bills Kenya operating entities cost-plus for shared services only.
- Abdikadir Kadeye (Kediye Holdings) is a DIFFERENT person from Abdirahman Abdiaziz (Afro Farm). "Kadeye" is not "Kediye".

GAKC's books are fully separate from Afro Farm's and from every US entity. Label any figure KES or USD.
"""),
    },
    {
        "name": "NH formation & related-party lease status",
        "business": "nh",
        "apps": ["google-mail", "google-drive", "google-calendar"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["10:00"], [1]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Nour Hammam LLC is pre-formation. This task tracks the two things that have to be right before anything else: the entity existing, and its lease being defensible.

FORMATION CHECKLIST — report the status of each, and say "no indexed source found" where you cannot verify one:
- MN LLC filing
- EIN
- bank account
- general liability and property insurance
- operating agreement

OWNERSHIP: Gandi owns 100%. Any record implying Samsam co-owns NH is wrong — flag it if you see one.

THE RELATED-PARTY LEASE — this is the compliance item that matters. GAK Properties is the landlord and Gandi is on both sides of the deal (NH tenant, GAK landlord, west wing of 720 E Lake St). It therefore needs an arm's-length rate, documentation on BOTH entities' books, and disclosure to the CPA. The signing target was around June 2026.

Report the lease's current status from indexed documents and email. Do not treat it as signed unless you can point to a signed document — if you cannot, say so and list it as the open item. Tag it RELATED PARTY.

End with what is blocking formation, and who has to move.
"""),
    },
    {
        "name": "Week ahead: father's care & schedule conflicts",
        "business": "per",
        "apps": ["google-calendar", "google-mail"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["18:00"], [6]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
Sunday evening look at the week ahead. Personal lane only — never reference a business's books.

FATHER'S CARE FIRST. Yusuf Mohamed Hared is 97 and Gandi is his daily caregiver. For the coming 7 days report:
- every appointment: date, time, provider, location, and whether transport is arranged
- prescription refills coming due
- home-care visits scheduled, and any gap in coverage
- anything from a clinic, pharmacy or insurer that needs a response this week

THEN the rest of the week:
- personal commitments, and where they collide with Gandi's business calendar
- conflicts with Twin Cities prayer times — compute them for each actual date, do not assume fixed clock times
- religious-calendar dates coming up (Ramadan, Eid al-Fitr, Eid al-Adha, Hajj) and what they will displace
- anything booked over the Kenya Window (6:00–9:00 AM CST), which is reserved for business
- overlaps with Samsam's schedule that need coordinating

Read-only. Flag conflicts; never move or create a calendar entry.

End with the one day this week most likely to fall apart, and why.
"""),
    },
    {
        "name": "Month-close prep (all entities)",
        "business": None,
        "apps": ["quickbooks", "google-mail", "google-drive"],
        "autonomy": "readonly",
        "schedule": sched("weekly", ["13:00"], [2]),
        "timezone": CT,
        "rollout": True,
        "prompt": body("""
DATE GATE — check this first: if today is not within the last 6 days of the month or the first 6 days of the month, reply with the single line "Not in the month-close window." and stop. Do not call any tool.

Inside the window, produce a close checklist for each entity separately — GAK Properties, GIF Properties, Afro Farm, GAK Commodities, Nour Hammam. Never blend two entities' books.

Per entity, from its own QuickBooks file:
- uncategorised transactions
- unmatched deposits
- open invoices past terms
- bank-feed against ledger deltas
- missing vendor W-9s

Then the items that need documenting rather than fixing:
- RELATED-PARTY entries needing a memo: 1st Attentive Services LLC (Samsam's company) as tenant at 1108 Pearson, 1717 Thomas and 8101 Vincent; Nour Hammam at 720 E Lake once that lease is live. Each needs arm's-length documentation on both sides and CPA disclosure.
- At 1229 E Lake, confirm signage income (Clear Channel) is still booked on its own line and not folded into base rent.

NEVER write to QuickBooks. This is a checklist for Gandi and for USFAAF, not a set of corrections to apply.

Give each item a status: clear, needs Gandi, or needs the CPA.
"""),
    },
]


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    ap.add_argument("--notify-to", default="", help="set notify_to on every kept/created task")
    ap.add_argument("--enable-all", action="store_true", help="enable every task, not just the first-rollout set")
    args = ap.parse_args()

    live = {t["task_id"]: t for t in repo.list_tasks()}
    print(f"Live tasks: {len(live)}")
    print(f"Plan: delete {len(DELETE)}, rewrite {len(KEEP)}, create {len(NEW)} "
          f"-> {len(KEEP) + len(NEW)} tasks\n")

    verb = "" if args.apply else "[dry run] "

    # scheduled_task_runs.task_id is ON DELETE CASCADE, so deleting a task takes
    # its whole run history with it — including `reply`, the generated report
    # text. There is no undo, so never delete without an export on disk first.
    if args.apply and DELETE:
        from backup_scheduled_tasks import export
        export()

    print("--- DELETE ---")
    for task_id, expected_name, why in DELETE:
        row = live.get(task_id)
        if row is None:
            print(f"  SKIP {task_id} — not found (already gone?)")
            continue
        if row.get("name") != expected_name:
            print(f"  SKIP {task_id} — name is {row.get('name')!r}, expected {expected_name!r}")
            continue
        print(f"  {verb}delete {expected_name} — {why}")
        if args.apply and not repo.delete_task(task_id):
            print("    ! delete failed")

    print("\n--- REWRITE ---")
    for spec in KEEP:
        task_id = spec["task_id"]
        if task_id not in live:
            print(f"  SKIP {task_id} ({spec['name']}) — not found")
            continue
        enabled = True if args.enable_all else bool(spec.get("rollout"))
        _write(spec, task_id, enabled, args, verb, existing=True)

    # Re-running must converge, not accumulate: a task this catalog already
    # created is updated in place rather than created a second time.
    by_name = {t["name"]: t["task_id"] for t in live.values()}

    print("\n--- CREATE ---")
    for spec in NEW:
        enabled = True if args.enable_all else bool(spec.get("rollout"))
        existing_id = by_name.get(spec["name"])
        if existing_id:
            print(f"  (already exists — updating in place)")
            _write(spec, existing_id, enabled, args, verb, existing=True)
        else:
            _write(spec, None, enabled, args, verb, existing=False)

    if not args.apply:
        print("\nNothing was written. Re-run with --apply to commit this plan.")
    return 0


def _write(spec: dict[str, Any], task_id: str | None, enabled: bool,
           args: argparse.Namespace, verb: str, *, existing: bool) -> None:
    biz = spec["business"]
    entity = BUSINESS_TO_ENTITY_SLUG[biz] if biz else None
    tz = spec["timezone"]
    nxt = compute_next_run(spec["schedule"], tz) if enabled else None
    s = spec["schedule"]
    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    days = ",".join(labels[d] for d in s["days_of_week"]) or "-"
    where = biz or "all"
    state = "on" if enabled else "off"
    print(f"  {verb}{'update' if existing else 'create'} [{where:>4}] {spec['name']} "
          f"({s['frequency']} {days} {','.join(s['times'])} {tz}, {spec['autonomy']}, {state})")

    if not args.apply:
        return

    if existing:
        ok = repo.update_task(
            task_id,
            name=spec["name"],
            prompt=compose(spec),
            schedule=spec["schedule"],
            timezone_name=tz,
            allowed_apps=apps_for(spec),
            autonomy=spec["autonomy"],
            enabled=enabled,
            entity_slug=entity,
            entity_slug_set=True,
            next_run_at=nxt,
            next_run_set=True,
            notify_to=(args.notify_to or None),
            notify_to_set=bool(args.notify_to),
        )
        if not ok:
            print("    ! update failed")
    else:
        new_id = repo.create_task(
            entity_slug=entity,
            name=spec["name"],
            prompt=compose(spec),
            schedule=spec["schedule"],
            timezone_name=tz,
            allowed_apps=apps_for(spec),
            autonomy=spec["autonomy"],
            enabled=enabled,
            next_run_at=nxt,
            notify_to=(args.notify_to or None),
        )
        if not new_id:
            print("    ! create failed")


if __name__ == "__main__":
    sys.exit(main())
