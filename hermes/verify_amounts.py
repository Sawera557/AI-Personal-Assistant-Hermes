"""Resolve the register's "AMOUNT IN PDF - UNVERIFIED" lines from stored documents.

The Sheet records an amount as UNVERIFIED when it exists only inside a PDF
nobody has opened. Hermes already holds many of those PDFs in S3 (captured by
the Gmail scan) and has an extraction pipeline, so it can read them and offer
the figure — with the document it came from attached.

It offers. It does not decide. Every result is written to `amount_proposals`
with its evidence and waits for a human; the Sheet's no-estimation rule is the
whole point of the register and this must not erode it. Concretely:

  * a proposal is only created when a document actually yielded an amount,
  * the matched document is recorded so the approver can open it,
  * a weak vendor match is reported, not quietly accepted,
  * approving writes a CHANGE LOG row in the Sheet alongside the cell edit.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from . import register as reg
from . import storage
from .bills import extract_bill
from .db.connection import cursor

log = logging.getLogger("hermes.verify_amounts")


# Register "Entity / Mailbox" values -> Hermes business slug. The register uses
# human names ("AFK / Afro Farm"); bills carry a business_id. Anything absent
# here simply contributes no entity signal to the match.
ENTITY_TO_SLUG = {
    "afk": "afk",
    "afk / afro farm": "afk",
    "personal": "per",
    "gak properties": "gak",
    "gak commodities": "gakc",
    "gif properties": "gif",
    "nour hammam": "nh",
}

# Words that carry no identifying weight when comparing a register vendor to a
# bill vendor — matching on these alone produces false pairings.
_NOISE = {
    "inc", "llc", "ltd", "corp", "co", "company", "the", "and", "plc", "pbc",
    "bill", "bills", "invoice", "invoices", "statement", "payment", "payments",
    "subscription", "account", "services", "service", "group", "intl",
}

MIN_MATCH_SCORE = 0.34

# A proposal has to clear this to be offered at all. Below it the line is
# reported as needing a human, which is the honest answer.
MIN_CONFIDENCE = 0.75

# Vendor name alone is not identity. The register distinguishes lines by what is
# in "What it is": a domain, an account number, a plan name, a mailbox. Three
# separate Google Workspace subscriptions share a vendor and differ only there,
# and a first pass happily proposed one Enterprise Plus invoice as the evidence
# for all three. A document that cannot be tied to a specific line by one of
# these identifiers is ambiguous, and ambiguous must mean "ask a human".
_DISCRIMINATOR_PATTERNS = [
    r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|ai|co|ke|co\.ke)\b",   # domains
    # Require a real separator after the keyword, or "invoice" splits into
    # "inv" + "oice" and offers "oice" as an identifying token.
    r"\b(?:acc|acct|account|inv|invoice)[-#\s]+([a-z0-9][a-z0-9-]{3,})\b",
    r"\b\d{5,}\b",                                                    # account numbers
    r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\b",                                # mailboxes
]

# Plan/tier words that meaningfully separate two lines of the same vendor.
_PLAN_WORDS = {
    "enterprise", "business", "premium", "standard", "starter", "pro", "plus",
    "basic", "max", "team", "personal", "residential", "commercial", "yearly",
    "monthly", "annual",
}


def _tokens(name: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", (name or "").lower())
        if len(t) > 2 and t not in _NOISE
    }


def match_score(register_vendor: str, bill_vendor: str) -> float:
    """Jaccard-ish overlap between two vendor names, 0.0 - 1.0."""
    a, b = _tokens(register_vendor), _tokens(bill_vendor)
    if not a or not b:
        return 0.0
    overlap = a & b
    if not overlap:
        return 0.0
    # Favour the smaller set: "Starlink" fully inside "Starlink Internet
    # Services" should score high, not be diluted by the longer name.
    return len(overlap) / min(len(a), len(b))


def discriminators(line: dict[str, Any]) -> set[str]:
    """Identifiers that distinguish this register line from its siblings."""
    text = f"{line.get('what_it_is','')} {line.get('source_mailbox','')}".lower()
    out: set[str] = set()
    for pat in _DISCRIMINATOR_PATTERNS:
        for m in re.findall(pat, text):
            token = (m if isinstance(m, str) else m[0]).strip(" -#@.")
            if len(token) >= 4:
                out.add(token)
    out |= {w for w in re.split(r"[^a-z]+", text) if w in _PLAN_WORDS}
    return out


def document_blob(extracted: dict[str, Any]) -> str:
    """Everything we managed to read from a document, as one searchable string."""
    parts = [
        str(extracted.get("vendor") or ""),
        str(extracted.get("invoice_number") or ""),
        str(extracted.get("category") or ""),
    ]
    for li in extracted.get("line_items") or []:
        parts.append(str((li or {}).get("description") or ""))
    raw = extracted.get("raw_extracted")
    if isinstance(raw, dict):
        try:
            parts.append(json.dumps(raw)[:6000])
        except (TypeError, ValueError):
            pass
    return " ".join(parts).lower()


def extraction_looks_sane(extracted: dict[str, Any]) -> bool:
    """Reject a reading that is clearly OCR noise rather than an invoice.

    A scanned photo can yield descriptions like 'MΣpΣTpΞ M3HĐáŸ s I t W' and a
    number that is not an amount at all. Proposing that against a register line
    would put a fabricated figure in front of an approver wearing the costume of
    evidence.
    """
    amount = extracted.get("amount")
    if amount is None or amount <= 0 or amount > 10_000_000:
        return False
    descs = [
        str((li or {}).get("description") or "")
        for li in (extracted.get("line_items") or [])
    ]
    descs = [d for d in descs if d.strip()]
    if descs:
        # Mostly-unreadable descriptions mean the page did not OCR cleanly.
        legible = sum(
            1 for d in descs
            if len(re.findall(r"[a-zA-Z0-9 ]", d)) / max(len(d), 1) > 0.8
        )
        if legible / len(descs) < 0.5:
            return False
    return True


def corroborates_register(line: dict[str, Any], extracted: dict[str, Any]) -> bool:
    """True when the register's own 'Currency as billed' text already contains
    the figure we just read — the strongest confirmation available, because it
    means a human and the extractor independently agree."""
    billed = (line.get("currency_as_billed") or "").replace(",", "").upper()
    amount = extracted.get("amount")
    if amount is None or not billed:
        return False
    return f"{amount:.2f}" in billed or f"{amount:.0f}" in billed


def _candidate_documents() -> list[dict[str, Any]]:
    """Bills that hold a document we can actually read.

    Drive bills store a webViewLink, not bytes — the Maton JSON gateway cannot
    download Drive binaries — so only S3-backed rows are usable here.
    """
    with cursor(commit=False) as cur:
        cur.execute(
            """SELECT b.bill_id, b.vendor, b.file_url, b.source, b.source_ref,
                      b.business_id, bz.slug, b.created_at
               FROM bills b
               LEFT JOIN businesses bz USING (business_id)
               WHERE b.file_url LIKE 's3://%'
               ORDER BY b.created_at DESC;"""
        )
        cols = ["bill_id", "vendor", "file_url", "source", "source_ref",
                "business_id", "slug", "created_at"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def find_documents_for(line: dict[str, Any], docs: list[dict[str, Any]]) -> list[dict]:
    """Documents that plausibly evidence one register line, best first."""
    want_slug = ENTITY_TO_SLUG.get((line.get("entity") or "").strip().lower())
    scored = []
    for d in docs:
        score = match_score(line.get("vendor", ""), d.get("vendor") or "")
        if score < MIN_MATCH_SCORE:
            continue
        # The entity is corroboration, not a gate: a bill can sit under the
        # wrong business (guess_business is only a guess) and still be the right
        # document, so a mismatch costs confidence instead of discarding it.
        if want_slug and d.get("slug"):
            score = score * (1.0 if d["slug"] == want_slug else 0.72)
        scored.append({**d, "score": round(score, 3)})
    scored.sort(key=lambda d: (-d["score"], d["created_at"]), reverse=False)
    scored.sort(key=lambda d: -d["score"])
    return scored


def _extract_from(
    doc: dict[str, Any], cache: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Read one stored document, or None if it cannot be read.

    `cache` is keyed by file_url and shared across a sweep: several register
    lines legitimately point at the same document (three Google Workspace
    subscriptions bill to one receipt), and each extraction is an OCR plus an
    LLM call. Reading the same PDF once per line would triple the cost for no
    new information.
    """
    url = doc["file_url"]
    if cache is not None and url in cache:
        return cache[url]

    result: dict[str, Any] | None = None
    try:
        raw, ctype = storage.get_bytes(url)
        if raw:
            name = url.rsplit("/", 1)[-1]
            result = extract_bill(name, raw, ctype or "application/pdf")
    except Exception as exc:
        log.info("verify: could not read %s: %s", url, exc)
        result = None

    if cache is not None:
        cache[url] = result
    return result


