"""Bill extraction (NuExtract) and connected-app scouting (Maton Gmail / Drive)."""
from __future__ import annotations

import base64
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import requests

from .config import Config
from .db.bills import insert_bill, source_ref_exists, update_bill
from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.bills")

_NUEXTRACT_TIMEOUT = 60


# ── NuExtract client ─────────────────────────────────────────────────────────

_BILL_SCHEMA = {
    "vendor_name": "",
    "invoice_number": "",
    "issue_date": "",
    "due_date": "",
    "total_amount": "",
    "currency": "",
    "category": "",
    "line_items": [
        {"description": "", "quantity": "", "unit_price": "", "total": ""}
    ],
}


def extract_bill(filename: str, raw: bytes, mime: str) -> dict[str, Any]:
    """Send a file to NuExtract and return normalised bill fields.

    Returns a dict with keys: vendor, amount, invoice_number, due_date,
    issue_date, currency, category, line_items, raw_extracted.
    """
    api_key = Config.NUEXTRACT_API_KEY
    base = Config.NUEXTRACT_BASE_URL
    if not api_key:
        raise RuntimeError("NUEXTRACT_API_KEY is not configured.")

    import json

    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        resp = requests.post(
            f"{base}/api/extract",
            headers=headers,
            files={"file": (filename, raw, mime or "application/octet-stream")},
            data={"schema": json.dumps(_BILL_SCHEMA)},
            timeout=_NUEXTRACT_TIMEOUT,
        )
        resp.raise_for_status()
        raw_result = resp.json()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (404, 405):
            try:
                resp2 = requests.post(
                    f"{base}/v1/extract",
                    headers=headers,
                    files={"file": (filename, raw, mime or "application/octet-stream")},
                    data={"schema": json.dumps(_BILL_SCHEMA)},
                    timeout=_NUEXTRACT_TIMEOUT,
                )
                resp2.raise_for_status()
                raw_result = resp2.json()
            except Exception as exc2:
                raise RuntimeError(f"NuExtract extraction failed: {exc2}") from exc2
        else:
            raise RuntimeError(f"NuExtract extraction failed ({exc.response.status_code}): {exc}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"NuExtract network error: {exc}") from exc

    return _normalise(raw_result, filename)


def _normalise(raw: dict, filename: str) -> dict[str, Any]:
    """Turn the raw NuExtract response into our internal bill format."""
    data: dict = raw
    for key in ("result", "data", "extraction", "output"):
        if isinstance(raw.get(key), dict):
            data = raw[key]
            break

    def _str(k: str) -> str | None:
        v = data.get(k)
        return str(v).strip() or None if v else None

    def _amount(k: str) -> float | None:
        v = _str(k)
        if not v:
            return None
        cleaned = re.sub(r"[^\d.]", "", v)
        try:
            return float(cleaned)
        except ValueError:
            return None

    vendor = _str("vendor_name") or filename.split(".")[0].replace("-", " ").replace("_", " ").title()
    amount = _amount("total_amount")
    currency = (_str("currency") or "USD").upper()[:3]
    due_date = _normalise_date(_str("due_date"))
    issue_date = _normalise_date(_str("issue_date"))
    invoice_number = _str("invoice_number")
    category = _str("category")

    raw_items = data.get("line_items") or []
    line_items = []
    for item in (raw_items if isinstance(raw_items, list) else []):
        if not isinstance(item, dict):
            continue
        line_items.append({
            "description": str(item.get("description") or "").strip() or None,
            "quantity": _parse_float(item.get("quantity")),
            "unit_price": _parse_float(item.get("unit_price")),
            "total": _parse_float(item.get("total")),
        })

    return {
        "vendor": vendor,
        "amount": amount,
        "invoice_number": invoice_number,
        "due_date": due_date,
        "issue_date": issue_date,
        "currency": currency,
        "category": category,
        "line_items": line_items,
        "raw_extracted": raw,
    }


def _extract_date_from_filename(filename: str) -> str | None:
    """Extract ISO date from Drive filename patterns like '09 04 2023' (DD MM YYYY).

    Returns ISO date string (e.g. '2023-04-09') or None if no date found.
    """
    if not filename:
        return None
    # Primary: DD MM YYYY embedded in filename
    m = re.search(r'\b(\d{2})\s+(\d{2})\s+(20[1-3]\d)\b', filename)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            from datetime import date as _date
            return _date(year, month, day).isoformat()
        except ValueError:
            pass
    # Fallback: bare 4-digit year → approximate Jan 1
    m = re.search(r'\b(20[1-3]\d)\b', filename)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _vendor_from_filename(filename: str) -> str:
    """Extract a clean vendor name from Drive filing-system filenames."""
    v = re.sub(r"\.(pdf|png|jpg|jpeg|heic)$", "", filename, flags=re.I).strip()
    v = re.sub(r"[-_]+", " ", v).strip()

    m = re.match(r"^[A-Z]{2,5}\s+[A-Z]+\s+[A-Z]\s+(.+)$", v)
    if m:
        v = m.group(1).strip()
        v = re.sub(r"^[A-Z]+\s+(?:LLC|INC|CORP|LTD)\s+", "", v).strip()

    m = re.match(r"^row\d+\s+FOUND\s+\w+\s+INVOICE\s+\w+\s+(.+)$", v, re.I)
    if m:
        v = m.group(1).strip()

    m = re.match(r"^R\d+\s+\w+\s+(.+)$", v, re.I)
    if m:
        v = m.group(1).strip()

    m = re.match(r"^(?:CS|GIF|INV|ARC)\s+(?:INV|ARC|GIF)?\s*(?:ARC)?\s*(.+?)\s+INVOICE\b.*$", v, re.I)
    if m:
        v = m.group(1).strip()

    v = re.sub(r"\s+(?:BILL|INVOICE|RECEIPT|STATEMENT|PAYMENT|ENERGY BILL|UTILITY BILL|ELEC\w*)$", "", v, flags=re.I).strip()
    v = re.sub(r"\s+\d{2}\s+\d{2}\s+\d{4}.*$", "", v).strip()
    v = re.sub(r"\s+\d{4}[\s\-]\d{2}.*$", "", v).strip()
    v = re.sub(r"\s+\d{2}\s+\d{2}\s+\d{4}$", "", v).strip()
    v = re.sub(r"\s+[0-9a-f]{6,}(?:\s+\d+)?$", "", v, flags=re.I).strip()
    v = re.sub(r"\s+\d+$", "", v).strip()
    v = re.sub(r"\b(UNKNOWN|ARCHIVED|DUE|FOUND|GIF|PER|ARC)\b", "", v, flags=re.I).strip()
    v = re.sub(r"\s{2,}", " ", v).strip()

    return v.title() if v else filename


def _normalise_date(value: str | None) -> str | None:
    if not value:
        return None
    import datetime as dt
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y",
                "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return value.strip() or None


def _parse_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(v)))
    except (ValueError, TypeError):
        return None


