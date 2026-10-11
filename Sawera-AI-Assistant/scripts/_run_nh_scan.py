"""One-off: fast consolidated Nour Hammam WhatsApp scan for Jun 24 -> Jul 17.

One agent run (read-only) that scans all WhatsApp conversations once and buckets
Nour Hammam-related messages by day across the window where data exists, instead
of 31 separate per-day scans. Reuses the same business/apps/autonomy scope as the
scheduled task. Prints the full report. Safe to delete after.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from hermes.agent import run_turn
from hermes.memory import store

PROMPT = (
    "AUTONOMY: READ-ONLY. This is an automated run — read/analyze/report only, and "
    "you may use create_file. Do NOT send, reply to, or modify anything on WhatsApp "
    "or elsewhere.\n\n"
    "TASK: Produce a consolidated report of every WhatsApp message related to NOUR "
    "HAMMAM — the Moroccan spa / women's hospitality venue (variants: 'Nour Hammam', "
    "'Noor Hammam', 'hammam', 'Moroccan spa', 'women's spa', 'hospitality', "
    "'wellness', the 720 E Lake buildout) — across the date range JUNE 24 2026 "
    "through JULY 17 2026 (America/Chicago). Note: this WhatsApp account has no "
    "history before Jun 24 2026, so earlier days are empty.\n\n"
    "How to read WhatsApp (WATI has no date filter): (1) list ALL contacts, paging "
    "through every page; (2) for each contact pull their message history; (3) keep "
    "messages dated Jun 24 - Jul 17 2026 that relate to Nour Hammam.\n\n"
    "ORGANIZE THE REPORT BY DAY (one section per calendar day that has relevant "
    "messages, in date order). For each day list: the contact (name + number), the "
    "time, and a short quote/summary of each relevant message, plus a 1-2 line "
    "day summary. If a day in the range has no Nour Hammam messages, you may list it "
    "briefly as 'no mentions'. End with an overall summary: which contacts discuss "
    "Nour Hammam, the main themes (buildout, staffing, suppliers, bookings, pricing, "
    "branding, permits), and anything time-sensitive. Save the full report with "
    "create_file titled 'Nour Hammam WhatsApp — Jun 24-Jul 17 2026 (consolidated)'."
)

session = "__nh_scan_oneoff__"
try:
    res = run_turn(session, [{"type": "text", "text": PROMPT}], voice=False,
                   active_business="nh", allowed_apps=["wati"])
finally:
    store.reset(session)

print("=== STATUS:", "error" if res.get("stop_reason") == "refusal" else "success", "===", flush=True)
tools = [ (tc.get("name") or tc.get("tool") or "?") for tc in (res.get("tool_calls") or []) ]
print("tools used:", tools, flush=True)
print("\n=== REPORT ===\n", flush=True)
print((res.get("reply") or "(no reply)").encode("ascii", "replace").decode(), flush=True)

out = os.path.join(os.path.dirname(__file__), "nh_scan_result.md")
with open(out, "w", encoding="utf-8") as f:
    f.write(res.get("reply") or "")
print(f"\n(saved full reply to {out})", flush=True)
