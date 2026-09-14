"""Billing feature regressions.

Every test here pins a defect that was live in the bills feature:

* 'overdue' and 'due-soon' were read all over the UI but never written by
  anything, so those tiles, that filter and the overdue insight group were
  permanently empty.
* An unparseable date was passed straight through to a Postgres DATE column,
  which threw away the entire bill — including the fields we HAD read.
* Money was parsed by stripping everything but digits and dots, so European
  formatting and credit notes came out wrong by orders of magnitude.
* update_bill smuggled a SQL expression through as the literal string "now()"
  and filtered values by comparing against it, so a bill whose notes were
  exactly "now()" had those notes replaced by a SQL call.
* compute_insights composed HTML from vendor names that come out of email
  From headers, and the frontend dropped that into innerHTML.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes.bills import (  # noqa: E402
    _maton_data,
    _normalise,
    _normalise_date,
    _parse_float,
    _parse_rfc2822_date,
    _payment_keyword,
    _vendor_from_sender,
    _vendor_from_subject,
)
from hermes.db.bills import (  # noqa: E402
    BillUpdateError,
    compute_insights,
    derive_status,
    update_bill,
)

TODAY = date(2026, 9, 14)


# ---- a bill must be able to become overdue ----

@pytest.mark.parametrize("due,expected", [
    ("2026-09-13", "overdue"),      # yesterday
    ("2026-01-01", "overdue"),      # long past
    ("2026-09-14", "due-soon"),     # today
    ("2026-09-21", "due-soon"),     # edge of the 7-day window
    ("2026-09-22", "pending"),      # just outside it
    (None,         "pending"),      # no due date to judge by
])
def test_pending_bill_ages_into_overdue_and_due_soon(due, expected):
    assert derive_status("pending", due, TODAY) == expected


@pytest.mark.parametrize("terminal", ["paid", "processing"])
def test_terminal_status_is_never_overridden_by_a_due_date(terminal):
    """A paid invoice with a past due date is paid, not overdue."""
    assert derive_status(terminal, "2020-01-01", TODAY) == terminal


def test_legacy_stored_derived_status_without_a_due_date_falls_back():
    """Rows written before the derivation may still carry a stale 'overdue'."""
    assert derive_status("overdue", None, TODAY) == "pending"


def test_derive_status_accepts_a_date_object_not_just_a_string():
    assert derive_status("pending", date(2026, 9, 1), TODAY) == "overdue"


def test_derive_status_survives_a_junk_due_date():
    assert derive_status("pending", "Net 30", TODAY) == "pending"


# ---- an unreadable date must not destroy the whole bill ----

@pytest.mark.parametrize("value", ["Net 30", "upon receipt", "TBD", "", "  ", "13/45/2026"])
def test_unparseable_date_becomes_none_not_free_text(value):
    assert _normalise_date(value) is None


@pytest.mark.parametrize("value,expected", [
    ("2026-09-14",      "2026-09-14"),
    ("14/09/2026",      "2026-09-14"),
    ("09/14/2026",      "2026-09-14"),   # unambiguous: no 14th month
    ("14.09.2026",      "2026-09-14"),
    ("September 14, 2026", "2026-09-14"),
    ("14 Sep 2026",     "2026-09-14"),
])
def test_recognised_date_formats_normalise_to_iso(value, expected):
    assert _normalise_date(value) == expected


def test_bill_with_a_junk_due_date_still_keeps_its_other_fields():
    """The regression: one bad field used to cost the entire extraction."""
    bill = _normalise({"result": {
        "vendor_name": "Acme Corp",
        "total_amount": "1,240.00",
        "due_date": "Net 30",
        "invoice_number": "INV-9001",
    }}, "acme.pdf")
    assert bill["due_date"] is None
    assert bill["vendor"] == "Acme Corp"
    assert bill["amount"] == 1240.0
    assert bill["invoice_number"] == "INV-9001"


# ---- money ----

@pytest.mark.parametrize("raw,expected", [
    ("1234",            1234.0),
    ("1,234.56",        1234.56),    # US thousands separator
    ("1.234,56",        1234.56),    # European — used to parse as 1.234
    ("$1,234.56",       1234.56),
    ("€1.234,56",       1234.56),
    ("1 234,56",        1234.56),    # space as thousands separator
    ("-500.00",         -500.0),     # credit note — the sign used to be dropped
    ("(1,200.00)",      -1200.0),    # accounting negative
    ("USD 99.99",       99.99),
    ("99",              99.0),
    ("",                None),
    ("n/a",             None),
    (None,              None),
    (1234.5,            1234.5),
])
def test_money_parsing(raw, expected):
    assert _parse_float(raw) == expected


# ---- update_bill ----

def test_a_field_whose_value_is_literally_now_is_not_treated_as_sql(monkeypatch):
    """The "now()" sentinel used to swallow any value equal to that string."""
    captured = {}

    class _Cur:
        rowcount = 1
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

    class _Ctx:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr("hermes.db.bills.cursor", lambda *a, **k: _Ctx())

    assert update_bill("bill-1", notes="now()") is True
    assert "notes = %s" in captured["sql"]
    assert "updated_at = now()" in captured["sql"]
    # The literal string must be bound as a parameter, not inlined as SQL.
    assert captured["params"] == ["now()", "bill-1"]


def test_update_bill_rejects_an_unknown_status():
    with pytest.raises(BillUpdateError):
        update_bill("bill-1", status="banana")


def test_update_bill_rejects_a_malformed_date():
    with pytest.raises(BillUpdateError):
        update_bill("bill-1", due_date="whenever")


def test_update_bill_collapses_a_derived_status_to_its_stored_form(monkeypatch):
    """'overdue' is computed from due_date; persisting it would go stale."""
    captured = {}

    class _Cur:
        rowcount = 1
        def execute(self, sql, params):
            captured["params"] = params

    class _Ctx:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    monkeypatch.setattr("hermes.db.bills.cursor", lambda *a, **k: _Ctx())
    update_bill("bill-1", status="overdue")
    assert captured["params"] == ["pending", "bill-1"]


def test_update_bill_ignores_fields_it_does_not_own():
    assert update_bill("bill-1", created_at="x", source="gmail", raw_extracted={}) is False


# ---- insights carry data, not markup ----

def _bill(**over):
    base = {
        "bill_id": "b1", "vendor": "Acme", "amount": 100.0, "currency": "USD",
        "status": "overdue", "due_date": "2026-09-01", "business_id": "biz1",
        "business_name": "Acme Holdings", "business_color": "#ff0000",
    }
    base.update(over)
    return base


def test_insights_never_return_html():
    """vendor comes from an email From header; it must not reach innerHTML
    pre-composed into markup."""
    hostile = '<img src=x onerror="alert(1)">'
    out = compute_insights([_bill(vendor=hostile)])
    row = out["overdue"][0]
    assert row["vendor"] == hostile          # reported verbatim as data
    assert "text" not in row                 # no pre-built markup field
    assert not any(
        "<" in str(v) for k, v in row.items() if k not in ("vendor", "business")
    )


def test_insights_report_the_bill_currency():
    out = compute_insights([_bill(currency="EUR")])
    assert out["overdue"][0]["currency"] == "EUR"


def test_cashflow_flags_a_business_mixing_currencies():
    out = compute_insights([
        _bill(bill_id="b1", currency="USD", status="pending", due_date=None),
        _bill(bill_id="b2", currency="EUR", status="pending", due_date=None),
    ])
    bar = out["cashflow"][0]
    assert bar["currency"] is None                    # not safely one symbol
    assert bar["currencies"] == ["EUR", "USD"]


def test_cashflow_excludes_paid_bills():
    out = compute_insights([_bill(status="paid")])
    assert out["cashflow"] == []


def test_insights_on_an_empty_list():
    assert compute_insights([]) == {
        "overdue": [], "upcoming": [], "cashflow": [], "suggestions": [],
    }


# ---- Gmail scan helpers ----

def test_email_date_header_becomes_an_issue_date():
    """Without this, every Gmail bill had no date at all and could never age."""
    assert _parse_rfc2822_date("Mon, 14 Sep 2026 09:30:00 -0500") == "2026-09-14"


@pytest.mark.parametrize("bad", ["", None, "not a date"])
def test_bad_email_date_header_is_ignored(bad):
    assert _parse_rfc2822_date(bad) is None


# ---- a Maton error body is not message data ----

def test_a_failed_maton_call_raises_instead_of_yielding_empty_data():
    """The exact production response behind the 48 nameless July bills.

    MatonClient.request() returns ok=False rather than raising, so the error
    body used to be read as if it were a Gmail message: no payload, no headers,
    vendor "".
    """
    from hermes.maton import MatonError

    disconnected = {
        "status": 400, "ok": False,
        "data": {"code": 400, "type": "Bad Request",
                 "message": "Your connections for `google-mail` are either PENDING or FAILED."},
    }
    with pytest.raises(MatonError) as exc:
        _maton_data(disconnected, "Gmail search")
    assert "PENDING or FAILED" in str(exc.value)


def test_the_old_unchecked_read_would_have_produced_an_empty_vendor():
    """Pins why the guard is needed, using the same error envelope."""
    error_body = {"code": 400, "message": "…PENDING or FAILED…"}
    payload = (error_body or {}).get("payload") or {}      # the old line
    headers = {h["name"].lower(): h["value"] for h in (payload.get("headers") or [])}
    assert headers.get("from", "") == ""
    assert _vendor_from_sender(headers.get("from", "")) is None


def test_a_successful_call_passes_its_data_through():
    ok = {"status": 200, "ok": True, "data": {"messages": [{"id": "abc"}]}}
    assert _maton_data(ok, "Gmail search")["messages"] == [{"id": "abc"}]


def test_a_non_dict_body_becomes_an_empty_dict_not_a_crash():
    assert _maton_data({"status": 200, "ok": True, "data": "plain text"}, "x") == {}


@pytest.mark.parametrize("sender,expected", [
    ("Acme Billing <billing@acme.com>", "Acme Billing"),
    ('"Acme, Inc." <ar@acme.com>',      "Acme, Inc."),
    ("<billing@acme-corp.com>",         "Acme Corp"),   # no display name
    ("billing@acme.com",                "billing@acme.com"),
    ("",                                None),          # the July failure mode
    ("   ",                             None),
])
def test_vendor_name_from_a_from_header(sender, expected):
    assert _vendor_from_sender(sender) == expected


def test_a_message_with_no_headers_yields_no_vendor():
    """49 production bills were filed with an empty vendor this way."""
    assert _vendor_from_sender("") is None
    assert _vendor_from_subject("") is None
    assert _vendor_from_subject("   \n ") is None


def test_subject_is_the_last_resort_vendor_name():
    assert _vendor_from_subject("  Invoice   4821  from  Acme ") == "Invoice 4821 from Acme"
    assert len(_vendor_from_subject("x" * 500)) == 80


@pytest.mark.parametrize("vendor,expected", [
    ("Blue Ridge Energy", "Blue"),
    ("Acme Inc",          "Acme"),
    ("The Company",       None),    # nothing identifying left after stopwords
    ("Billing Services",  None),
    ("",                  None),
])
def test_payment_search_keyword_skips_generic_vendor_words(vendor, expected):
    assert _payment_keyword(vendor) == expected
