"""Tests for the Bills feature.

Two layers:

* **Offline** — pure extraction / parsing / routing logic in `hermes.bills`
  (NuExtract normalisation, filename vendor+date parsing, date/amount coercion,
  business guessing). No DB, no network.
* **Live DB round-trip** — insert → read → patch → insights → delete against the
  real Postgres, using a uniquely tagged throwaway vendor and guaranteed cleanup
  in `finally`, so nothing lingers. Skipped automatically if the DB is
  unreachable.

Run directly:

    python tests/test_bills.py

Network-bound paths (NuExtract HTTP, Maton Gmail/Drive scans, auto-pay) are NOT
exercised here — they need live third-party credentials.
"""
from __future__ import annotations

import os
import sys
import uuid

# Make the project root importable when run as `python tests/test_bills.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from hermes import bills as B  # noqa: E402


# ── Offline: NuExtract normalisation ─────────────────────────────────────────

def test_normalise_full_response():
    raw = {
        "result": {
            "vendor_name": "  Amazon Web Services  ",
            "invoice_number": "INV-2024-99",
            "issue_date": "01/15/2024",
            "due_date": "2024-02-14",
            "total_amount": "$1,234.56",
            "currency": "usd",
            "category": "cloud",
            "line_items": [
                {"description": " EC2 ", "quantity": "2", "unit_price": "10.00", "total": "20.00"},
                "not-a-dict",  # must be skipped, not crash
            ],
        }
    }
    out = B._normalise(raw, "aws-jan.pdf")
    assert out["vendor"] == "Amazon Web Services", out["vendor"]
    assert out["amount"] == 1234.56, out["amount"]
    assert out["currency"] == "USD", out["currency"]
    assert out["invoice_number"] == "INV-2024-99"
    assert out["issue_date"] == "2024-01-15", out["issue_date"]
    assert out["due_date"] == "2024-02-14"
    assert len(out["line_items"]) == 1
    assert out["line_items"][0] == {
        "description": "EC2", "quantity": 2.0, "unit_price": 10.0, "total": 20.0,
    }
    assert out["raw_extracted"] is raw  # original preserved


def test_normalise_vendor_falls_back_to_filename():
    out = B._normalise({}, "acme_power-co.pdf")
    # No vendor_name -> derive a title-cased name from the filename stem.
    assert out["vendor"] == "Acme Power Co", out["vendor"]
    assert out["amount"] is None
    assert out["currency"] == "USD"          # default
    assert out["line_items"] == []


def test_normalise_currency_truncated_to_three_chars():
    out = B._normalise({"currency": "dollars"}, "x.pdf")
    assert out["currency"] == "DOL", out["currency"]


# ── Offline: filename date extraction ────────────────────────────────────────

def test_extract_date_dd_mm_yyyy():
    assert B._extract_date_from_filename("Sysco 09 04 2023 invoice.pdf") == "2023-04-09"


def test_extract_date_bare_year_fallback():
    assert B._extract_date_from_filename("Utility bill 2022.pdf") == "2022-01-01"


def test_extract_date_invalid_day_month_ignored():
    # 45 is not a valid day → the DD MM YYYY branch fails, bare-year fallback wins.
    assert B._extract_date_from_filename("weird 45 99 2021.pdf") == "2021-01-01"


def test_extract_date_none_when_absent():
    assert B._extract_date_from_filename("just-a-name.pdf") is None
    assert B._extract_date_from_filename("") is None


# ── Offline: filename vendor cleanup ─────────────────────────────────────────

def test_vendor_from_filename_strips_extension_and_keywords():
    # ".pdf" and the trailing "ENERGY BILL" keyword phrase are stripped, leaving
    # the core brand. A plain "BILL" suffix leaves the descriptor intact.
    assert B._vendor_from_filename("comcast-energy-bill.pdf") == "Comcast"
    assert B._vendor_from_filename("pacific-gas-electric-bill.pdf") == "Pacific Gas Electric"


def test_vendor_from_filename_strips_trailing_date():
    assert B._vendor_from_filename("Sysco 09 04 2023.pdf") == "Sysco"


def test_vendor_from_filename_non_empty():
    # Whatever the heuristics do, they must never return an empty string.
    for name in ["invoice.pdf", "R12 GIF ACME INVOICE 9f3a2b.pdf",
                 "CS INV ARC Comcast INVOICE 2024.pdf", "x.png"]:
        v = B._vendor_from_filename(name)
        assert isinstance(v, str) and v.strip(), f"empty vendor for {name!r}"


# ── Offline: scalar coercion ─────────────────────────────────────────────────

def test_parse_float_variants():
    assert B._parse_float("$1,299.00") == 1299.0
    assert B._parse_float("42") == 42.0
    assert B._parse_float(None) is None
    assert B._parse_float("n/a") is None


