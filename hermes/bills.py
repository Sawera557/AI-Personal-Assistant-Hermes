"""Bill extraction (NuExtract) and connected-app scouting (Maton Gmail / Drive)."""
from __future__ import annotations

import base64
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .db.bills import insert_bill, source_ref_exists, update_bill
from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.bills")


# ── NuExtract client ─────────────────────────────────────────────────────────

# NuExtract template. Values are TYPE placeholders, not empty strings — the API
# rejects "" with 'Not a valid NuExtract template. Invalid Format'. Fields the
# document states literally use "verbatim-string" so _normalise gets the text as
# written (it does its own currency/date parsing); `category` is inferred rather
# than quoted, so it's a generated "string".
_BILL_SCHEMA = {
    "vendor_name": "verbatim-string",
    "invoice_number": "verbatim-string",
    "issue_date": "verbatim-string",
    "due_date": "verbatim-string",
    "total_amount": "verbatim-string",
    "currency": "verbatim-string",
    "category": "string",
    "line_items": [
        {
            "description": "verbatim-string",
            "quantity": "verbatim-string",
            "unit_price": "verbatim-string",
            "total": "verbatim-string",
        }
    ],
}


_numind_client = None


def _numind():
    """Lazily build (and cache) the NuMind SDK client. Raises RuntimeError with a
    user-relayable message when the API key is missing. The SDK targets
    https://nuextract.ai by default; NUEXTRACT_BASE_URL overrides the host."""
    global _numind_client
    if _numind_client is not None:
        return _numind_client
    api_key = Config.NUEXTRACT_API_KEY
    if not api_key:
        raise RuntimeError("NUEXTRACT_API_KEY is not configured.")
    try:
        from numind import Configuration, NuMind
    except ImportError as exc:  # pragma: no cover — dependency must be installed
        raise RuntimeError(f"numind SDK is not installed: {exc}") from exc
    if Config.NUEXTRACT_BASE_URL:
        _numind_client = NuMind(
            api_key=api_key, configuration=Configuration(host=Config.NUEXTRACT_BASE_URL)
        )
    else:
        _numind_client = NuMind(api_key=api_key)
    return _numind_client


