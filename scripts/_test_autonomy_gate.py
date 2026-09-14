"""Prove the scheduled-task autonomy tier is a capability gate, not just prompt
text: a readonly or draft run must have no send/write tool at all.

    python scripts/_test_autonomy_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

from hermes.tools import build_tools, write_blocked_reason  # noqa: E402

P = {"pass": 0, "fail": 0}


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    P["pass" if cond else "fail"] += 1


APPS = ["google-mail", "google-calendar", "google-drive", "google-sheets", "quickbooks"]


def names(autonomy):
    return [t["name"] for t in build_tools("gak", APPS, autonomy=autonomy)]


def methods(autonomy):
    for t in build_tools("gak", APPS, autonomy=autonomy):
        if t["name"] == "maton_request":
            return t["input_schema"]["properties"]["method"].get("enum")
    return None


print("\nSchema: the model is not offered a send tool")
for au in ("readonly", "draft"):
    n = names(au)
    check(f"{au}: send_email withheld", "send_email" not in n)
    check(f"{au}: email_with_attachment withheld", "email_with_attachment" not in n)
    check(f"{au}: maton_create_connection withheld", "maton_create_connection" not in n)
    check(f"{au}: search_business_knowledge still available", "search_business_knowledge" in n)
    check(f"{au}: create_file still available (reports need it)", "create_file" in n)

check("readonly: maton_request is GET-only", methods("readonly") == ["GET"])
check("draft: maton_request is GET+POST (drafts)", methods("draft") == ["GET", "POST"])

print("\nChat and full autonomy are unchanged")
for au in (None, "full"):
    n = names(au)
    check(f"{au}: send_email present", "send_email" in n)
    check(f"{au}: maton_request keeps all methods", len(methods(au) or []) == 5)

print("\nDispatch: the call is refused even if it arrives anyway")
SEND = "/gmail/v1/users/me/messages/send"
DRAFTS = "/gmail/v1/users/me/drafts"
SHEET = "/v4/spreadsheets/X/values/A1"
cases = [
    ("send_email blocked (readonly)", "send_email", {}, "readonly", True),
    ("send_email blocked (draft)", "send_email", {}, "draft", True),
    ("send_email allowed (full)", "send_email", {}, "full", False),
    ("send_email allowed (chat)", "send_email", {}, None, False),
    ("raw Gmail send blocked (readonly)", "maton_request", {"method": "POST", "path": SEND}, "readonly", True),
    ("raw Gmail send blocked (draft)", "maton_request", {"method": "POST", "path": SEND}, "draft", True),
    ("Gmail draft allowed (draft)", "maton_request", {"method": "POST", "path": DRAFTS}, "draft", False),
    ("Gmail draft blocked (readonly)", "maton_request", {"method": "POST", "path": DRAFTS}, "readonly", True),
    ("Sheets write blocked (readonly)", "maton_request", {"method": "PUT", "path": SHEET}, "readonly", True),
    ("Sheets write blocked (draft)", "maton_request", {"method": "PUT", "path": SHEET}, "draft", True),
    ("Sheets read allowed (readonly)", "maton_request", {"method": "GET", "path": SHEET}, "readonly", False),
    ("no method defaults to read (readonly)", "maton_request", {"path": SHEET}, "readonly", False),
]
for label, name, ti, au, want_blocked in cases:
    check(label, bool(write_blocked_reason(name, ti, au)) is want_blocked)

print(f"\n{P['pass']} passed, {P['fail']} failed")
sys.exit(1 if P["fail"] else 0)