def _as_date(v: Any) -> str | None:
    s = str(v or "").strip()
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


def score_pair(
    line: dict[str, Any], doc: dict[str, Any], extracted: dict[str, Any]
) -> tuple[float, list[str]]:
    """Confidence that `doc` evidences `line`, plus the reasons for it."""
    reasons: list[str] = []
    confidence = doc["score"] * 0.55          # vendor/entity similarity
    reasons.append(f"vendor match {doc['score']:.2f}")

    blob = document_blob(extracted)
    hits = sorted(d for d in discriminators(line) if d in blob)
    if hits:
        confidence += 0.35
        reasons.append("identifiers in document: " + ", ".join(hits[:4]))
    else:
        reasons.append("no identifier from 'What it is' found in the document")

    if corroborates_register(line, extracted):
        confidence += 0.30
        reasons.append("figure matches the register's 'Currency as billed'")

    return min(confidence, 1.0), reasons


def verify_line(
    line: dict[str, Any],
    docs: list[dict[str, Any]] | None = None,
    cache: dict[str, Any] | None = None,
    claimed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Try to resolve one register line.

    `claimed` maps file_url -> register ref for documents already assigned in
    this sweep. One receipt cannot be the evidence for several distinct
    subscriptions, so a document is spent once; a later line that wants it is
    reported as ambiguous rather than handed the same figure.
    """
    docs = docs if docs is not None else _candidate_documents()
    ref = str(line.get("ref"))
    base = {"ref": ref, "vendor": line.get("vendor")}
    candidates = find_documents_for(line, docs)
    if not candidates:
        return {**base, "outcome": "no_document"}

    best: dict[str, Any] | None = None
    notes: list[str] = []
    for doc in candidates[:3]:          # best few only; extraction is not free
        extracted = _extract_from(doc, cache)
        if not extracted:
            continue
        if not extraction_looks_sane(extracted):
            notes.append(f"{doc['file_url'].rsplit('/', 1)[-1]}: unreadable scan")
            continue
        confidence, reasons = score_pair(line, doc, extracted)
        if claimed is not None and doc["file_url"] in claimed and claimed[doc["file_url"]] != ref:
            notes.append(
                f"best document is already the evidence for Ref#{claimed[doc['file_url']]}"
            )
            continue
        if best is None or confidence > best["confidence"]:
            best = {"doc": doc, "extracted": extracted,
                    "confidence": confidence, "reasons": reasons}

    if best is None:
        return {**base, "outcome": "ambiguous", "reasons": notes or ["nothing readable"]}

    if best["confidence"] < MIN_CONFIDENCE:
        return {
            **base, "outcome": "needs_review",
            "confidence": round(best["confidence"], 2),
            "would_propose": best["extracted"].get("amount"),
            "currency": best["extracted"].get("currency"),
            "reasons": best["reasons"] + notes,
        }

    proposal_id = _store_proposal(
        line, best["doc"], best["extracted"], best["confidence"], best["reasons"],
    )
    if claimed is not None:
        claimed[best["doc"]["file_url"]] = ref
    return {
        **base,
        "outcome": "proposed",
        "proposal_id": proposal_id,
        "amount": best["extracted"]["amount"],
        "currency": best["extracted"].get("currency"),
        "confidence": round(best["confidence"], 2),
        "reasons": best["reasons"],
        "evidence_bill_id": str(best["doc"]["bill_id"]),
    }


def _store_proposal(
    line: dict, doc: dict, extracted: dict,
    confidence: float, reasons: list[str],
) -> str:
    """File a proposal, superseding any earlier pending one for this line."""
    raw = extracted.get("raw_extracted") or {}
    extractor = "ocr-fallback" if isinstance(raw, dict) and raw.get("_extracted_by") else "nuextract"
    extractor = f"{extractor} ({'; '.join(reasons)})"
    proposal_id = str(uuid.uuid4())
    with cursor() as cur:
        cur.execute(
            "UPDATE amount_proposals SET status='superseded', decided_at=now() "
            "WHERE register_ref=%s AND status='pending';",
            (str(line["ref"]),),
        )
        cur.execute(
            """INSERT INTO amount_proposals
                 (proposal_id, register_ref, register_row, vendor, entity,
                  previous_value, proposed_amount, proposed_currency,
                  invoice_number, issue_date, due_date, line_items,
                  evidence_bill_id, evidence_file_url, evidence_source,
                  extractor, match_score)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
            (
                proposal_id, str(line["ref"]), int(line["row_number"]),
                line.get("vendor"), line.get("entity"), line.get("amount_usd"),
                extracted.get("amount"), extracted.get("currency"),
                extracted.get("invoice_number"),
                _as_date(extracted.get("issue_date")), _as_date(extracted.get("due_date")),
                json.dumps(extracted.get("line_items") or []),
                doc["bill_id"], doc["file_url"], doc.get("source_ref"),
                extractor, round(confidence, 3),
            ),
        )
    return proposal_id