# ── Business assignment ───────────────────────────────────────────────────────

def guess_business(
    vendor: str,
    category: str | None,
    businesses: list[dict],
) -> tuple[str | None, float]:
    """Return (business_id, confidence) by fuzzy-matching vendor/category keywords."""
    if not businesses:
        return None, 0.0

    vendor_lower = (vendor or "").lower()
    cat_lower = (category or "").lower()

    tech_keywords = {"aws", "cloud", "saas", "stripe", "github", "figma", "slack",
                     "notion", "linear", "vercel", "openai", "anthropic", "mongo", "postgres"}
    media_keywords = {"meta", "google ads", "facebook", "instagram", "ad", "campaign",
                      "creative", "podcast", "broadcast", "media"}
    food_keywords = {"sysco", "food", "restaurant", "catering", "kitchen", "produce",
                     "ingredients", "beverage", "dining"}
    hr_keywords = {"adp", "payroll", "hr", "staffing", "recruiting", "benefits",
                   "employee", "payslip", "compensation"}
    farm_keywords = {"deere", "farm", "crop", "irrigation", "seed", "agri", "livestock",
                     "equipment lease", "tractor"}
    realestate_keywords = {"cushman", "property", "lease", "rent", "real estate",
                            "mortgage", "maintenance", "facility", "office"}
    consulting_keywords = {"quickbooks", "accounting", "consulting", "advisory",
                            "professional services", "strategy", "audit"}

    keyword_sets = [
        (tech_keywords,        "SaaS / Tech"),
        (media_keywords,       "Media"),
        (food_keywords,        "Food & Bev"),
        (hr_keywords,          "HR Services"),
        (farm_keywords,        "Agriculture"),
        (realestate_keywords,  "Real Estate"),
        (consulting_keywords,  "Consulting"),
    ]

    matched_industry = None
    for kws, industry in keyword_sets:
        if any(k in vendor_lower or k in cat_lower for k in kws):
            matched_industry = industry
            break

    if not matched_industry:
        return None, 0.4

    matches = [b for b in businesses if (b.get("industry") or "").lower() == matched_industry.lower()]
    if not matches:
        return None, 0.4
    if len(matches) == 1:
        return str(matches[0]["business_id"]), 0.88
    return str(matches[0]["business_id"]), 0.60


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _decode_b64url(data: str) -> str:
    """Decode Gmail's base64url-encoded body content to a UTF-8 string."""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def fetch_gmail_body(message_id: str) -> dict | None:
    """Fetch and return the decoded body of a Gmail message.

    Returns {"type": "html"|"text", "content": "..."} or None.
    Handles both simple (top-level body) and multipart messages.
    """
    maton = MatonClient()
    try:
        resp = maton.request(
            "google-mail", "GET",
            f"gmail/v1/users/me/messages/{message_id}",
            query={"format": "full"},
        )
    except MatonError as exc:
        log.warning("fetch_gmail_body %s failed: %s", message_id, exc)
        return None

    payload = (resp.get("data") or {}).get("payload") or {}
    html_body: str | None = None
    text_body: str | None = None

    def _try_part(mime: str, body: dict) -> None:
        nonlocal html_body, text_body
        data = (body or {}).get("data", "")
        if not data:
            return
        try:
            decoded = _decode_b64url(data)
        except Exception:
            return
        mime = mime.lower()
        if mime == "text/html" and not html_body:
            html_body = decoded
        elif mime == "text/plain" and not text_body:
            text_body = decoded

    # Top-level body (simple non-multipart message)
    _try_part(payload.get("mimeType", ""), payload.get("body") or {})

    # Walk all MIME leaf parts
    for part in _flatten_parts(payload):
        _try_part(part.get("mimeType", ""), part.get("body") or {})
        if html_body and text_body:
            break

    if html_body:
        return {"type": "html", "content": html_body}
    if text_body:
        return {"type": "text", "content": text_body}
    return None


