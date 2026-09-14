"""The register integration, and the judgement that keeps it trustworthy.

The Google Sheet is the source of truth for recurring bills, and its defining
rule is that nothing is estimated: an amount that exists only inside an unopened
PDF is recorded as UNVERIFIED rather than guessed. Hermes reads that register
and offers figures it extracted from documents it already holds.

That makes the failure mode worth guarding: a confident wrong number, dressed as
evidence, is far more damaging than no number. A first pass proposed one Google
Workspace invoice as the amount for THREE different Workspace subscriptions, and
offered an OCR misread of a photo as a real figure. These tests pin the rules
that stop both.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes import register as reg  # noqa: E402
from hermes import verify_amounts as V  # noqa: E402


# ---- reading amounts: never turn "unknown" into a number ----

@pytest.mark.parametrize("raw,expected", [
    ("$175.00", 175.0),
    ("175", 175.0),
    ("$1,374.54", 1374.54),
    ("2456", 2456.0),
])
def test_plain_amounts_parse(raw, expected):
    assert reg.parse_money(raw) == expected


@pytest.mark.parametrize("raw", [
    "AMOUNT IN PDF - UNVERIFIED",
    "AMOUNT IN PDF — UNVERIFIED",
    "n/a", "N/A", "", "   ", "—", "TBD",
    "KES 6,501.00",     # a figure, but not in USD — not the USD column's value
])
def test_unknown_amounts_are_none_never_zero(raw):
    """Coercing UNVERIFIED to 0 would silently understate every total."""
    assert reg.parse_money(raw) is None


def test_summary_excludes_unverified_from_the_run_rate():
    lines = [
        {"status": "ACTIVE", "entity": "AFK",
         "run_rate_value": 175.0, "amount_unverified": False},
        {"status": "FAILING", "entity": "AFK",
         "run_rate_value": None, "amount_unverified": True},
        {"status": "ACTIVE", "entity": "Personal",
         "run_rate_value": 49.0, "amount_unverified": False},
    ]
    s = reg.summarise(lines)
    assert s["verified_monthly_run_rate"] == 224.0     # the unverified line adds nothing
    assert s["unverified_amounts"] == 1
    assert s["by_entity"]["AFK"] == 2


# ---- vendor matching ----

@pytest.mark.parametrize("a,b,floor", [
    ("Starlink", "Starlink", 1.0),
    ("Google Workspace", "Google Payments", 0.4),
    ("Hostinger", "Hostinger International Ltd", 0.9),
])
def test_related_vendors_score(a, b, floor):
    assert V.match_score(a, b) >= floor


@pytest.mark.parametrize("a,b", [
    ("Starlink", "Xcel Energy"),
    ("Notion", "Verizon Wireless"),
    ("Acme Inc", "Globex LLC"),      # only stopwords in common
])
def test_unrelated_vendors_do_not_match(a, b):
    assert V.match_score(a, b) < V.MIN_MATCH_SCORE


def test_corporate_suffixes_alone_never_match():
    """'Inc' and 'LLC' are not identity."""
    assert V.match_score("Something Inc", "Different LLC Inc") == 0.0


# ---- discriminators: vendor name is not identity ----

def test_discriminators_pick_up_the_identifying_details():
    line = {"what_it_is": "Enterprise Plus - afrofarmke.com", "source_mailbox": "admin@afrofarmke.com"}
    d = V.discriminators(line)
    assert "afrofarmke.com" in d
    assert "enterprise" in d and "plus" in d


def test_discriminators_separate_two_lines_of_the_same_vendor():
    """The exact case that produced three identical wrong proposals."""
    a = V.discriminators({"what_it_is": "Enterprise Plus - afrofarmke.com", "source_mailbox": ""})
    b = V.discriminators({"what_it_is": "Subscription - gandikediye", "source_mailbox": ""})
    assert a != b
    assert not (a & b)


def test_invoice_keyword_is_not_mistaken_for_an_identifier():
    """'invoice' used to be split into 'inv' + 'oice' and 'oice' offered as an
    identifying token, which made a garbage match look corroborated."""
    assert "oice" not in V.discriminators({"what_it_is": "invoice", "source_mailbox": ""})


def test_account_numbers_are_discriminators():
    d = V.discriminators({"what_it_is": "Utility bill, acct 730656302", "source_mailbox": ""})
    assert "730656302" in d


# ---- refusing to read noise as evidence ----

def test_ocr_noise_is_not_accepted_as_an_amount():
    """A photo that did not OCR cleanly produced descriptions like
    'MΣpΣTpΞ M3HĐáŸ s I t W' alongside a number. That number is not evidence."""
    assert V.extraction_looks_sane({
        "amount": 727.0,
        "line_items": [
            {"description": "MΣpΣTpΞ M3HĐáŸ s I t W", "total": None},
            {"description": "¤‡×Ø¶ }{ |~", "total": None},
        ],
    }) is False


def test_a_clean_extraction_is_accepted():
    assert V.extraction_looks_sane({
        "amount": 6501.0,
        "line_items": [{"description": "Residential (Aug 28 - Sep 28) HOUSE KIT401069676",
                        "total": 6501.0}],
    }) is True


@pytest.mark.parametrize("amount", [None, 0, -5, 99_000_000])
def test_impossible_amounts_are_rejected(amount):
    assert V.extraction_looks_sane({"amount": amount, "line_items": []}) is False


def test_an_extraction_with_no_line_items_is_judged_on_the_amount_alone():
    assert V.extraction_looks_sane({"amount": 49.0, "line_items": []}) is True


# ---- corroboration against the register's own figure ----

def test_matching_the_registers_billed_currency_counts_as_corroboration():
    """The register independently recorded 'KES 6,501.00'; reading 6501 out of
    the PDF means a human and the extractor agree."""
    line = {"currency_as_billed": "KES 6,501.00"}
    assert V.corroborates_register(line, {"amount": 6501.0}) is True


def test_a_different_figure_does_not_corroborate():
    line = {"currency_as_billed": "KES 6,501.00"}
    assert V.corroborates_register(line, {"amount": 412.30}) is False


def test_no_billed_figure_means_no_corroboration():
    assert V.corroborates_register({"currency_as_billed": "USD"}, {"amount": 10.0}) is False


# ---- confidence ----

def _line(**over):
    base = {"ref": "4", "vendor": "Google Workspace", "entity": "AFK",
            "what_it_is": "Enterprise Plus - afrofarmke.com",
            "source_mailbox": "", "currency_as_billed": "USD"}
    base.update(over)
    return base


def test_a_document_naming_the_plan_clears_the_bar():
    doc = {"score": 1.0}
    extracted = {"vendor": "Google", "invoice_number": "5644914697", "amount": 1374.54,
                 "line_items": [{"description": "Google Workspace Enterprise Plus Usage Jul 1 - Jul 5"}]}
    confidence, reasons = V.score_pair(_line(), doc, extracted)
    assert confidence >= V.MIN_CONFIDENCE
    assert any("identifiers in document" in r for r in reasons)


def test_a_sibling_subscription_does_not_clear_the_bar_on_the_same_document():
    """Ref#5 is a different Workspace subscription; the Enterprise Plus invoice
    says nothing about it, so it must fall short of a proposal."""
    doc = {"score": 0.72}
    extracted = {"vendor": "Google", "invoice_number": "5644914697", "amount": 1374.54,
                 "line_items": [{"description": "Google Workspace Enterprise Plus Usage Jul 1 - Jul 5"}]}
    line = _line(ref="5", what_it_is="Subscription - gandikediye")
    confidence, reasons = V.score_pair(line, doc, extracted)
    assert confidence < V.MIN_CONFIDENCE
    assert any("no identifier" in r for r in reasons)


def test_one_document_cannot_evidence_two_register_lines():
    """A receipt is spent once. The second claimant is reported, not handed the
    same figure."""
    docs = [{
        "bill_id": "b1", "vendor": "Google Workspace", "file_url": "s3://b/k.pdf",
        "source": "gmail", "source_ref": "m1", "business_id": None, "slug": "afk",
        "created_at": "2026-09-01",
    }]
    extracted = {"vendor": "Google", "amount": 1374.54,
                 "line_items": [{"description": "Enterprise Plus", "total": 1374.54}]}
    cache = {"s3://b/k.pdf": extracted}
    claimed = {"s3://b/k.pdf": "4"}        # already assigned to Ref#4

    result = V.verify_line(_line(ref="6", what_it_is="Subscription - afrofarm001"),
                           docs, cache, claimed)
    assert result["outcome"] in {"needs_review", "ambiguous"}
    assert any("already the evidence for Ref#4" in r for r in result.get("reasons", []))


def test_a_line_with_no_candidate_document_says_so():
    result = V.verify_line(_line(vendor="Totally Unrelated Vendor"), [], {}, {})
    assert result["outcome"] == "no_document"
