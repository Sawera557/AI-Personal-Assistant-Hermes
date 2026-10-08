"""LinkedIn carousel — "The Golden Guide" — 1080x1350 (4:5).

Structure: SLAY as the engagement arc (Stop / Lead / Amplify / Yield) wrapped
around STAR as the case backbone (Situation / Task / Action / Result).
Editorial palette: cream content pages, navy section dividers, muted gold accent.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

W, H = 1080, 1350
M = 92
OUT = Path(r"C:\Users\Asus Rog Strix G17\Documents\LinkedIn_Carousel_Golden_Guide.pdf")

CREAM = HexColor("#FBF9F5")
NAVY = HexColor("#101826")
INK = HexColor("#131820")
MUTED = HexColor("#5E6875")
GOLD = HexColor("#A8842C")
GOLD_LT = HexColor("#D9BC70")
RULE = HexColor("#DED8CB")
RULE_D = HexColor("#2A3547")

SER_B, SER = "Times-Bold", "Times-Roman"
COL = 292  # x-offset for the definition column
SAN_B, SAN = "Helvetica-Bold", "Helvetica"

# (variant, kicker, headline, body[], stat, stat_label)
SLIDES = [
    ("hook", "THE GOLDEN GUIDE",
     "How I streamlined\nsix businesses\non Claude",
     ["An agentic AI architecture case study",
      "Three custom MCP servers  ·  40+ systems  ·  $31,000 saved"], "", ""),

    ("promise", "WHAT THIS COVERS",
     "Six moves that made\nit work.",
     ["Told in STAR — Situation, Task, Action, Result — because the",
      "architecture only makes sense once you know the constraint.",
      "",
      "01   Why off-the-shelf tooling could not be used",
      "02   The incident that redefined the project",
      "03   Three MCP servers, and the auth problem they solved",
      "04   Autonomy ceilings for unattended agents",
      "05   Retrieval tuned toward silence",
      "06   Auditing my own system in public"], "", ""),

    ("divider", "SITUATION", "The founder was\npaying more to get\nworse answers.",
     ["Part one of four"], "", ""),

    ("content", "SITUATION · 01",
     "Six businesses.\nNo single view.",
     ["A farm in Kenya. Three US property and commodity entities.",
      "A spa. Personal and family affairs.",
      "",
      "Each with its own mail, calendar, drive, accounting and messaging.",
      "No consolidated cash position. No way to ask a question across",
      "entities. Managers who could not see their own operations without",
      "going through the founder first."], "", ""),

    ("stat", "SITUATION · 02",
     "And the assistant was\ngetting worse.",
     ["Unnecessary skills, connectors and plugins. Several memory files",
      "contradicting one another. A local database nobody had validated.",
      "Each one claiming context on every single turn.",
      "",
      "Cost and quality had decoupled entirely."],
     "$1,000+", "per week in usage credits, on a $200/month plan"),

    ("content", "SITUATION · 03",
     "The data could not\nbe pooled.",
     ["Six entities with separate legal standing. One handling protected",
      "health information.",
      "",
      "Every off-the-shelf answer wanted to centralise the data first.",
      "That was the one thing that could never happen.",
      "",
      "Isolation had to be structural — enforced by credentials, not by",
      "a filter and not by a prompt."], "", ""),

    ("divider", "TASK", "Then it invented\na payroll figure.",
     ["Part two of four"], "", ""),

    ("content", "TASK · 01",
     "A trust problem,\nnot a capability one.",
     ["July 2026. The agent produced a payroll number close to",
      "KES 900,000 and stated it with complete confidence.",
      "",
      "It was fiction.",
      "",
      "My client makes payroll and tenancy decisions from these answers.",
      "One confident wrong number costs more than ten missing features.",
      "",
      "Everything after this point was designed around that."], "", ""),

    ("content", "TASK · 02",
     "The four mandates.",
     ["GROUNDING||Never state what cannot be traced to a source",
      "",
      "ISOLATION||No entity may ever reach another's data",
      "",
      "AUTONOMY||Unattended runs need a ceiling, not a hope",
      "",
      "ECONOMY||Context is the budget; spend it deliberately"], "", ""),

    ("divider", "ACTION", "Three MCP servers,\nand the guardrails\naround them.",
     ["Part three of four"], "", ""),

    ("content", "ACTION · 01",
     "Three custom\nMCP servers.",
     ["KNOWLEDGE||Vector search with a sensitivity ceiling",
      "||hard-capped in code — restricted material",
      "||can never be served.",
      "",
      "DOCUMENTS||Real OCR over scans, phone photos and",
      "||architectural floormaps, returning",
      "||schema-locked JSON.",
      "",
      "GATEWAY||40+ applications behind one authenticated",
      "||surface, per-business OAuth doubling as",
      "||the isolation boundary."], "", ""),

    ("content", "ACTION · 02",
     "A token-only server\ncannot be added at all.",
     ["Claude's connector dialog accepts a URL and OAuth credentials.",
      "There is no request-header field, so bearer-token servers fail",
      "at registration.",
      "",
      "The fix was dual-mode: a transparent OAuth proxy delegating",
      "identity to the corporate IdP with an email allowlist for web,",
      "Desktop and mobile — plus bearer token for Claude Code and the",
      "Messages API."], "", ""),

    ("stat", "ACTION · 03",
     "Batch, with failure\nisolation.",
     ["Twenty files per call, processed concurrently server-side. A batch",
      "costs its slowest file rather than the sum, and one corrupt",
      "document returns its own error while the other nineteen succeed."],
     "4.5x", "20 PDFs in 12.5s against ~56s sequential"),

    ("stat", "ACTION · 04",
     "Nobody is watching\nan unattended run.",
     ["READ-ONLY||read, analyse, report",
      "DRAFT||may create drafts, never sends",
      "FULL||may send and modify — explicit standing authority",
      "",
      "Plus a prepare-then-send confirmation on every destructive action,",
      "enforced by control flow rather than by asking the model nicely."],
     "21", "unattended automations live in production"),

    ("content", "ACTION · 05",
     "Recall is retrieval.\nNever generation.",
     ["Stored, timestamped, source-tagged records are injected verbatim",
      "as citable evidence. The model may say \"you told me\" only about",
      "text that literally appears in a record. Otherwise the answer is",
      "\"I don't have that.\"",
      "",
      "The relevance threshold is tuned toward silence. Injecting nothing",
      "beats injecting a weak match — an empty context produces",
      "\"I don't know\", a marginal one produces confident invention."], "", ""),

    ("content", "ACTION · 06",
     "Reachability is not\nlocality of processing.",
     ["Protected health information, immigration material and anything",
      "marked restricted are recorded in the database only — never sent",
      "to a cloud OCR service or an embedding API.",
      "",
      "For binary files the gate runs twice: once on filename and metadata",
      "before extraction, then again on the extracted text, pulling the",
      "bytes back out of storage if the second pass blocks."], "", ""),

    ("content", "ACTION · 07",
     "Then I audited\nmy own system.",
     ["Five findings, published with the architecture:",
      "",
      "1   Context packs dead in production — a migration never applied",
      "2   Retrieval reaching 0.3% of everything ingested",
      "3   The autonomy ceiling was never actually enforced",
      "4   Write gates that existed only in the prompt",
      "5   A migration ledger that could not be trusted",
      "",
      "A system built to never overstate what it knows cannot be",
      "documented any other way."], "", ""),

    ("divider", "RESULT", "What it was\nworth.",
     ["Part four of four"], "", ""),

    ("stat", "RESULT · 01",
     "One dashboard,\nreconciled nightly.",
     ["Portfolio-wide cash position across all six businesses — money in,",
      "money out, what is owed — with every figure verified against",
      "QuickBooks rather than trusted from a spreadsheet."],
     "$31,000", "saved per year"),

    ("content", "RESULT · 02",
     "Managers stopped\nasking the founder.",
     ["Farm operations across years of historical data, with attendance",
      "reports driving payroll for roughly 54 staff.",
      "",
      "Rental tenancy tracking that issues invoices and applies late fees",
      "without anyone remembering to.",
      "",
      "A spa dashboard that sends drafted emails to contacts from the",
      "same screen the context lives on.",
      "",
      "And the usage overrun that started all of this is gone."], "", ""),

    ("lessons", "YIELD",
     "Five things I would\nkeep.",
     ["01   Run the sensitivity gate twice — a filename lies about what",
      "is inside the file.",
      "",
      "02   Tune retrieval toward silence — a confident wrong answer",
      "costs more than a missing one.",
      "",
      "03   Make seams explicit rather than merging them, when the",
      "merge is a migration across live financial data.",
      "",
      "04   Document the limits you find, so the next engineer meets a",
      "constraint instead of a mystery.",
      "",
      "05   Audit your own work in public. It is the only way a trust",
      "system earns the name."], "", ""),

    ("outro", "", "Sawera Khadium",
     ["Agentic AI Architect",
      "Enterprise LLM, MCP & RAG systems",
      "",
      "Full case study and architecture available on request.",
      "",
      "linkedin.com/in/sawera-khadium",
      "github.com/Sawera557"], "", ""),
]

BOLD_PREFIX = ("KNOWLEDGE", "DOCUMENTS", "GATEWAY", "READ-ONLY", "DRAFT", "FULL",
               "GROUNDING", "ISOLATION", "AUTONOMY", "ECONOMY")


def wrap(text, font, size, maxw):
    return simpleSplit(text, font, size, maxw) or [""]


PART_NO = {"SITUATION": "01", "TASK": "02", "ACTION": "03", "RESULT": "04"}


def layout(variant, kicker, headline, body, stat):
    """Measure the content block so it can be optically centred."""
    hsize = 82 if variant == "hook" else 74 if variant in ("divider", "outro") else 56
    hfont = SER_B if variant in ("hook", "divider", "outro") else SAN_B
    lines = []
    for para in headline.split("\n"):
        lines.extend(wrap(para, hfont, hsize, W - 2 * M))
    total = (66 if kicker else 0) + len(lines) * hsize * 1.13
    if body:
        total += 66 + len(body) * 40
    if stat:
        total += 56 + 118 + 44
    return hsize, hfont, lines, total


def draw(c, i, slide):
    variant, kicker, headline, body, stat, stat_label = slide
    dark = variant in ("hook", "divider", "outro")
    bg, ink, muted, accent, rule = (
        (NAVY, CREAM, HexColor("#95A2B5"), GOLD_LT, RULE_D) if dark
        else (CREAM, INK, MUTED, GOLD, RULE))

    c.setFillColor(bg)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # oversized ghost numeral anchors the section dividers
    if variant == "divider":
        c.setFillColor(HexColor("#18233A"))
        c.setFont(SER_B, 540)
        c.drawRightString(W - M, M + 34, PART_NO.get(kicker, ""))

    c.setFillColor(rule)
    c.rect(M, H - M + 26, W - 2 * M, 1.5, fill=1, stroke=0)
    c.setFillColor(accent)
    c.rect(M, H - M + 26, 78, 1.5, fill=1, stroke=0)

    hsize, hfont, lines, total = layout(variant, kicker, headline, body, stat)
    top_y, bot_y = H - M - 6, M + 34
    y = top_y - max(0.0, (top_y - bot_y - total) * 0.40)  # optical centre, slight top bias

    if kicker:
        c.setFillColor(accent)
        c.setFont(SAN_B, 19)
        c.drawString(M, y - 16, kicker)
        y -= 66

    c.setFillColor(ink)
    c.setFont(hfont, hsize)
    for ln in lines:
        y -= hsize * 1.13
        c.drawString(M, y, ln)

    if body:
        y -= 44
        c.setFillColor(accent)
        c.rect(M, y + 18, 62, 2.5, fill=1, stroke=0)
        y -= 22
        for ln in body:
            y -= 40
            if "||" in ln:  # aligned label / definition columns
                label, _, defn = ln.partition("||")
                if label:
                    c.setFont(SAN_B, 26)
                    c.setFillColor(ink)
                    c.drawString(M, y, label)
                c.setFont(SAN, 26)
                c.setFillColor(muted if label else muted)
                c.drawString(M + COL, y, defn)
            else:
                c.setFont(SAN, 26)
                c.setFillColor(ink if variant in ("hook", "outro") else muted)
                c.drawString(M, y, ln)

    if stat:
        # tied to the body above it by a rule, not floated at the page foot
        y -= 34
        c.setFillColor(accent)
        c.rect(M, y, 250, 2.5, fill=1, stroke=0)
        y -= 110
        c.setFillColor(accent)
        c.setFont(SER_B, 112)
        c.drawString(M, y, stat)
        y -= 40
        c.setFillColor(muted)
        c.setFont(SAN, 25)
        c.drawString(M, y, stat_label)

    # footer
    c.setFillColor(rule)
    c.rect(M, M - 14, W - 2 * M, 1.5, fill=1, stroke=0)
    c.setFillColor(muted)
    c.setFont(SAN, 19)
    c.drawString(M, M - 48, "Sawera Khadium  ·  Agentic AI Architect")
    c.setFont(SAN_B, 19)
    c.setFillColor(accent)
    if variant == "hook":
        c.drawRightString(W - M, M - 48, "swipe")
    elif variant != "outro":
        c.drawRightString(W - M, M - 48, f"{i:02d} / {len(SLIDES) - 1:02d}")


if __name__ == "__main__":
    c = canvas.Canvas(str(OUT), pagesize=(W, H))
    for i, s in enumerate(SLIDES):
        draw(c, i, s)
        c.showPage()
    c.save()
    print(f"wrote {OUT.name} — {len(SLIDES)} slides at {W}x{H}")