def _flatten_parts(payload: dict) -> list[dict]:
    """Recursively collect all MIME leaf parts from a Gmail message payload."""
    result = []
    for part in (payload.get("parts") or []):
        if part.get("parts"):
            result.extend(_flatten_parts(part))
        else:
            result.append(part)
    return result


def _download_gmail_attachment(
    maton: MatonClient,
    msg_id: str,
    att_id: str,
    mime: str,
) -> bytes | None:
    """Download a Gmail attachment via Maton and decode from base64url."""
    try:
        resp = maton.request(
            "google-mail", "GET",
            f"gmail/v1/users/me/messages/{msg_id}/attachments/{att_id}",
        )
        b64 = (resp.get("data") or {}).get("data", "")
        if not b64:
            return None
        # Gmail uses base64url (RFC 4648), add padding
        padding = 4 - len(b64) % 4
        if padding != 4:
            b64 += "=" * padding
        return base64.urlsafe_b64decode(b64)
    except Exception as exc:
        log.debug("Gmail attachment download failed msg=%s att=%s: %s", msg_id, att_id, exc)
        return None


# ── Gmail scanner ─────────────────────────────────────────────────────────────

_ATTACHMENT_MIMES = {"application/pdf", "image/jpeg", "image/jpg", "image/png", "image/heic"}