def _with_timeout(fn, *args, **kwargs):
    """Run a blocking NuExtract SDK call under a hard wall-clock bound.

    The SDK submits a job and then blocks on an SSE stream whose call takes no
    timeout argument (`_request_timeout` only reaches the job-submission POST),
    so a slow or stuck job would otherwise hang the request indefinitely — long
    enough for a serverless platform to kill the function BEFORE the Claude-vision
    fallback can run. Bounding it here guarantees the caller regains control and
    can degrade. A timed-out call's thread is abandoned (Python can't kill
    threads); it is a daemon worker that dies with the process.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=Config.NUEXTRACT_TIMEOUT)
        except FuturesTimeout as exc:
            future.cancel()
            raise RuntimeError(
                f"NuExtract timed out after {Config.NUEXTRACT_TIMEOUT}s"
            ) from exc
    finally:
        # Don't block on an abandoned worker; let it finish in the background.
        executor.shutdown(wait=False)


def _result_dict(result: Any) -> dict[str, Any]:
    """Coerce a NuExtract structured `.result` (dict / pydantic model / JSON str)
    into a plain dict for downstream normalisation."""
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, str):
        try:
            import json

            parsed = json.loads(result)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def nuextract(filename: str, raw: bytes, mime: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Run NuExtract STRUCTURED extraction on a file, returning the extracted
    fields as a dict (wrapped as {"result": ...} for bills._normalise).

    Uses the official `numind` SDK's structured-extraction API. When
    NUEXTRACT_PROJECT_ID is configured the file is extracted against that project
    (its template defined in the dashboard); otherwise `schema` is sent as an
    on-the-fly template. Raises RuntimeError with a user-relayable message on
    failure so callers can fall back (e.g. to Claude vision).
    """
    client = _numind()
    project_id = Config.NUEXTRACT_PROJECT_ID or None
    try:
        resp = _with_timeout(
            client.extract_structured_data,
            project_id=project_id,
            template=None if project_id else schema,
            # Template mode makes the SDK create a temporary project, whose
            # CreateStructuredProjectRequest rejects a None `instructions` with a
            # pydantic validation error — so always pass a string.
            instructions="",
            input_file=raw,
            _request_timeout=Config.NUEXTRACT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — normalise every SDK/network error
        raise RuntimeError(f"NuExtract extraction failed: {exc}") from exc

    if getattr(resp, "error", None):
        raise RuntimeError(f"NuExtract extraction error: {resp.error}")
    return {"result": _result_dict(getattr(resp, "result", None))}


def nuextract_content(filename: str, raw: bytes, mime: str) -> str:
    """Run NuExtract CONTENT extraction on a file, returning verbatim Markdown
    text. Raises RuntimeError with a user-relayable message on failure so callers
    can fall back (e.g. to Claude vision)."""
    client = _numind()
    try:
        resp = _with_timeout(
            client.extract_content, input_file=raw, _request_timeout=Config.NUEXTRACT_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — normalise every SDK/network error
        raise RuntimeError(f"NuExtract content extraction failed: {exc}") from exc

    result = getattr(resp, "result", None)
    return result if isinstance(result, str) else str(result or "")


def extract_bill(filename: str, raw: bytes, mime: str) -> dict[str, Any]:
    """Read a bill document and return normalised bill fields.

    Returns a dict with keys: vendor, amount, invoice_number, due_date,
    issue_date, currency, category, line_items, raw_extracted.

    NuExtract first; on any failure fall back to OCR-then-LLM. _with_timeout()
    bounds the NuExtract call specifically so this fallback still gets to run,
    but until now there was no fallback here to run — a NuExtract outage or an
    unset API key meant every bill upload was rejected outright, even though
    hermes/extract.py already carries a working vision OCR path. Reading a bill
    is the whole feature; it must not rest on one vendor being up.
    """
    try:
        return _normalise(nuextract(filename, raw, mime, _BILL_SCHEMA), filename)
    except RuntimeError as exc:
        log.warning("NuExtract failed for %s (%s); falling back to OCR + LLM", filename, exc)

    try:
        return _extract_bill_via_ocr(filename, raw, mime)
    except Exception as exc:  # noqa: BLE001 — one relayable message for the caller
        raise RuntimeError(
            f"Couldn't read '{filename}': NuExtract was unavailable and the OCR "
            f"fallback also failed ({exc})."
        ) from exc


_OCR_BILL_SYSTEM = (
    "You read invoices and bills. Given the text of one document, return a single "
    "JSON object with exactly these keys: vendor_name, invoice_number, issue_date, "
    "due_date, total_amount, currency, category, line_items. line_items is a list of "
    "objects with description, quantity, unit_price, total. Copy values verbatim from "
    "the document. Use null for anything the document does not state — never guess, "
    "and never invent an amount or a date. Return only the JSON object."
)


def _extract_bill_via_ocr(filename: str, raw: bytes, mime: str) -> dict[str, Any]:
    """Transcribe the document (NuExtract content → Claude vision) and have the
    background model pull the bill fields out of that text."""
    from . import extract, llm_json  # lazy — heavy imports, only needed on failure

    text = extract.extract_text(filename, raw, mime).strip()
    if not text:
        raise RuntimeError("the document contained no readable text")

    data = llm_json.call_json(
        system=_OCR_BILL_SYSTEM,
        user_message=text[:24000],
        max_tokens=2000,
        fallback_key="bill",
    )
    return _normalise({"result": data, "_extracted_by": "ocr-fallback"}, filename)


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
        return _parse_float(_str(k))

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


def _parse_rfc2822_date(value: str | None) -> str | None:
    """Turn a Gmail `Date:` header into an ISO date string, or None."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


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


def _filename_date_is_exact(filename: str) -> bool:
    """True when the filename carried a full DD MM YYYY, not just a bare year.

    A bare year is guessed as Jan 1, which is precise enough to record as a
    rough issue_date but far too coarse to reject a file on.
    """
    return bool(re.search(r'\b\d{2}\s+\d{2}\s+20[1-3]\d\b', filename or ""))


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
    """Parse a date the model read off a document into ISO, or give up.

    Giving up must mean None. Returning the raw string on a failed parse (the
    previous behaviour) sent free text such as "Net 30" or "upon receipt" into
    a Postgres DATE column, and since insert_bill() writes the bill and its
    line items in one transaction, that single bad field threw away the whole
    bill — the user got an extraction error for a document we had read fine.
    """
    if not value:
        return None
    import datetime as dt
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y",
                "%d.%m.%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    log.debug("Unparseable date on bill document: %r", value)
    return None


# 1.234,56 (European) vs 1,234.56 (US) — decided by which separator comes last.
_TRAILING_DECIMAL = re.compile(r"[.,](\d{1,2})\s*$")


def _parse_float(v: Any) -> float | None:
    """Parse a money-ish string into a float.

    Handles thousands separators in either convention, a leading/trailing minus,
    and parenthesised negatives ("(1,200.00)" is accounting for -1200). The old
    version stripped everything but digits and dots, so it read "1.234,56" as
    1.234 — a €1,234.56 invoice recorded as one euro twenty-three — and silently
    dropped the sign from every credit note.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    negative = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    # Peel the sign markers off before looking for the decimal group, or a
    # trailing ")" pushes the decimals out of the end-anchored match and
    # "(1,200.00)" reads as 120000.
    s = s.strip("()").strip().strip("-").strip()
    m = _TRAILING_DECIMAL.search(s)
    if m:
        whole = re.sub(r"\D", "", s[: m.start()])
        cleaned = f"{whole or '0'}.{m.group(1)}"
    else:
        cleaned = re.sub(r"\D", "", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -amount if negative else amount


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
        data = _maton_data(resp, f"Gmail message {message_id}")
    except MatonError as exc:
        log.warning("fetch_gmail_body %s failed: %s", message_id, exc)
        return None

    payload = data.get("payload") or {}
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
        b64 = _maton_data(resp, f"Gmail attachment {att_id}").get("data", "")
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


def _maton_data(resp: dict, what: str) -> dict:
    """Unwrap a Maton `{status, ok, data}` envelope, raising on a failed call.

    MatonClient.request() does NOT raise for a non-2xx — it returns ok=False
    with the API's error body sitting in `data`. Every caller here used to read
    `resp["data"]` straight through, so a disconnected account came back as
    {"code": 400, "message": "Your connections for `google-mail` are either
    PENDING or FAILED..."} and got treated as a Gmail message: no `payload`, no
    `headers`, and a bill filed with an empty vendor. That is where the 48
    nameless July bills came from — not from the message data being odd, but
    from an error response being mistaken for data.
    """
    if not resp.get("ok", True):
        data = resp.get("data")
        detail = data.get("message") if isinstance(data, dict) else None
        raise MatonError(
            f"{what} failed ({resp.get('status')}): {detail or data or 'unknown error'}"
        )
    data = resp.get("data")
    return data if isinstance(data, dict) else {}


def _vendor_from_sender(sender: str) -> str | None:
    """Display name from a From header, falling back to the sending domain.

    `Acme Billing <billing@acme.com>` -> "Acme Billing"
    `<billing@acme.com>`              -> "Acme"
    """
    sender = (sender or "").strip()
    if not sender:
        return None
    display = re.match(r'^([^<]+)', sender)
    if display:
        name = display.group(1).strip().strip('"').strip("'").strip()
        if name:
            return name
    # No display name — derive one from the address rather than returning the
    # raw "<billing@acme.com>" the old fallback produced.
    addr = re.search(r'[\w.+-]+@([\w-]+)', sender)
    if addr:
        return addr.group(1).replace("-", " ").title()
    return None


def _vendor_from_subject(subject: str) -> str | None:
    """Last-resort name: the subject line, trimmed to something card-sized."""
    s = re.sub(r"\s+", " ", (subject or "")).strip()
    return s[:80] if s else None


def _client_for_business(business: dict) -> MatonClient:
    """Resolve the MatonClient for one business's own connected account.

    Raises ValueError (caller should surface this as a clear "not connected"
    message — never silently fall back to a different business's key).
    """
    if not business:
        raise ValueError("No business given — cannot resolve a Maton connection.")
    slug = business.get("slug")
    key = Config.maton_key_for_slug(slug)
    if not key:
        raise ValueError(f"'{slug}' has no Maton connection configured yet.")
    return MatonClient(api_key=key)


def scan_gmail(business: dict, all_businesses: list[dict], days: int = 45) -> list[dict]:
    """Fetch invoice emails from ONE business's own Gmail, download PDF
    attachments, upload to S3.

    Every bill found is tagged directly with `business`'s own business_id —
    we already know whose mailbox we scanned, so no guessing is needed. The
    one exception is the shared "Personal & Family" account ("per"), which
    mixes personal and business content, so `guess_business()` still runs
    against `all_businesses` there to route bills to their real business.
    """
    from . import storage  # lazy — avoids circular at module load

    maton = _client_for_business(business)
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
        data = _maton_data(resp, f"Gmail search for '{business.get('slug')}'")
    except MatonError as exc:
        # Raise rather than return []: a disconnected mailbox is not the same
        # answer as an empty one, and the scan endpoint now reports it.
        log.warning("Gmail message list failed: %s", exc)
        raise

    messages = data.get("messages") or []
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
            detail_data = _maton_data(detail, f"Gmail message {msg_id}")
        except MatonError as exc:
            log.warning("Gmail scan: could not read message %s (%s)", msg_id, exc)
            continue

        payload = detail_data.get("payload") or {}
        headers_list = payload.get("headers") or []
        headers = {h["name"].lower(): h["value"] for h in headers_list if "name" in h}

        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        date_str = headers.get("date", "")

        # A message we can't name at all is not a bill we can file. Between
        # 1 and 27 July 2026 a Maton payload-truncation bug returned messages
        # with no headers, and this code happily wrote 49 bills with an empty
        # vendor — blank rows in the list that no search or payment check could
        # ever match. The transport bug is fixed; this guard makes the scanner
        # refuse to record a nameless bill if it ever recurs.
        vendor = _vendor_from_sender(sender) or _vendor_from_subject(subject)
        if not vendor:
            log.warning(
                "Gmail scan: message %s has no usable From or Subject header "
                "(headers=%d) — skipping rather than filing a nameless bill",
                msg_id, len(headers_list),
            )
            continue

        amount_match = re.search(r"\$[\d,]+(?:\.\d{2})?", subject)
        amount = _parse_float(amount_match.group()) if amount_match else None

        # group(2) is the identifier; group() would keep the "Invoice #" prefix
        # and store it as the invoice number, which then never matches a search.
        inv_match = re.search(r"(?:INV[-\s]?|Invoice\s*#?\s*|#)([A-Z0-9][A-Z0-9\-]{2,})", subject, re.I)
        invoice_number = inv_match.group(1).strip() if inv_match else None

        # The email's own Date header is the only date signal a subject-line
        # scan gets. Without it every Gmail bill landed with issue_date and
        # due_date both NULL, so it could never age into due-soon or overdue —
        # it sat in "pending" forever no matter how old it was.
        issue_date = _parse_rfc2822_date(date_str)

        if business.get("slug") == "per":
            biz_id, confidence = guess_business(vendor, None, all_businesses)
        else:
            biz_id, confidence = business.get("business_id"), 1.0

        # Find and download the first PDF/image attachment we can actually
        # retrieve → S3. Stopping at the first *candidate* meant one failed
        # download (expired attachment id, Maton hiccup) discarded the
        # remaining attachments on the same email and filed a documentless bill.
        file_url = None
        for part in _flatten_parts(payload):
            part_mime = (part.get("mimeType") or "").lower()
            att_id = (part.get("body") or {}).get("attachmentId")
            if not att_id or part_mime not in _ATTACHMENT_MIMES:
                continue
            raw_bytes = _download_gmail_attachment(maton, msg_id, att_id, part_mime)
            if not raw_bytes:
                continue
            ext = part_mime.split("/")[-1].replace("jpeg", "jpg")
            s3_key = f"bills/gmail/{msg_id[:16]}_{uuid.uuid4().hex[:8]}.{ext}"
            file_url = storage.put(s3_key, raw_bytes, part_mime)
            log.info("Gmail: uploaded %s attachment to S3 (%d bytes)", part_mime, len(raw_bytes))
            break

        try:
            bill_id = insert_bill(
                vendor=vendor,
                amount=amount,
                invoice_number=invoice_number,
                issue_date=issue_date,
                status="pending",
                business_id=biz_id,
                confidence=confidence,
                source="gmail",
                source_ref=msg_id,
                file_url=file_url,
                raw_extracted={"subject": subject, "from": sender, "date": date_str},
            )
        except Exception as exc:
            # One malformed message must not abort the rest of the mailbox.
            log.warning("Gmail scan: could not record bill for message %s: %s", msg_id, exc)
            continue
        created.append({"bill_id": bill_id, "vendor": vendor, "source": "gmail",
                        "has_document": bool(file_url)})

    log.info("Gmail scan: created %d new bills (%d with documents)",
             len(created), sum(1 for c in created if c.get("has_document")))
    return created


# ── Google Drive scanner ──────────────────────────────────────────────────────

def scan_drive(business: dict, all_businesses: list[dict], days: int = 45) -> list[dict]:
    """List invoice PDFs from ONE business's own Google Drive, record bill stubs.

    Drive binary download is not possible through the Maton JSON gateway,
    so we store the Drive webViewLink as file_url for in-browser viewing.
    Business assignment follows the same direct-tag rule as scan_gmail().
    """
    maton = _client_for_business(business)
    created: list[dict] = []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        data = _maton_data(resp, f"Drive search for '{business.get('slug')}'")
    except MatonError as exc:
        log.warning("Drive file list failed: %s", exc)
        raise

    files = data.get("files") or []
    if not files:
        return []

    # Drive is also the household's document archive, so a backfill would
    # otherwise sweep in years of historical invoices as live "pending" bills.
    # Skip anything the filename dates before the window the caller asked for.
    # This used to be a frozen literal ("2026-06-01") that silently aged into a
    # filter rejecting genuinely current bills, and it was applied to the
    # bare-year guess too — so "Acme Invoice 2026.pdf" resolved to 2026-01-01
    # and got dropped even when the file was uploaded last week.
    window_start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

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

        # Skip files the filename dates before the requested window — but only
        # on an exact DD MM YYYY, never on the bare-year guess.
        if from_date and from_date < window_start and _filename_date_is_exact(filename):
            log.debug("Skipping Drive file '%s' (dated %s, window starts %s)",
                      filename, from_date, window_start)
            continue

        vendor = _vendor_from_filename(filename)
        if business.get("slug") == "per":
            biz_id, confidence = guess_business(vendor, None, all_businesses)
        else:
            biz_id, confidence = business.get("business_id"), 1.0

        try:
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
        except Exception as exc:
            log.warning("Drive scan: could not record bill for '%s': %s", filename, exc)
            continue
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

_PAYMENT_SUBJECTS = (
    '(subject:receipt OR subject:"payment received" OR subject:"payment processed" '
    'OR subject:"payment confirmed" OR subject:"thank you for your payment" '
    'OR subject:"your payment" OR subject:"invoice paid")'
)

_VENDOR_STOPWORDS = frozenset({
    "the", "and", "for", "inc", "llc", "ltd", "corp", "co", "company",
    "invoice", "billing", "bill", "payments", "payment", "services", "service",
})

# One Gmail search per bill was an unbounded fan-out: 200 pending bills meant
# 200 sequential round trips inside one HTTP request — minutes of latency and a
# reliable way to hit Gmail's rate limit. Bills from the same vendor share a
# keyword, so search once per distinct keyword and apply the answer to all of
# them; then cap the total so a large backlog degrades instead of timing out.
MAX_PAYMENT_SEARCHES = 40


def _payment_keyword(vendor: str) -> str | None:
    """The most identifying word of a vendor name, or None if there isn't one.

    A one-word generic keyword is what makes this check dangerous: searching
    "Payment" or "Inc" against a mailbox matches almost any receipt.
    """
    words = [
        w.strip(".,&") for w in (vendor or "").split()
        if len(w.strip(".,&")) > 2 and w.strip(".,&").lower() not in _VENDOR_STOPWORDS
    ]
    return words[0] if words else None


def check_paid_bills(bills: list[dict], businesses: list[dict]) -> list[str]:
    """Search each business's own Gmail for payment confirmation emails
    matching its own pending bills.

    Returns list of bill_ids that were marked paid.
    Conservative: requires both an exact-phrase vendor keyword AND a
    payment-confirmation subject. Bills with no business assigned fall back to
    the personal/default key ("per"), matching the original behavior before
    per-business keys existed.
    """
    paid_ids: list[str] = []

    pending = [
        b for b in bills
        if b.get("status") in ("pending", "overdue", "due-soon")
    ]
    if not pending:
        return []

    by_slug = {b["slug"]: b for b in businesses if b.get("slug")}
    business_by_id = {
        str(b["business_id"]): b for b in businesses if b.get("business_id")
    }

    # Group pending bills by which business's Gmail account should be searched.
    groups: dict[str, list[dict]] = {}
    for bill in pending:
        biz = business_by_id.get(str(bill.get("business_id") or ""))
        slug = (biz or {}).get("slug") or "per"
        groups.setdefault(slug, []).append(bill)

    searches = 0
    for slug, group_bills in groups.items():
        business = by_slug.get(slug)
        try:
            maton = _client_for_business(business) if business else MatonClient()
        except ValueError as exc:
            log.info("check_paid_bills: skipping '%s' (%s)", slug, exc)
            continue

        # keyword -> bills sharing it, so each keyword costs one search.
        by_keyword: dict[str, list[dict]] = {}
        for bill in group_bills:
            keyword = _payment_keyword(bill.get("vendor") or "")
            if keyword:
                by_keyword.setdefault(keyword, []).append(bill)

        for keyword, keyword_bills in by_keyword.items():
            if searches >= MAX_PAYMENT_SEARCHES:
                log.info("check_paid_bills: hit the %d-search cap, stopping early",
                         MAX_PAYMENT_SEARCHES)
                return paid_ids

            # Quote the keyword: unquoted, Gmail tokenises it and a vendor like
            # "Blue Ridge" matched any mail containing "blue".
            query = f'"{keyword}" {_PAYMENT_SUBJECTS} newer_than:30d'
            searches += 1
            try:
                resp = maton.request(
                    "google-mail", "GET", "gmail/v1/users/me/messages",
                    query={"q": query, "maxResults": 3},
                )
                search = _maton_data(resp, f"Gmail payment search in '{slug}'")
            except MatonError as exc:
                # A disconnected mailbox returns an error body, not an empty
                # result. Treating the two the same would silently report
                # "no payments found" for an account we never actually searched.
                log.info("check_paid_bills: '%s' search failed (%s)", slug, exc)
                break

            messages = search.get("messages") or []
            if not messages:
                continue

            # One receipt cannot settle several outstanding invoices from the
            # same vendor. Marking all of them paid off a single confirmation
            # is how a real unpaid bill disappears from the list; leave an
            # ambiguous group alone for the human to resolve.
            if len(keyword_bills) > 1:
                log.info(
                    "check_paid_bills: %d open bills match '%s' in %s — leaving "
                    "them for manual review rather than guessing which was paid",
                    len(keyword_bills), keyword, slug,
                )
                continue

            bill = keyword_bills[0]
            if update_bill(bill["bill_id"], status="paid"):
                paid_ids.append(bill["bill_id"])
                log.info("Auto-marked bill %s (%s) as paid via Gmail confirmation (%s)",
                         bill["bill_id"], bill.get("vendor"), slug)

    return paid_ids
