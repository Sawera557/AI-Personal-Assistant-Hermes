"""DB operations for bills, businesses, and line items."""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.bills")


# ── Serialisation helpers ────────────────────────────────────────────────────

def _row(cur, cols: list[str]) -> list[dict]:
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _serial(obj: Any) -> Any:
    """Make Postgres types JSON-friendly."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return obj


def _clean(d: dict) -> dict:
    return {k: _serial(v) for k, v in d.items()}


# ── Businesses ───────────────────────────────────────────────────────────────

# list_businesses() is called on nearly every request (chat, meeting copilot,
# bills) just to resolve a slug -> display name, and the 8-ish rows behind it
# change essentially never. A synchronous Postgres round trip on that hot path
# was pure added latency, so cache it for a few seconds and invalidate on write.
_businesses_cache: list[dict] | None = None
_businesses_cache_at: float = 0.0
_BUSINESSES_CACHE_TTL = 30.0  # seconds


def list_businesses() -> list[dict]:
    global _businesses_cache, _businesses_cache_at
    now = time.monotonic()
    if _businesses_cache is not None and (now - _businesses_cache_at) < _BUSINESSES_CACHE_TTL:
        # Hand out a copy: callers that mutate rows (e.g. tagging `connected`)
        # must not poison the shared cache for every other request.
        return [dict(b) for b in _businesses_cache]
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT business_id, name, slug, emoji, color, industry, created_at "
                "FROM businesses ORDER BY name;"
            )
            result = [_clean(r) for r in _row(cur, [
                "business_id", "name", "slug", "emoji", "color", "industry", "created_at"
            ])]
    except Exception as exc:
        log.warning("list_businesses failed: %s", exc)
        return [dict(b) for b in (_businesses_cache or [])]
    _businesses_cache = result
    _businesses_cache_at = now
    return [dict(b) for b in result]


def upsert_business(
    name: str,
    slug: str,
    emoji: str = "🏢",
    color: str = "#9aa3b2",
    industry: str | None = None,
    business_id: str | None = None,
) -> dict:
    try:
        with cursor() as cur:
            if business_id:
                cur.execute(
                    """UPDATE businesses SET name=%s, slug=%s, emoji=%s, color=%s, industry=%s
                       WHERE business_id=%s RETURNING business_id, name, slug, emoji, color, industry;""",
                    (name, slug, emoji, color, industry, business_id),
                )
            else:
                cur.execute(
                    """INSERT INTO businesses (name, slug, emoji, color, industry)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (slug) DO UPDATE
                         SET name=EXCLUDED.name, emoji=EXCLUDED.emoji,
                             color=EXCLUDED.color, industry=EXCLUDED.industry
                       RETURNING business_id, name, slug, emoji, color, industry;""",
                    (name, slug, emoji, color, industry),
                )
            row = cur.fetchone()
            result = _clean(dict(zip(
                ["business_id", "name", "slug", "emoji", "color", "industry"], row
            )))
    except Exception as exc:
        log.error("upsert_business failed: %s", exc)
        raise
    global _businesses_cache
    _businesses_cache = None  # force a fresh read next call, picking up this change
    return result


# ── Bills ────────────────────────────────────────────────────────────────────

_BILL_COLS = [
    "bill_id", "business_id", "vendor", "amount", "invoice_number",
    "due_date", "issue_date", "currency", "status", "category",
    "confidence", "source", "source_ref", "file_url", "notes", "created_at", "updated_at",
]

# ── Status ───────────────────────────────────────────────────────────────────
#
# `bills.status` in Postgres only ever holds a state a human or the system
# explicitly chose: pending / paid / processing. 'overdue' and 'due-soon' are
# NOT stored — nothing in the codebase ever wrote them, which is why the
# Overdue and Due Soon tiles, the Overdue filter and the overdue insight group
# were permanently empty. They are a pure function of due_date and today, so we
# derive them on read instead of needing a cron to sweep the table nightly.

DUE_SOON_DAYS = 7          # "Due This Week" tile
TERMINAL_STATUSES = frozenset({"paid", "processing"})
STORED_STATUSES = frozenset({"pending", "paid", "processing"})
DERIVED_STATUSES = frozenset({"overdue", "due-soon"})
ALL_STATUSES = STORED_STATUSES | DERIVED_STATUSES

# Read-side ordering: most urgent first.
_STATUS_RANK = {"overdue": 0, "due-soon": 1, "processing": 2, "pending": 3, "paid": 4}

# Upper bound on rows pulled from Postgres before status derivation. The status
# filter can no longer run in SQL (the value being filtered on may not exist in
# the table), so filtering and LIMIT both happen in Python and the SQL side
# needs enough headroom to make that faithful.
_FETCH_CAP = 2000


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def derive_status(stored: str | None, due_date: Any, today: date | None = None) -> str:
    """Effective status of a bill = stored state, refined by its due date."""
    stored = (stored or "pending").strip()
    if stored in TERMINAL_STATUSES:
        return stored
    dd = _as_date(due_date)
    if dd is None:
        # A legacy row may still carry a stale 'overdue'/'due-soon' with no
        # due_date to justify it; fall back to the only defensible state.
        return "pending" if stored in DERIVED_STATUSES else stored
    days = (dd - (today or date.today())).days
    if days < 0:
        return "overdue"
    if days <= DUE_SOON_DAYS:
        return "due-soon"
    return "pending"


def _apply_derived_status(bills: list[dict], today: date | None = None) -> None:
    today = today or date.today()
    for b in bills:
        b["stored_status"] = b.get("status")
        b["status"] = derive_status(b.get("status"), b.get("due_date"), today)


def list_bills(
    business_id: str | None = None,
    status: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> list[dict]:
    try:
        with cursor(commit=False) as cur:
            where, params = ["1=1"], []
            if business_id:
                where.append("b.business_id = %s"); params.append(business_id)
            if source:
                where.append("b.source = %s"); params.append(source)
            cur.execute(
                f"""SELECT {', '.join('b.' + c for c in _BILL_COLS)},
                       bz.name AS business_name, bz.emoji AS business_emoji,
                       bz.color AS business_color
                    FROM bills b
                    LEFT JOIN businesses bz USING (business_id)
                    WHERE {' AND '.join(where)}
                    ORDER BY b.due_date ASC NULLS LAST, b.created_at DESC
                    LIMIT %s;""",
                params + [_FETCH_CAP],
            )
            cols = _BILL_COLS + ["business_name", "business_emoji", "business_color"]
            bills = [_clean(r) for r in _row(cur, cols)]

            _apply_derived_status(bills)
            if status:
                bills = [b for b in bills if b["status"] == status]
            bills.sort(key=lambda b: (
                _STATUS_RANK.get(b["status"], 5),
                _as_date(b.get("due_date")) or date.max,
            ))
            bills = bills[: max(1, limit)]

            # Attach line items
            if bills:
                ids = [b["bill_id"] for b in bills]
                cur.execute(
                    "SELECT bill_id, description, quantity, unit_price, total "
                    "FROM bill_line_items WHERE bill_id = ANY(%s::uuid[]);",
                    (ids,),
                )
                items_by_bill: dict[str, list] = {}
                for row in cur.fetchall():
                    bid = str(row[0])
                    items_by_bill.setdefault(bid, []).append({
                        "description": row[1],
                        "quantity": _serial(row[2]),
                        "unit_price": _serial(row[3]),
                        "total": _serial(row[4]),
                    })
                for b in bills:
                    b["line_items"] = items_by_bill.get(b["bill_id"], [])

            return bills
    except Exception as exc:
        log.warning("list_bills failed: %s", exc)
        return []


def get_bill(bill_id: str) -> dict | None:
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                f"""SELECT {', '.join('b.' + c for c in _BILL_COLS)},
                       bz.name AS business_name, bz.emoji AS business_emoji,
                       bz.color AS business_color
                    FROM bills b
                    LEFT JOIN businesses bz USING (business_id)
                    WHERE b.bill_id = %s;""",
                (bill_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = _BILL_COLS + ["business_name", "business_emoji", "business_color"]
            bill = _clean(dict(zip(cols, row)))
            _apply_derived_status([bill])
            return bill
    except Exception as exc:
        log.warning("get_bill failed: %s", exc)
        return None


def insert_bill(
    vendor: str,
    amount: float | None = None,
    invoice_number: str | None = None,
    due_date: str | None = None,
    issue_date: str | None = None,
    currency: str = "USD",
    status: str = "pending",
    category: str | None = None,
    business_id: str | None = None,
    confidence: float | None = None,
    source: str = "upload",
    source_ref: str | None = None,
    file_url: str | None = None,
    raw_extracted: dict | None = None,
    line_items: list[dict] | None = None,
) -> str:
    bill_id = str(uuid.uuid4())
    try:
        with cursor() as cur:
            # ON CONFLICT DO NOTHING closes the check-then-insert race in
            # source_ref_exists(): two scans of the same mailbox running
            # concurrently both saw "not imported yet" and both inserted, so a
            # bill could land twice. Backed by the partial unique index in
            # migrations/002_bills_hardening.sql; untargeted so it stays valid
            # on a database where that migration has not been applied yet.
            cur.execute(
                """INSERT INTO bills
                   (bill_id, business_id, vendor, amount, invoice_number,
                    due_date, issue_date, currency, status, category,
                    confidence, source, source_ref, file_url, raw_extracted)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING
                   RETURNING bill_id;""",
                (
                    bill_id, business_id, vendor, amount, invoice_number,
                    due_date, issue_date, currency, status, category,
                    confidence, source, source_ref, file_url,
                    json.dumps(raw_extracted) if raw_extracted else None,
                ),
            )
            if cur.fetchone() is None:
                # Lost the race — the other writer's row is the canonical one.
                cur.execute(
                    "SELECT bill_id FROM bills WHERE source=%s AND source_ref=%s LIMIT 1;",
                    (source, source_ref),
                )
                existing = cur.fetchone()
                if existing:
                    log.info("insert_bill: %s/%s already imported, reusing %s",
                             source, source_ref, existing[0])
                    return str(existing[0])
                raise RuntimeError("bill insert was rejected and no existing row matched")
            if line_items:
                for item in line_items:
                    cur.execute(
                        """INSERT INTO bill_line_items (bill_id, description, quantity, unit_price, total)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            bill_id,
                            item.get("description"),
                            item.get("quantity", 1),
                            item.get("unit_price"),
                            item.get("total"),
                        ),
                    )
    except Exception as exc:
        log.error("insert_bill failed: %s", exc)
        raise
    return bill_id


