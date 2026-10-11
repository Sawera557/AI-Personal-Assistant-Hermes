"""Fetch ALL WhatsApp messages Jun 17 -> Jul 17 2026 (every conversation).

Direct WATI pull (no LLM), de-duplicated (WATI returns overlapping pages),
bucketed by America/Chicago day and by contact. Read-only. Saves a full markdown
transcript + prints a per-day summary.
"""
import os, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from hermes.config import Config
from hermes.maton import MatonClient
from hermes.wati import WatiClient

CHI = ZoneInfo("America/Chicago")
START, END = date(2026, 6, 17), date(2026, 7, 17)
PAGE = 10  # WATI caps page_size at ~10

def unwrap(r):
    return r.get("data", r) if isinstance(r, dict) else r

def local_dt(created):
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(CHI)
    except Exception:
        return None

wc = WatiClient(MatonClient(api_key=Config.maton_key_for_slug("nh")))

# 1) all contacts (page by number until empty; nextPage flag is unreliable)
contacts, page = [], 0
while page < 50:
    payload = unwrap(wc.list_contacts(page_size=PAGE, page_number=page))
    batch = payload.get("contact_list", []) if isinstance(payload, dict) else []
    if not batch:
        break
    contacts.extend(batch)
    page += 1
print(f"contacts: {len(contacts)}", flush=True)

OUT = os.path.join(os.path.dirname(__file__), "nh_all_messages_jun17_jul17.md")

def safe(s):  # ascii-safe for the Windows console; the file itself stays utf-8
    return str(s).encode("ascii", "replace").decode()

def write_report(contacts, by_day, total):
    lines = ["# All WhatsApp messages -- Jun 17 -> Jul 17, 2026 (all conversations)\n",
             f"- Contacts: **{len(contacts)}**",
             f"- Unique messages in range: **{total}**\n",
             "## Per-day counts\n"]
    d = START
    while d <= END:
        lines.append(f"- {d.isoformat()}: {len(by_day.get(d, []))} message(s)")
        d += timedelta(days=1)
    lines.append("\n---\n")
    d = START
    while d <= END:
        entries = sorted(by_day.get(d, []), key=lambda e: e[0])
        lines.append(f"\n## {d.isoformat()} - {len(entries)} message(s)\n")
        for (dt, name, num, direction, txt) in entries:
            snippet = (txt or "(no text / attachment)").replace("\n", " ")
            lines.append(f"- {dt.strftime('%H:%M')} - **{name}** ({num}) [{direction}]: {snippet}")
        d += timedelta(days=1)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# 2) all messages in range, de-duplicated. Wrapped so a late failure still writes
#    whatever was collected.
seen = set()
by_day = {}            # date -> list of (dt, name, num, direction, text)
total = 0
try:
    for i, c in enumerate(contacts):
        num = c.get("wAid") or c.get("phone")
        name = c.get("fullName") or c.get("firstName") or num
        if not num:
            continue
        p, stop = 0, False
        while not stop and p <= 120:
            mp = unwrap(wc.get_messages(str(num), page_size=PAGE, page_number=p))
            msgs = (mp.get("messages") or {}) if isinstance(mp, dict) else {}
            items = msgs.get("items", []) if isinstance(msgs, dict) else []
            if not items:
                break
            for m in items:
                if m.get("eventType") != "message":
                    continue
                created = m.get("created", "")
                dt = local_dt(created)
                if not dt:
                    continue
                d = dt.date()
                if d < START:
                    stop = True
                    continue
                if d > END:
                    continue
                txt = (m.get("text") or "").strip()
                key = (str(num), created, txt)
                if key in seen:
                    continue
                seen.add(key)
                direction = "me" if m.get("owner") else "them"
                by_day.setdefault(d, []).append((dt, name, str(num), direction, txt))
                total += 1
            p += 1
        print(f"  [{i+1}/{len(contacts)}] {safe(name)}: running total {total}", flush=True)
finally:
    write_report(contacts, by_day, total)
    print("\n=== PER-DAY SUMMARY ===", flush=True)
    d = START
    while d <= END:
        print(f"{d.isoformat()}: {len(by_day.get(d, []))}", flush=True)
        d += timedelta(days=1)
    print(f"\nTOTAL unique messages: {total}", flush=True)
    print(f"Saved full transcript to {OUT}", flush=True)