def verify_unverified(limit: int = 25, only_ref: str | None = None) -> dict[str, Any]:
    """Sweep the register's unverified lines and file proposals.

    `limit` bounds how many lines are attempted in one run — each one can mean
    an OCR call and an LLM call, so an unbounded sweep would be slow and costly.
    """
    lines = reg.fetch_register()
    targets = [l for l in lines if l.get("amount_unverified")]
    if only_ref:
        targets = [l for l in lines if str(l["ref"]) == str(only_ref)]
    total_unverified = sum(1 for l in lines if l.get("amount_unverified"))

    docs = _candidate_documents()
    cache: dict[str, Any] = {}
    claimed: dict[str, str] = {}

    # Lines carrying their own identifiers go first, so a document is claimed by
    # the line that can actually prove it owns it rather than by whichever
    # same-vendor line the register happens to list first.
    targets = sorted(targets, key=lambda l: -len(discriminators(l)))[:limit]
    results = [verify_line(l, docs, cache, claimed) for l in targets]

    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {
        "checked": len(results),
        "unverified_total": total_unverified,
        "proposed": counts.get("proposed", 0),
        "needs_review": counts.get("needs_review", 0),
        "ambiguous": counts.get("ambiguous", 0),
        "no_document": counts.get("no_document", 0),
        "results": results,
    }


