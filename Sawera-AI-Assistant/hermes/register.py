"""The subscriptions & bills register — read from the Google Sheet that owns it.

The Sheet is the SOURCE OF TRUTH for recurring bills, not this app. It carries
things Hermes' own `bills` table has no concept of: recurrence, monthly
run-rate, which card is charged, an operational status vocabulary (FAILING,
DECLINED, AUTO-PAY SCHEDULED, …), a per-line evidence trail, and a change log.
Hermes reads it and never authors a line.

What Hermes adds is the one thing the Sheet cannot do for itself: it holds the
original PDFs and can read them. Around 45 register lines record
"AMOUNT IN PDF - UNVERIFIED" — an amount that exists only inside an attachment
nobody opened. hermes/verify_amounts.py resolves those against documents already
in S3 and files a PROPOSAL; a human approves before anything is written back.

The Sheet's own rule — nothing is estimated, an unverifiable amount stays
unverified — is preserved end to end: a proposal always carries the evidence it
came from, and an approval writes a CHANGE LOG row recording what changed.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from .config import Config
from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.register")


class RegisterError(RuntimeError):
    """The register could not be read or written."""


# Tab layout. Header rows are fixed by the Sheet's own design (title, subtitle,
# blank, header, then data), so the data range starts at row 5.
REGISTER_TAB = "Register"
CHANGE_LOG_TAB = "CHANGE LOG"
DEADLINES_TAB = "DEADLINES"
_FIRST_DATA_ROW = 5

_REGISTER_COLS = [
    "ref", "vendor", "what_it_is", "entity", "billing_basis", "amount_usd",
    "monthly_run_rate", "currency_as_billed", "card", "status",
    "last_evidence_date", "source_mailbox", "notes",
]
_AMOUNT_COL_LETTER = "F"        # "Amount (USD)"

_DEADLINE_COLS = [
    "ref", "vendor", "obligation", "due_date", "recurrence", "amount_usd",
    "currency_as_billed", "kind", "status", "entity", "source_mailbox",
    "last_evidence_date", "notes",
]

# The Sheet's marker for "this amount is real but locked in a PDF we haven't
# opened". Matching is loose because the wording varies slightly by row.
UNVERIFIED_MARKER = "UNVERIFIED"


def _client() -> MatonClient:
    """Maton client for the account that can see the register spreadsheet."""
    slug = Config.REGISTER_SHEET_BUSINESS
    key = Config.maton_key_for_slug(slug)
    if not key:
        raise RegisterError(
            f"No Maton connection for '{slug}' — set REGISTER_SHEET_BUSINESS to a "
            f"business whose Google account can open the register spreadsheet."
        )
    return MatonClient(api_key=key)


def _sheet_id() -> str:
    sid = Config.REGISTER_SHEET_ID
    if not sid:
        raise RegisterError("REGISTER_SHEET_ID is not configured.")
    return sid


def _values(rng: str) -> list[list[str]]:
    client = _client()
    try:
        resp = client.request(
            "google-sheets", "GET", f"v4/spreadsheets/{_sheet_id()}/values/{rng}",
        )
    except MatonError as exc:
        raise RegisterError(f"Could not reach the register sheet: {exc}") from exc
    if not resp.get("ok"):
        data = resp.get("data")
        detail = data.get("message") if isinstance(data, dict) else data
        raise RegisterError(f"Register sheet read failed ({resp.get('status')}): {detail}")
    return ((resp.get("data") or {}).get("values") or [])


def _row_to_dict(row: list[str], cols: list[str], row_number: int) -> dict[str, Any]:
    # The Sheets API truncates trailing empty cells, so short rows are normal.
    padded = list(row) + [""] * (len(cols) - len(row))
    out: dict[str, Any] = {c: (padded[i] or "").strip() for i, c in enumerate(cols)}
    out["row_number"] = row_number
    return out


def _is_unverified(value: str) -> bool:
    return UNVERIFIED_MARKER in (value or "").upper()


def parse_money(value: str) -> float | None:
    """A register amount as a number, or None when it is not a plain figure.

    Deliberately returns None for "AMOUNT IN PDF - UNVERIFIED" and "n/a" rather
    than coercing them to 0 — a zero would silently understate a total.
    """
    s = (value or "").strip()
    if not s or _is_unverified(s) or s.lower() in {"n/a", "na", "-", "—", "tbd"}:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    m = re.match(r"^-?\d+(\.\d+)?$", s)
    return float(s) if m else None


def fetch_register() -> list[dict[str, Any]]:
    """Every numbered line of the Register tab, in sheet order."""
    rows = _values(f"{REGISTER_TAB}!A{_FIRST_DATA_ROW}:M2000")
    out: list[dict[str, Any]] = []
    for offset, row in enumerate(rows):
        line = _row_to_dict(row, _REGISTER_COLS, _FIRST_DATA_ROW + offset)
        # Rows below the register (totals, notes) have no numeric Ref#.
        if not re.match(r"^\d+$", line["ref"]):
            continue
        line["amount_value"] = parse_money(line["amount_usd"])
        line["run_rate_value"] = parse_money(line["monthly_run_rate"])
        line["amount_unverified"] = _is_unverified(line["amount_usd"])
        out.append(line)
    return out


def fetch_deadlines() -> list[dict[str, Any]]:
    rows = _values(f"{DEADLINES_TAB}!A{_FIRST_DATA_ROW}:M500")
    out = []
    for offset, row in enumerate(rows):
        line = _row_to_dict(row, _DEADLINE_COLS, _FIRST_DATA_ROW + offset)
        if not re.match(r"^\d+$", line["ref"]):
            continue
        line["amount_value"] = parse_money(line["amount_usd"])
        out.append(line)
    return out


def _append(tab: str, rows: list[list[str]]) -> None:
    client = _client()
    try:
        resp = client.request(
            "google-sheets", "POST",
            f"v4/spreadsheets/{_sheet_id()}/values/{tab}!A1:append",
            query={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            body={"values": rows},
        )
    except MatonError as exc:
        raise RegisterError(f"Could not write to '{tab}': {exc}") from exc
    if not resp.get("ok"):
        data = resp.get("data")
        detail = data.get("message") if isinstance(data, dict) else data
        raise RegisterError(f"Append to '{tab}' failed ({resp.get('status')}): {detail}")


def append_change_log(
    ref: str, field: str, old: str, new: str, evidence: str,
    when: date | None = None,
) -> None:
    """Record one change, in the Sheet's own CHANGE LOG format.

    Columns: Date | Ref# | Field changed | From | To | Evidence.
    Every write Hermes makes to the register leaves one of these behind, so the
    Sheet's audit trail stays complete whether a human or this app made the edit.
    """
    _append(CHANGE_LOG_TAB, [[
        (when or date.today()).isoformat(), str(ref), field,
        old or "—", new or "—", evidence,
    ]])


def write_amount(row_number: int, amount: str) -> None:
    """Set the Amount (USD) cell on one register row.

    Only ever called after a human approves a proposal — see
    app.py::api_register_approve. Pair it with append_change_log().
    """
    client = _client()
    rng = f"{REGISTER_TAB}!{_AMOUNT_COL_LETTER}{int(row_number)}"
    try:
        resp = client.request(
            "google-sheets", "PUT", f"v4/spreadsheets/{_sheet_id()}/values/{rng}",
            query={"valueInputOption": "USER_ENTERED"},
            body={"range": rng, "majorDimension": "ROWS", "values": [[amount]]},
        )
    except MatonError as exc:
        raise RegisterError(f"Could not update {rng}: {exc}") from exc
    if not resp.get("ok"):
        data = resp.get("data")
        detail = data.get("message") if isinstance(data, dict) else data
        raise RegisterError(f"Register update failed ({resp.get('status')}): {detail}")


def summarise(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline figures, computed the way the Sheet computes them: verified
    amounts only, with unverified lines counted but never guessed at."""
    verified_run_rate = sum(
        l["run_rate_value"] for l in lines if l.get("run_rate_value") is not None
    )
    by_status: dict[str, int] = {}
    by_entity: dict[str, int] = {}
    for l in lines:
        by_status[l["status"] or "—"] = by_status.get(l["status"] or "—", 0) + 1
        by_entity[l["entity"] or "—"] = by_entity.get(l["entity"] or "—", 0) + 1
    return {
        "lines": len(lines),
        "verified_monthly_run_rate": round(verified_run_rate, 2),
        "unverified_amounts": sum(1 for l in lines if l.get("amount_unverified")),
        "by_status": by_status,
        "by_entity": by_entity,
    }
