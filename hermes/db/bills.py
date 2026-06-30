"""DB operations for bills, businesses, and line items."""
from __future__ import annotations

import json
import logging
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

def list_businesses() -> list[dict]:
    try:
        with cursor(commit=False) as cur:
            cur.execute(
                "SELECT business_id, name, slug, emoji, color, industry, created_at "
                "FROM businesses ORDER BY name;"
            )
            return [_clean(r) for r in _row(cur, [
                "business_id", "name", "slug", "emoji", "color", "industry", "created_at"
            ])]
    except Exception as exc:
        log.warning("list_businesses failed: %s", exc)
        return []


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
            return _clean(dict(zip(
                ["business_id", "name", "slug", "emoji", "color", "industry"], row
            )))
    except Exception as exc:
        log.error("upsert_business failed: %s", exc)
        raise


# ── Bills ────────────────────────────────────────────────────────────────────

_BILL_COLS = [
    "bill_id", "business_id", "vendor", "amount", "invoice_number",
    "due_date", "issue_date", "currency", "status", "category",
    "confidence", "source", "source_ref", "file_url", "notes", "created_at", "updated_at",
]


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
            if status:
                where.append("b.status = %s"); params.append(status)
            if source:
                where.append("b.source = %s"); params.append(source)
            cur.execute(
                f"""SELECT {', '.join('b.' + c for c in _BILL_COLS)},
                       bz.name AS business_name, bz.emoji AS business_emoji,
                       bz.color AS business_color
                    FROM bills b
                    LEFT JOIN businesses bz USING (business_id)
                    WHERE {' AND '.join(where)}
                    ORDER BY
                      CASE b.status WHEN 'overdue' THEN 0 WHEN 'due-soon' THEN 1
                        WHEN 'processing' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,
                      b.due_date ASC NULLS LAST
                    LIMIT %s;""",
                params + [limit],
            )
            cols = _BILL_COLS + ["business_name", "business_emoji", "business_color"]
            bills = [_clean(r) for r in _row(cur, cols)]

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
            return _clean(dict(zip(cols, row)))
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
            cur.execute(
                """INSERT INTO bills
                   (bill_id, business_id, vendor, amount, invoice_number,
                    due_date, issue_date, currency, status, category,
                    confidence, source, source_ref, file_url, raw_extracted)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    bill_id, business_id, vendor, amount, invoice_number,
                    due_date, issue_date, currency, status, category,
                    confidence, source, source_ref, file_url,
                    json.dumps(raw_extracted) if raw_extracted else None,
                ),
            )
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


def update_bill(bill_id: str, **fields) -> bool:
    allowed = {
        "business_id", "vendor", "amount", "invoice_number", "due_date",
        "status", "category", "confidence", "notes", "file_url",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = "now()"
    cols = ", ".join(
        f"{k} = now()" if v == "now()" else f"{k} = %s" for k, v in updates.items()
    )
    vals = [v for v in updates.values() if v != "now()"]
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

def compute_insights() -> dict:
    bills = list_bills()
    if not bills:
        return {"overdue": [], "upcoming": [], "cashflow": [], "suggestions": []}

    from datetime import date as d
    today = d.today()

    overdue, upcoming, cashflow_map = [], [], {}
    for b in bills:
        amt = float(b["amount"] or 0)
        biz = b.get("business_name") or "Unknown"
        bid = b.get("business_id")

        if b["status"] == "overdue":
            overdue.append({
                "bill_id": b["bill_id"],
                "text": f"{biz}: {b['vendor']} invoice of <strong>${amt:,.2f}</strong> is overdue.",
                "dot": "#f87171",
                "action": "Pay Now",
            })

        elif b["status"] in ("pending", "due-soon") and b.get("due_date"):
            try:
                dd = date.fromisoformat(b["due_date"]) if isinstance(b["due_date"], str) else b["due_date"]
                days = (dd - today).days
                if 0 <= days <= 14:
                    upcoming.append({
                        "bill_id": b["bill_id"],
                        "text": f"{biz}: {b['vendor']} (${amt:,.0f}) due in <strong>{days} day{'s' if days!=1 else ''}</strong>.",
                        "dot": "#f59e0b" if days <= 3 else "#34d399",
                        "action": "Set Reminder",
                    })
            except (ValueError, TypeError):
                pass

        if bid and b["status"] != "paid":
            cashflow_map[bid] = cashflow_map.get(bid, {
                "label": (biz or "")[:10],
                "color": b.get("business_color") or "#9aa3b2",
                "amount": 0,
            })
            cashflow_map[bid]["amount"] += amt

    cashflow = sorted(cashflow_map.values(), key=lambda x: -x["amount"])
    max_amt = max((c["amount"] for c in cashflow), default=1) or 1
    for c in cashflow:
        c["pct"] = round(c["amount"] / max_amt * 100)

    return {
        "overdue": overdue[:5],
        "upcoming": upcoming[:5],
        "cashflow": cashflow[:7],
        "suggestions": [],   # populated by AI in future phase
    }