_UPDATABLE = {
    "business_id", "vendor", "amount", "invoice_number", "due_date", "issue_date",
    "currency", "status", "category", "confidence", "notes", "file_url",
}


class BillUpdateError(ValueError):
    """A caller-supplied field is invalid — surface as 400, not 404."""


def update_bill(bill_id: str, **fields) -> bool:
    """Patch a bill. Raises BillUpdateError for bad input so the API layer can
    tell "you sent nonsense" apart from "no such bill"."""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return False

    if "status" in updates:
        wanted = (updates["status"] or "").strip()
        if wanted not in ALL_STATUSES:
            raise BillUpdateError(
                f"status must be one of {sorted(ALL_STATUSES)}, got {updates['status']!r}"
            )
        # 'overdue'/'due-soon' are computed from due_date on read; storing them
        # would just go stale. Collapse them back to the state they refine.
        updates["status"] = "pending" if wanted in DERIVED_STATUSES else wanted

    for key in ("due_date", "issue_date"):
        if key in updates and updates[key] is not None:
            if _as_date(updates[key]) is None:
                raise BillUpdateError(f"{key} must be an ISO date (YYYY-MM-DD)")

    # updated_at is a SQL expression, not a bound value. It used to be smuggled
    # through as the literal string "now()" and filtered back out by comparing
    # every value against that string — which meant a bill whose notes were
    # exactly "now()" had those notes turned into a SQL call. Keep the
    # expression out of the value dict entirely.
    cols = ", ".join(f"{k} = %s" for k in updates) + ", updated_at = now()"
    vals = list(updates.values())
    try:
        with cursor() as cur:
            cur.execute(
                f"UPDATE bills SET {cols} WHERE bill_id = %s",
                vals + [bill_id],
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("update_bill failed: %s", exc)
        return False


def cleanup_old_drive_bills() -> int:
    """Delete Google Drive bill stubs that have no financial data.

    These are filename-only stubs imported before the date-filter was in place.
    Going forward, scan_drive() skips pre-June-2026 files and always sets
    issue_date from the filename, so this cleanup won't delete new imports.
    """
    try:
        with cursor() as cur:
            cur.execute("""
                DELETE FROM bills
                WHERE source = 'google-drive'
                AND amount IS NULL
                AND issue_date IS NULL
                AND due_date IS NULL
            """)
            return cur.rowcount
    except Exception as exc:
        log.warning("cleanup_old_drive_bills failed: %s", exc)
        return 0


_BILL_NOT_FOUND = object()  # sentinel distinguishing "not found" from "no file_url"


def delete_bill(bill_id: str):
    """Delete a bill and its line items.

    Returns the file_url string (possibly None/empty) on success, or the
    _BILL_NOT_FOUND sentinel if no row matched.  Callers check with
    ``result is _BILL_NOT_FOUND``.
    """
    try:
        with cursor() as cur:
            cur.execute("SELECT file_url FROM bills WHERE bill_id = %s;", (bill_id,))
            row = cur.fetchone()
            if not row:
                return _BILL_NOT_FOUND
            file_url = row[0]
            cur.execute("DELETE FROM bills WHERE bill_id = %s;", (bill_id,))
            return file_url  # may be None if no S3 object
    except Exception as exc:
        log.error("delete_bill failed: %s", exc)
        raise


def source_ref_exists(source: str, source_ref: str) -> bool:
    """De-dupe: returns True if we already imported this email/drive item."""
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT 1 FROM bills WHERE source=%s AND source_ref=%s LIMIT 1;",
                (source, source_ref),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


# ── Insights (computed on-the-fly) ───────────────────────────────────────────

UPCOMING_WINDOW_DAYS = 14

_EMPTY_INSIGHTS = {"overdue": [], "upcoming": [], "cashflow": [], "suggestions": []}


def compute_insights(bills: list[dict] | None = None) -> dict:
    """Summarise the bill list for the insights column.

    Returns STRUCTURED rows, never HTML. This function used to interpolate the
    vendor and business name into a markup string that the frontend dropped
    straight into innerHTML — and `vendor` is derived from an email's From
    header or a Drive filename, i.e. from outside our trust boundary. Rendering
    (and escaping) is the frontend's job; this layer only reports facts.

    `bills` may be passed in by a caller that has already listed them, so a
    single page load does not query the same table twice.
    """
    if bills is None:
        bills = list_bills()
    if not bills:
        return dict(_EMPTY_INSIGHTS)

    today = date.today()

    overdue, upcoming, cashflow_map = [], [], {}
    for b in bills:
        amt = float(b["amount"] or 0)
        biz = b.get("business_name") or "Unassigned"
        bid = b.get("business_id")
        currency = b.get("currency") or "USD"
        dd = _as_date(b.get("due_date"))
        days = (dd - today).days if dd else None

        if b["status"] == "overdue":
            overdue.append({
                "bill_id": b["bill_id"],
                "business": biz,
                "vendor": b.get("vendor") or "Unknown vendor",
                "amount": amt,
                "currency": currency,
                "days_overdue": -days if days is not None else None,
                "dot": "#f87171",
                "action": "Pay Now",
            })

        elif b["status"] in ("pending", "due-soon") and days is not None:
            if 0 <= days <= UPCOMING_WINDOW_DAYS:
                upcoming.append({
                    "bill_id": b["bill_id"],
                    "business": biz,
                    "vendor": b.get("vendor") or "Unknown vendor",
                    "amount": amt,
                    "currency": currency,
                    "days_until_due": days,
                    "dot": "#f59e0b" if days <= 3 else "#34d399",
                    "action": "Set Reminder",
                })

        if bid and b["status"] != "paid":
            entry = cashflow_map.setdefault(bid, {
                "business_id": bid,
                "label": biz,
                "color": b.get("business_color") or "#9aa3b2",
                "amount": 0.0,
                "currencies": set(),
            })
            entry["amount"] += amt
            entry["currencies"].add(currency)

    cashflow = sorted(cashflow_map.values(), key=lambda x: -x["amount"])
    max_amt = max((c["amount"] for c in cashflow), default=0) or 1
    for c in cashflow:
        c["pct"] = round(c["amount"] / max_amt * 100)
        # A single bar can only honestly carry one currency symbol; report the
        # set so the UI can flag a mixed-currency total rather than imply "$".
        c["currencies"] = sorted(c.pop("currencies"))
        c["currency"] = c["currencies"][0] if len(c["currencies"]) == 1 else None

    overdue.sort(key=lambda r: -(r["days_overdue"] or 0))
    upcoming.sort(key=lambda r: r["days_until_due"])

    return {
        "overdue": overdue[:5],
        "upcoming": upcoming[:5],
        "cashflow": cashflow[:7],
        "suggestions": [],   # populated by AI in future phase
    }
