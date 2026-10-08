"""Deterministic Nour Hammam WhatsApp report — direct WATI pull, no LLM.

Pulls every contact + their messages via WatiClient, buckets by America/Chicago
day across Jun 24 -> Jul 17 2026, and flags Nour Hammam-related messages by
keyword. Read-only. Exhaustive and fast (no agent token limits). Writes a
markdown report and prints it.
"""
import os, sys
from datetime import datetime, date
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from hermes.config import Config
from hermes.maton import MatonClient
from hermes.wati import WatiClient

CHI = ZoneInfo("America/Chicago")
START, END = date(2026, 6, 24), date(2026, 7, 17)
KEYWORDS = ["nour", "hammam", "hamam", "noor", "moroccan", "morrocan", "spa",
            "hospitality", "wellness", "720 e lake", "720 east lake", "massage",
            "esthetician", "facial", "salon"]

def unwrap(r):
    return r.get("data", r) if isinstance(r, dict) else r

def local_day(created):
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return dt.astimezone(CHI)
    except Exception:
        return None

wc = WatiClient(MatonClient(api_key=Config.maton_key_for_slug("nh")))

# 1) all contacts. WATI caps page_size at 10 and its nextPage flag is unreliable,
# so page by number (0-indexed) until a batch comes back empty.
PAGE = 10
contacts, page = [], 0
while page < 50:
    payload = unwrap(wc.list_contacts(page_size=PAGE, page_number=page))
    batch = payload.get("contact_list", []) if isinstance(payload, dict) else []
    if not batch:
        break
    contacts.extend(batch)
    page += 1
print(f"contacts fetched: {len(contacts)}", flush=True)

# 2) messages per contact, keep only those in range
by_day = {}          # date -> list of (contact, timelocal, owner, text, is_nh)
per_contact_hits = {}  # contact -> count of NH msgs
total_in_range = 0
for c in contacts:
    num = c.get("wAid") or c.get("phone")
    name = c.get("fullName") or c.get("firstName") or num
    if not num:
        continue
    p = 0
    stop = False
    while not stop and p <= 60:
        mp = unwrap(wc.get_messages(str(num), page_size=PAGE, page_number=p))
        msgs = (mp.get("messages") or {}) if isinstance(mp, dict) else {}
        items = msgs.get("items", []) if isinstance(msgs, dict) else []
        if not items:
            break
        for m in items:
            if m.get("eventType") != "message":
                continue
            txt = (m.get("text") or "").strip()
            ldt = local_day(m.get("created", ""))
            if not ldt:
                continue
            d = ldt.date()
            if d < START:
                stop = True   # newest-first: older than range -> done with this contact
                continue
            if d > END:
                continue
            total_in_range += 1
            is_nh = any(k in txt.lower() for k in KEYWORDS)
            by_day.setdefault(d, []).append((name, ldt.strftime("%H:%M"),
                                             "me" if m.get("owner") else "them", txt, is_nh))
            if is_nh:
                per_contact_hits[name] = per_contact_hits.get(name, 0) + 1
        p += 1   # page until empty items or a message older than the range (stop)

# 3) build report
nh_total = sum(per_contact_hits.values())
lines = ["# Nour Hammam WhatsApp — Jun 24 → Jul 17, 2026 (direct scan, all days)\n"]
lines.append(f"- Contacts scanned: **{len(contacts)}**")
lines.append(f"- Messages in range (Jun 24–Jul 17): **{total_in_range}**")
lines.append(f"- Nour Hammam-related messages (keyword match): **{nh_total}**")
lines.append(f"- Contacts with Nour Hammam mentions: **{len(per_contact_hits)}**\n")

if per_contact_hits:
    lines.append("## Contacts mentioning Nour Hammam\n")
    for nm, ct in sorted(per_contact_hits.items(), key=lambda x: -x[1]):
        lines.append(f"- **{nm}** — {ct} message(s)")
    lines.append("")

lines.append("## Day by day (Jun 24 → Jul 17)\n")
d = START
from datetime import timedelta
while d <= END:
    entries = by_day.get(d, [])
    nh = [e for e in entries if e[4]]
    iso = d.isoformat()
    if not entries:
        lines.append(f"### {iso} — no messages")
    elif not nh:
        lines.append(f"### {iso} — {len(entries)} message(s), 0 Nour Hammam mentions")
    else:
        lines.append(f"### {iso} — {len(nh)} Nour Hammam mention(s) (of {len(entries)} messages)")
        for (nm, tm, who, txt, _) in nh:
            snippet = txt.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(f"- {tm} · **{nm}** ({who}): {snippet}")
    d += timedelta(days=1)

report = "\n".join(lines)
out = os.path.join(os.path.dirname(__file__), "nh_scan_result.md")
with open(out, "w", encoding="utf-8") as f:
    f.write(report)
print(report.encode("ascii", "replace").decode(), flush=True)
print(f"\n(saved to {out})", flush=True)