# ── Proposals ────────────────────────────────────────────────────────────────

_PROPOSAL_COLS = [
    "proposal_id", "register_ref", "register_row", "vendor", "entity",
    "previous_value", "proposed_amount", "proposed_currency", "invoice_number",
    "issue_date", "due_date", "line_items", "evidence_bill_id",
    "evidence_file_url", "evidence_source", "extractor", "match_score",
    "status", "decided_at", "decided_note", "created_at",
]


def list_proposals(status: str | None = "pending") -> list[dict]:
    from .db.bills import _clean
    with cursor(commit=False) as cur:
        if status:
            cur.execute(
                f"SELECT {', '.join(_PROPOSAL_COLS)} FROM amount_proposals "
                "WHERE status=%s ORDER BY created_at DESC;", (status,),
            )
        else:
            cur.execute(
                f"SELECT {', '.join(_PROPOSAL_COLS)} FROM amount_proposals "
                "ORDER BY created_at DESC LIMIT 200;"
            )
        return [_clean(dict(zip(_PROPOSAL_COLS, r))) for r in cur.fetchall()]


def get_proposal(proposal_id: str) -> dict | None:
    from .db.bills import _clean
    with cursor(commit=False) as cur:
        cur.execute(
            f"SELECT {', '.join(_PROPOSAL_COLS)} FROM amount_proposals WHERE proposal_id=%s;",
            (proposal_id,),
        )
        row = cur.fetchone()
        return _clean(dict(zip(_PROPOSAL_COLS, row))) if row else None


def decide(proposal_id: str, approve: bool, note: str | None = None) -> dict:
    """Approve or reject a proposal.

    Approving is the ONLY path that writes to the register, and it writes twice:
    the Amount cell, and a CHANGE LOG row carrying the evidence. If the Sheet
    write fails the proposal stays pending, so the two never drift apart.
    """
    p = get_proposal(proposal_id)
    if not p:
        raise ValueError("No such proposal.")
    if p["status"] != "pending":
        raise ValueError(f"This proposal is already {p['status']}.")

    if approve:
        amount = p["proposed_amount"]
        currency = (p["proposed_currency"] or "USD").upper()
        cell = f"{amount:,.2f}" if currency == "USD" else f"{currency} {amount:,.2f}"
        evidence = _evidence_note(p)
        reg.write_amount(p["register_row"], cell)
        try:
            reg.append_change_log(
                ref=p["register_ref"], field="Amount (USD)",
                old=p["previous_value"] or "UNVERIFIED", new=cell, evidence=evidence,
            )
        except reg.RegisterError as exc:
            # The cell is already updated; a missing log line is recoverable and
            # must not leave the proposal looking undecided.
            log.error("Register cell updated but CHANGE LOG write failed: %s", exc)

    with cursor() as cur:
        cur.execute(
            "UPDATE amount_proposals SET status=%s, decided_at=now(), decided_note=%s "
            "WHERE proposal_id=%s;",
            ("approved" if approve else "rejected", note, proposal_id),
        )
    return get_proposal(proposal_id)


def _evidence_note(p: dict) -> str:
    bits = [f"Read from stored document by Hermes ({p.get('extractor') or 'extraction'})"]
    if p.get("invoice_number"):
        bits.append(f"invoice {p['invoice_number']}")
    if p.get("evidence_source"):
        bits.append(f"gmail msg {p['evidence_source']}")
    if p.get("evidence_file_url"):
        bits.append(str(p["evidence_file_url"]))
    return "; ".join(bits)
