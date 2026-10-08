"""Update the Nour Hammam backfill task's prompt: save each day's batch as a .md
Markdown file, and add a WATI pagination hint learned from live testing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from hermes.db import scheduled_tasks as db

TASK_ID = "9d797bbe-5104-400f-93ea-7355acbd3c9a"

PROMPT = (
    "Scan ALL WhatsApp conversations for messages related to Nour Hammam — the "
    "Moroccan spa / women's hospitality venue (variants: 'Nour Hammam', 'Noor "
    "Hammam', 'hammam', 'Moroccan spa', 'women's spa', 'hospitality', 'wellness').\n\n"
    "How to read WhatsApp: WATI has no date-filtered feed, and its contact/message "
    "lists CAP page_size at ~10 and are 0-indexed — so page through by page number "
    "starting at 0 until a page comes back empty (don't assume one page is all). "
    "(1) List all WhatsApp contacts across every page; (2) for each contact pull "
    "their message history (page until empty); (3) keep ONLY messages dated on this "
    "run's single target day (the DATE WINDOW instruction above). From those, pick "
    "out anything about Nour Hammam: the 720 E Lake buildout, services, women's "
    "hospitality, staffing/hiring, suppliers/vendors, bookings, pricing, branding, "
    "permits, or any related discussion.\n\n"
    "Then write the batch report for this one day AS MARKDOWN and SAVE it with "
    "create_file using the filename 'Nour Hammam WhatsApp <the target date>.md' "
    "(a .md file; pass content_type 'text/markdown'). Format it in Markdown: a "
    "'# ' title line with the date, then a bullet per relevant message (contact "
    "name + number, time, and a short quote or summary), the topic, and a 2–3 line "
    "day summary. If there were NO Nour Hammam-related messages that day, still save "
    "a short .md file saying 'No Nour Hammam mentions on <date>.' so the batch "
    "series stays complete.\n\n"
    "This is read-only: do not send, reply to, or modify anything."
)

ok = db.update_task(TASK_ID, prompt=PROMPT)
print("updated:", ok)
t = db.get_task(TASK_ID)
print("prompt now ends with:", t["prompt"][-90:].encode("ascii", "replace").decode())
