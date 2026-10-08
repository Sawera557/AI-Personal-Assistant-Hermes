"""Supplementary tests for hermes/artefacts.py (feat/artefactrs).

The branch ships tests/test_artefacts.py (35 checks: dashboard rendering, number
parsing, sync caching). This adds the areas those don't reach:

  * XSS — an artefact renders LIVE external data (Sheets cells, Docs text, file
    names) into HTML. Every render path is fed hostile input.
  * Shell-token injection — a source value equal to a shell placeholder.
  * Tenant isolation — a missing per-business key must NEVER fall back to
    another business's Maton client (the bug that breaks /api/bills email-body).

Offline: no DB, no network, no LLM. Scratch file — not intended to be committed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["HERMES_DISABLE_DB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes import artefacts as A  # noqa: E402

FAILS: list[str] = []
XSS = '<script>alert(1)</script>'
IMG = '<img src=x onerror=alert(1)>'


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  -- ' + detail}")
    if not cond:
        FAILS.append(label)


def clean(html: str, label: str) -> None:
    """No executable markup from source data survived into the output.

    The test is whether the payload's ANGLE BRACKETS were escaped — an escaped
    `&lt;img src=x onerror=alert(1)&gt;` is inert text, so searching for the
    substring `onerror=` would be a false positive.
    """
    check(f"{label}: no raw <script>", "<script>alert(1)</script>" not in html)
    check(f"{label}: no raw <img> tag from source", "<img src=x" not in html)
    check(f"{label}: payload present but escaped", "&lt;script&gt;" in html or "&lt;img" in html,
          "payload missing entirely — check the fixture reached the renderer")


# ── 1. XSS through every render path ─────────────────────────────────────────
print("\n[1] hostile source data — table render")
rows = {"kind": "rows", "rows": [["Name", XSS], [IMG, "ok"]], "range": XSS}
clean(A.render_html("sheets", {"render": "table"}, rows), "table")

print("\n[2] hostile source data — kv render")
clean(A.render_html("sheets", {"render": "kv"}, rows), "kv")

print("\n[3] hostile source data — raw/text render")
text = {"kind": "text", "text": XSS + "\n" + IMG, "title": XSS}
clean(A.render_html("docs", {"render": "raw"}, text), "raw-text")

print("\n[4] hostile source data — dashboard labels")
dash_rows = {"kind": "rows", "rows": [["Vendor", "Amount"], [XSS, "100"], [IMG, "50"]], "range": None}
recipe = {"render": "dashboard", "charts": [{"type": "bar", "group_by": "Vendor", "value": "Amount"}]}
clean(A.render_html("sheets", recipe, dash_rows), "dashboard")

print("\n[5] hostile file metadata — link/embed render")
link = {"kind": "link", "name": XSS, "mime": IMG, "web_link": "https://x/" + XSS, "note": XSS}
clean(A.render_html("drive", {"render": "raw"}, link), "drive-link")

print("\n[6] hostile title / error message")
clean(A.render_error_canvas(XSS, IMG), "error-canvas")


# ── 7. shell-token injection ─────────────────────────────────────────────────
print("\n[7] source value equal to a shell placeholder")
tok_rows = {"kind": "rows",
            "rows": [["h", "__A_TITLE__"], ["__A_BODY__", "__A_SUBTITLE__"]], "range": None}
h = A.render_html("sheets", {"render": "table", "title": "Real Title"}, tok_rows)
# A token appearing as CELL TEXT is fine and expected — _page() does ONE regex
# pass over the shell, so replacement text is never re-scanned. What must hold
# is that the shell itself is intact and the real title wasn't displaced.
check("shell head has no leftover placeholder",
      "__A_TITLE__" not in h.split("</head>")[0], "shell token survived in <head>")
check("real title still rendered", "Real Title" in h)
check("document well-formed (single <html>)", h.count("<html") == 1, str(h.count("<html")))
check("token-as-data did not spawn a second body", h.count("<body") == 1, str(h.count("<body")))


# ── 8. tenant isolation on sync ──────────────────────────────────────────────
print("\n[8] tenant isolation — missing per-business key")
built: list[str | None] = []


class SpyClient:
    def __init__(self, api_key=None):
        built.append(api_key)


A.MatonClient = SpyClient
A.Config.maton_key_for_slug = staticmethod(lambda slug: None)   # business disconnected
saved: list = []
A.artefacts_db.get_render_inputs = staticmethod(lambda aid: {
    "artefact_id": aid, "title": "T", "business_slug": "nh", "source_type": "sheets",
    "source_binding": {"spreadsheet_id": "x"}, "render_recipe": {"render": "table"},
})
A.artefacts_db.save_error = staticmethod(lambda aid, msg: saved.append(msg))
A.artefacts_db.get_cached_html = staticmethod(lambda aid: (None, None))

out = A.sync_and_render("a1")
check("no Maton client constructed when key is missing", built == [], str(built))
check("error recorded", bool(saved), str(saved))
check("error names the business", "nh" in (saved[0] if saved else ""), str(saved[:1]))
check("returns an error canvas, not None/exception", bool(out) and "<html" in out)

print("\n[8b] correct key IS used when present")
built.clear()
A.Config.maton_key_for_slug = staticmethod(lambda slug: f"key-for-{slug}")
A._fetch = lambda st, cl, b: {"kind": "rows", "rows": [["a"]], "range": None}
A.artefacts_db.save_render = staticmethod(lambda aid, html: None)
A.sync_and_render("a1")
check("client built with THIS business's key", built == ["key-for-nh"], str(built))


# ── 9. validation ────────────────────────────────────────────────────────────
print("\n[9] source/render validation")
check("VALID_SOURCE_TYPES as documented",
      A.VALID_SOURCE_TYPES == {"sheets", "drive", "docs", "reviews"}, str(A.VALID_SOURCE_TYPES))
check("VALID_RENDERS as documented",
      A.VALID_RENDERS == {"table", "kv", "raw", "dashboard", "reviews"}, str(A.VALID_RENDERS))
unknown = A.render_html("sheets", {"render": "not-a-render"}, rows)
check("unknown render still returns a page (no crash)", "<html" in unknown)
clean(unknown, "unknown-render")

print("\n[10] row clamping")
big = {"kind": "rows", "rows": [[f"r{i}", i] for i in range(5000)], "range": None}
hb = A.render_html("sheets", {"render": "table"}, big)
check("huge sheet does not emit all 5000 rows",
      hb.count("<tr") <= A._MAX_ROWS_DEFAULT + 5, f"{hb.count('<tr')} <tr> tags")

print("\n" + "=" * 62)
print(f"RESULT: {len(FAILS)} failure(s)" + (": " + ", ".join(FAILS) if FAILS else ""))
sys.exit(1 if FAILS else 0)