def scan_gmail(businesses: list[dict], days: int = 45) -> list[dict]:
    """Fetch invoice emails from Gmail, download PDF attachments, upload to S3."""
    from . import storage  # lazy — avoids circular at module load

    maton = MatonClient()
    created: list[dict] = []

    query = (
        "subject:(invoice OR bill OR receipt OR statement OR payment) "
        f"has:attachment newer_than:{days}d"
    )

    try:
        resp = maton.request(
            "google-mail", "GET", "gmail/v1/users/me/messages",
            query={"q": query, "maxResults": 50},
        )
    except MatonError as exc:
        log.warning("Gmail message list failed: %s", exc)
        return []

    messages = (resp.get("data") or {}).get("messages") or []
    if not messages:
        log.info("Gmail scan: no matching messages.")
        return []

    for msg_stub in messages[:30]:
        msg_id = msg_stub.get("id")
        if not msg_id:
            continue
        if source_ref_exists("gmail", msg_id):
            continue

        try:
            detail = maton.request(
                "google-mail", "GET",
                f"gmail/v1/users/me/messages/{msg_id}",
                query={"format": "full"},
            )
        except MatonError:
            continue

        payload = (detail.get("data") or {}).get("payload") or {}
        headers_list = payload.get("headers") or []
        headers = {h["name"].lower(): h["value"] for h in headers_list if "name" in h}

        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        date_str = headers.get("date", "")

        vendor_m = re.match(r'^([^<]+)', sender)
        vendor = vendor_m.group(1).strip().strip('"').strip("'") if vendor_m else sender.split("@")[0].title()

        amount_match = re.search(r"\$[\d,]+(?:\.\d{2})?", subject)
        amount = _parse_float(amount_match.group().replace(",", "").lstrip("$")) if amount_match else None

        inv_match = re.search(r"#?(INV|INV-|Invoice\s*#?\s*)([A-Z0-9\-]+)", subject, re.I)
        invoice_number = inv_match.group().strip() if inv_match else None

        biz_id, confidence = guess_business(vendor, None, businesses)

        # Find and download the first PDF/image attachment → S3
        file_url = None
        for part in _flatten_parts(payload):
            part_mime = (part.get("mimeType") or "").lower()
            att_id = (part.get("body") or {}).get("attachmentId")
            if not att_id or part_mime not in _ATTACHMENT_MIMES:
                continue
            raw_bytes = _download_gmail_attachment(maton, msg_id, att_id, part_mime)
            if raw_bytes:
                ext = part_mime.split("/")[-1].replace("jpeg", "jpg")
                s3_key = f"bills/gmail/{msg_id[:16]}_{uuid.uuid4().hex[:8]}.{ext}"
                file_url = storage.put(s3_key, raw_bytes, part_mime)
                log.info("Gmail: uploaded %s attachment to S3 (%d bytes)", part_mime, len(raw_bytes))
            break  # only first valid attachment

        bill_id = insert_bill(
            vendor=vendor,
            amount=amount,
            invoice_number=invoice_number,
            status="pending",
            business_id=biz_id,
            confidence=confidence,
            source="gmail",
            source_ref=msg_id,
            file_url=file_url,
            raw_extracted={"subject": subject, "from": sender, "date": date_str},
        )
        created.append({"bill_id": bill_id, "vendor": vendor, "source": "gmail",
                        "has_document": bool(file_url)})

    log.info("Gmail scan: created %d new bills (%d with documents)",
             len(created), sum(1 for c in created if c.get("has_document")))
    return created


# ── Google Drive scanner ──────────────────────────────────────────────────────

