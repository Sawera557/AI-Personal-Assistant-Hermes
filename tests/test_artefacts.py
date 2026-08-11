"""Tests for the Artefacts render pipeline (hermes/artefacts.py).

All network-free: the Maton client and the artefacts store are stubbed, so these
run without a DB or any external call. They lock the three things that matter:

  1. every source value is HTML-escaped into the canvas (no injection),
  2. sync_and_render re-fetches + re-renders deterministically, caches on success,
     and on failure records the error + serves the last-good cache — and NEVER
     imports/calls the LLM (opening an artefact must be LLM-free),
  3. the Google-Docs payload flattens to plain text correctly.

Run directly:

    python tests/test_artefacts.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("HERMES_DISABLE_DB", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes import artefacts  # noqa: E402
from hermes.config import Config  # noqa: E402
from hermes.maton import MatonClient  # noqa: E402

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


# ── 1. Rendering + escaping ──────────────────────────────────────────────────
def test_table_escapes_and_structures():
    rows = [["Name", "Qty"], ["<script>alert(1)</script>", "5"], ["Widget & Co", "12"]]
    html = artefacts.render_html("sheets", {"render": "table", "title": "Budget <b>"},
                                 {"kind": "rows", "rows": rows, "range": "Sheet1!A1:B3"})
    check("raw <script> never appears verbatim", "<script>alert(1)</script>" not in html)
    check("script cell is escaped", "&lt;script&gt;alert(1)&lt;/script&gt;" in html)
    check("ampersand cell is escaped", "Widget &amp; Co" in html)
    check("title is escaped in the page", "Budget &lt;b&gt;" in html)
    check("headers rendered as <th>", "<th>Name</th>" in html and "<th>Qty</th>" in html)
    check("data rendered as <td>", "<td>Widget &amp; Co</td>" in html)
    check("row count in subtitle", "2 rows" in html)


def test_table_max_rows_banner():
    rows = [["h"]] + [[str(i)] for i in range(10)]
    html = artefacts.render_html("sheets", {"render": "table", "options": {"max_rows": 3}},
                                 {"kind": "rows", "rows": rows})
    check("truncation banner shown", "Showing the first 3 of 10 rows" in html)


def test_kv_render():
    rows = [["Owner", "Gandi"], ["Region", "Kenya"]]
    html = artefacts.render_html("sheets", {"render": "kv"}, {"kind": "rows", "rows": rows})
    check("kv key present", "Owner" in html and "Region" in html)
    check("kv value present", "Gandi" in html and "Kenya" in html)


def test_raw_doc_escapes():
    data = {"kind": "text", "title": "Notes", "text": "line 1\n<img src=x onerror=1>\nline 3"}
    html = artefacts.render_html("docs", {"render": "raw"}, data)
    check("doc html escaped", "&lt;img src=x onerror=1&gt;" in html)
    check("no live img tag", "<img src=x" not in html)


def test_docs_plaintext_walk():
    doc = {"body": {"content": [
        {"paragraph": {"elements": [{"textRun": {"content": "Hello "}}, {"textRun": {"content": "world\n"}}]}},
        {"sectionBreak": {}},  # non-paragraph: skipped
        {"paragraph": {"elements": [{"textRun": {"content": "Line 2"}}]}},
    ]}}
    check("docs plaintext flattens", artefacts._docs_plaintext(doc) == "Hello world\nLine 2")


# ── 1b. Dashboard rendering (KPIs + SVG charts from live rows) ───────────────
def test_dashboard_kpis_and_charts():
    rows = [
        ["Date", "Vendor", "Category", "Total (KES)"],
        ["2025-04-10", "Amiran", "Farm inputs", "481,255"],
        ["2025-05-02", "Swaswa", "Fuel", "12,300"],
        ["2025-05-20", "Amiran", "Farm inputs", "9,000"],
        ["2026-03-15", "Josmar", "Household", "25,700"],
    ]
    recipe = {
        "render": "dashboard", "title": "AFK Receipts",
        "description": "Live spend view",
        "kpis": [
            {"label": "Total spend", "column": "Total (KES)", "agg": "sum"},
            {"label": "Receipts", "agg": "count"},
        ],
        "charts": [
            {"type": "bar", "title": "Spend by vendor", "group_by": "Vendor", "value": "Total (KES)", "agg": "sum"},
            {"type": "doughnut", "title": "By category", "group_by": "Category", "value": "Total (KES)", "agg": "sum"},
            {"type": "line", "title": "Monthly", "group_by": "Date", "value": "Total (KES)", "agg": "sum", "bucket": "month"},
        ],
        "table": True,
    }
    html = artefacts.render_html("sheets", recipe, {"kind": "rows", "rows": rows, "range": "A1:D5"})
    check("dashboard total sum KPI (481255+12300+9000+25700=528,255)", "528,255" in html)
    check("dashboard count KPI (4 data rows)", ">4<" in html)
    check("dashboard draws svg charts", html.count("<svg") >= 3)
    check("dashboard renders a chart title", "Spend by vendor" in html)
    check("dashboard vendor grouped (Amiran summed = 490,255)", "490,255" in html)
    check("dashboard description shown", "Live spend view" in html)
    check("dashboard appends table", "<table" in html)


def test_dashboard_escapes_labels():
    rows = [["Vendor", "Amt"], ["<script>x</script>", "5"], ["Safe", "3"]]
    recipe = {"render": "dashboard", "title": "T",
              "charts": [{"type": "bar", "title": "V", "group_by": "Vendor", "value": "Amt", "agg": "sum"}]}
    html = artefacts.render_html("sheets", recipe, {"kind": "rows", "rows": rows})
    check("chart label escaped in svg", "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html)


def test_number_parsing():
    check("KES + commas", artefacts._num("KES 481,255") == 481255.0)
    check("plain float", artefacts._num("43.7") == 43.7)
    check("blank -> None", artefacts._num("") is None)
    check("month bucket dmy", artefacts._month_bucket("10/04/2025") == "2025-04")
    check("month bucket iso", artefacts._month_bucket("2026-03-15") == "2026-03")


# ── 2. sync_and_render — success, failure, business-key resolution ───────────
class _FakeStore:
    def __init__(self, row):
        self.row = row
        self.saved_html = None
        self.saved_error = None
        self.cache = (None, None)

    def get_render_inputs(self, _id):
        return self.row

    def save_render(self, _id, html):
        self.saved_html = html

    def save_error(self, _id, msg):
        self.saved_error = msg

    def get_cached_html(self, _id):
        return self.cache


def _with_store(row):
    store = _FakeStore(row)
    artefacts.artefacts_db = store  # module-level reference used by sync_and_render
    return store


def test_sync_success_caches_html():
    store = _with_store({
        "title": "Budget", "source_type": "sheets", "business_slug": "nh",
        "source_binding": {"spreadsheet_id": "SID", "range": "Sheet1!A1:B2"},
        "render_recipe": {"render": "table", "title": "Budget"},
    })
    orig_key = Config.maton_key_for_slug
    orig_req = MatonClient.request
    Config.maton_key_for_slug = classmethod(lambda cls, slug: "fake-key")
    MatonClient.request = lambda self, app, method, path, query=None, body=None, connection_id=None: {
        "status": 200, "ok": True, "data": {"values": [["City", "Rent"], ["Seattle", "1200"]], "range": "Sheet1!A1:B2"},
    }
    try:
        html = artefacts.sync_and_render("art-1")
    finally:
        Config.maton_key_for_slug = orig_key
        MatonClient.request = orig_req
    check("success returns html", html and "Seattle" in html)
    check("success caches the html", store.saved_html is not None and "Seattle" in store.saved_html)
    check("success clears no error", store.saved_error is None)


def test_sync_failure_records_error_and_serves_stale():
    store = _with_store({
        "title": "Budget", "source_type": "sheets", "business_slug": "nh",
        "source_binding": {"spreadsheet_id": "SID", "range": "BadRange"},
        "render_recipe": {"render": "table"},
    })
    store.cache = ("<html>STALE</html>", "2026-08-01T00:00:00+00:00")
    orig_key = Config.maton_key_for_slug
    orig_req = MatonClient.request
    Config.maton_key_for_slug = classmethod(lambda cls, slug: "fake-key")
    MatonClient.request = lambda self, app, method, path, query=None, body=None, connection_id=None: {
        "status": 400, "ok": False, "data": {"error": {"message": "Unable to parse range"}},
    }
    try:
        html = artefacts.sync_and_render("art-2")
    finally:
        Config.maton_key_for_slug = orig_key
        MatonClient.request = orig_req
    check("failure records last_error", store.saved_error and "Unable to parse range" in store.saved_error)
    check("failure serves last-good cache", html == "<html>STALE</html>")


def test_sync_missing_key_renders_error_no_fetch():
    store = _with_store({
        "title": "Budget", "source_type": "sheets", "business_slug": "afk",
        "source_binding": {"spreadsheet_id": "SID", "range": "A1:B2"},
        "render_recipe": {"render": "table"},
    })
    orig_key = Config.maton_key_for_slug
    Config.maton_key_for_slug = classmethod(lambda cls, slug: None)
    try:
        html = artefacts.sync_and_render("art-3")
    finally:
        Config.maton_key_for_slug = orig_key
    check("missing key -> error recorded", store.saved_error and "Maton connection" in store.saved_error)
    check("missing key -> error canvas", html and "load this artefact" in html)


# ── 3. Opening an artefact must be LLM-free ──────────────────────────────────
def test_no_llm_imports_in_module():
    # Scan actual import statements (not comments/docstrings, which reference
    # these names to explain their deliberate absence).
    import inspect
    imports = [ln.strip() for ln in inspect.getsource(artefacts).splitlines()
               if ln.strip().startswith(("import ", "from "))]
    joined = "\n".join(imports)
    check("no 'anthropic' import in artefacts.py", "anthropic" not in joined)
    check("no agent import in artefacts.py", "agent" not in joined)


def main() -> int:
    tests = [
        test_table_escapes_and_structures,
        test_table_max_rows_banner,
        test_kv_render,
        test_raw_doc_escapes,
        test_docs_plaintext_walk,
        test_dashboard_kpis_and_charts,
        test_dashboard_escapes_labels,
        test_number_parsing,
        test_sync_success_caches_html,
        test_sync_failure_records_error_and_serves_stale,
        test_sync_missing_key_renders_error_no_fetch,
        test_no_llm_imports_in_module,
    ]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\nAll {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