def test_normalise_date_variants():
    assert B._normalise_date("2024-03-01") == "2024-03-01"
    assert B._normalise_date("03/01/2024") == "2024-03-01"
    assert B._normalise_date("March 1, 2024") == "2024-03-01"
    assert B._normalise_date(None) is None
    # Unparseable string is returned as-is (best effort), not dropped.
    assert B._normalise_date("sometime next week") == "sometime next week"


# ── Offline: business routing ────────────────────────────────────────────────

_BUSINESSES = [
    {"business_id": "b-tech", "slug": "tech", "industry": "SaaS / Tech"},
    {"business_id": "b-farm", "slug": "afk", "industry": "Agriculture"},
    {"business_id": "b-food", "slug": "nh", "industry": "Food & Bev"},
]


def test_guess_business_keyword_match_high_confidence():
    bid, conf = B.guess_business("AWS Cloud Services", "cloud hosting", _BUSINESSES)
    assert bid == "b-tech"
    assert conf == 0.88, conf   # single industry match


def test_guess_business_by_category_keyword():
    bid, conf = B.guess_business("John Deere", "tractor lease", _BUSINESSES)
    assert bid == "b-farm"
    assert conf == 0.88


def test_guess_business_no_keyword_low_confidence():
    bid, conf = B.guess_business("Random Vendor Ltd", None, _BUSINESSES)
    assert bid is None
    assert conf == 0.4, conf


def test_guess_business_matched_industry_absent_from_list():
    # "meta ads" → Media, but no Media business exists → unassigned, low conf.
    bid, conf = B.guess_business("Meta Ads", "ad campaign", _BUSINESSES)
    assert bid is None
    assert conf == 0.4


def test_guess_business_empty_list():
    assert B.guess_business("Anything", None, []) == (None, 0.0)


# ── Live DB round-trip (insert → read → patch → insights → delete) ───────────

def _db_available() -> bool:
    try:
        from hermes.db.connection import ping
        ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_bill_crud_round_trip():
    if not _db_available():
        print("SKIP  test_bill_crud_round_trip (DB unreachable)")
        return

    from hermes.db.bills import (
        insert_bill, get_bill, list_bills, update_bill, delete_bill,
        compute_insights, _BILL_NOT_FOUND,
    )

    marker = f"__pytest_bill_{uuid.uuid4().hex[:8]}"
    bill_id = None
    try:
        bill_id = insert_bill(
            vendor=marker,
            amount=100.0,
            invoice_number="TEST-1",
            due_date="2024-01-01",
            status="pending",
            source="upload",
            currency="USD",
            line_items=[{"description": "widget", "quantity": 2, "unit_price": 25, "total": 50}],
        )
        assert bill_id, "insert_bill returned no id"

        # get_bill returns the row with serialised (JSON-friendly) fields.
        row = get_bill(bill_id)
        assert row is not None
        assert row["vendor"] == marker
        assert row["amount"] == 100.0
        assert row["status"] == "pending"
        assert isinstance(row["due_date"], str)  # date serialised to ISO string

        # list_bills(status=...) includes it and attaches line items.
        listed = list_bills(status="pending", limit=500)
        mine = next((b for b in listed if b["bill_id"] == bill_id), None)
        assert mine is not None, "inserted bill missing from list_bills"
        assert len(mine["line_items"]) == 1
        assert mine["line_items"][0]["description"] == "widget"

        # patch: mark overdue and rename.
        assert update_bill(bill_id, status="overdue", vendor=marker + "_x") is True
        assert get_bill(bill_id)["status"] == "overdue"

        # insights should now surface it under "overdue" with the amount rendered.
        ins = compute_insights()
        assert any(bill_id == o["bill_id"] for o in ins["overdue"]), "not in overdue insights"

        # delete returns the (empty) file_url, and the row is gone afterwards.
        res = delete_bill(bill_id)
        assert res is not _BILL_NOT_FOUND
        assert get_bill(bill_id) is None
        # Deleting again reports not-found.
        assert delete_bill(bill_id) is _BILL_NOT_FOUND
        bill_id = None  # cleaned up
    finally:
        # Belt-and-suspenders: remove the row even if an assertion above failed.
        if bill_id:
            try:
                delete_bill(bill_id)
            except Exception:  # noqa: BLE001
                pass


def test_list_and_insights_read_only():
    """Bills endpoints' backing reads work against the live schema."""
    if not _db_available():
        print("SKIP  test_list_and_insights_read_only (DB unreachable)")
        return
    from hermes.db.bills import list_bills, compute_insights, list_businesses

    assert isinstance(list_businesses(), list)
    assert isinstance(list_bills(limit=5), list)
    ins = compute_insights()
    assert set(ins.keys()) >= {"overdue", "upcoming", "cashflow", "suggestions"}


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