def scan_drive(businesses: list[dict], days: int = 45) -> list[dict]:
    """List invoice PDFs from Google Drive and record bill stubs.

    Drive binary download is not possible through the Maton JSON gateway,
    so we store the Drive webViewLink as file_url for in-browser viewing.
    """
    maton = MatonClient()
    created: list[dict] = []

    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    drive_query = (
        "(name contains 'invoice' OR name contains 'bill' OR name contains 'receipt') "
        f"AND mimeType='application/pdf' AND modifiedTime > '{cutoff}'"
    )

    try:
        resp = maton.request(
            "google-drive", "GET", "drive/v3/files",
            query={
                "q": drive_query,
                "pageSize": 50,
                "orderBy": "modifiedTime desc",
                "fields": "files(id,name,size,modifiedTime,webViewLink,webContentLink,createdTime)",
            },
        )
    except MatonError as exc:
        log.warning("Drive file list failed: %s", exc)
        return []

    files = (resp.get("data") or {}).get("files") or []
    if not files:
        return []

    _CUTOFF = "2026-06-01"

    for f in files:
        file_id = f.get("id")
        filename = f.get("name", "document.pdf")
        web_link = f.get("webViewLink")
        if not file_id:
            continue
        if source_ref_exists("google-drive", file_id):
            continue

        # Extract date from filename (e.g. "09 04 2023" → "2023-04-09")
        from_date = _extract_date_from_filename(filename)

        # Skip files that are clearly dated before the cutoff
        if from_date and from_date < _CUTOFF:
            log.debug("Skipping Drive file '%s' (date %s before %s)", filename, from_date, _CUTOFF)
            continue

        vendor = _vendor_from_filename(filename)
        biz_id, confidence = guess_business(vendor, None, businesses)

        bill_id = insert_bill(
            vendor=vendor,
            amount=None,
            invoice_number=None,
            due_date=None,
            issue_date=from_date,   # Store extracted filename date
            status="pending",
            business_id=biz_id,
            confidence=confidence,
            source="google-drive",
            source_ref=file_id,
            file_url=web_link,
            raw_extracted={"drive_filename": filename, "webViewLink": web_link},
        )
        created.append({
            "bill_id": bill_id,
            "vendor": vendor,
            "source": "google-drive",
            "confidence": confidence,
            "file_url": web_link,
        })

    log.info("Drive scan: created %d bill stubs", len(created))
    return created


# ── Auto-pay detection ────────────────────────────────────────────────────────

def check_paid_bills(bills: list[dict]) -> list[str]:
    """Search Gmail for payment confirmation emails matching pending bills.

    Returns list of bill_ids that were marked paid.
    Conservative: requires both vendor keyword AND payment-confirmation subject.
    """
    maton = MatonClient()
    paid_ids: list[str] = []

    pending = [
        b for b in bills
        if b.get("status") in ("pending", "overdue", "due-soon")
    ]
    if not pending:
        return []

    for bill in pending:
        vendor = (bill.get("vendor") or "").strip()
        if not vendor or len(vendor) < 3:
            continue

        # Use first meaningful word from vendor name as search keyword
        # Skip generic words like "The", "A", "An"
        words = [w for w in vendor.split() if len(w) > 2 and w.lower() not in ("the", "and", "for")]
        if not words:
            continue
        keyword = words[0]

        query = (
            f'{keyword} '
            '(subject:receipt OR subject:"payment received" OR subject:"payment processed" '
            'OR subject:"payment confirmed" OR subject:"thank you for your payment" '
            'OR subject:"your payment" OR subject:"invoice paid") '
            'newer_than:30d'
        )

        try:
            resp = maton.request(
                "google-mail", "GET", "gmail/v1/users/me/messages",
                query={"q": query, "maxResults": 3},
            )
        except MatonError:
            continue

        messages = (resp.get("data") or {}).get("messages") or []
        if messages:
            update_bill(bill["bill_id"], status="paid")
            paid_ids.append(bill["bill_id"])
            log.info("Auto-marked bill %s (%s) as paid via Gmail confirmation",
                     bill["bill_id"], vendor)

    return paid_ids
