"""Drive → S3 sync — the code-driven Job that mirrors every connected business's
Google Drive into the S3 locker, filed into category folders for navigation.

Registered in hermes/job_runner.py as `drive_s3_sync`. Contract is the Jobs
signature `run(config, cancel_check) -> dict` (like reviews_scraper.run): a daily
cron or a manual Start fires it, it returns a JSON summary stored on the run row.

For each business with a configured Maton key it pages Drive, skips files already
in the sync ledger (hermes/db/drive_sync.py) that haven't changed, downloads the
rest through Maton (exporting Google-native Docs/Sheets/Slides to PDF), asks Claude
to file each into a fixed category, and stores it at a DETERMINISTIC S3 key
`drive-files/{slug}/{category}/{file_id}-{name}` so a re-sync overwrites in place
instead of duplicating. Every file is recorded in the ledger, whose unique index
(business_slug, drive_file_id) is the guarantee against duplicates / double-filing.

Bounded per run (max_files_per_run, split across businesses) so a huge first sweep
drains over several runs rather than blowing a serverless time limit — each run
skips what's already synced and copies the next batch.
"""
from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from . import credits, storage
from .config import Config
from .db import drive_sync as ledger
from .llm_json import call_claude_json
from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.drive_sync")

# ── Tunables (all overridable via the job's config jsonb) ────────────────────
_DEFAULT_MAX_FILES = 150          # copied files per run, split across businesses
_DEFAULT_MAX_FILE_MB = 40         # skip files larger than this (serverless memory)
_DEFAULT_CLASSIFY_BATCH = 50      # files per classification LLM call
_DEFAULT_CONCURRENCY = 4          # parallel download+upload workers per business
_PAGE_SIZE = 200                  # Drive files.list page (< Maton's 500-item cap)
_CLASSIFY_MODEL = "claude-haiku-4-5-20251001"  # cheap, batched file classifier

# The fixed, navigable taxonomy. The classifier is constrained to exactly these,
# so category folders stay consistent across runs and businesses.
_CATEGORIES = [
    "Invoices", "Receipts", "Contracts", "Financial", "Legal", "HR",
    "Marketing", "Reports", "Correspondence", "Images", "Spreadsheets", "Other",
]
_CATEGORY_LOOKUP = {c.lower(): c for c in _CATEGORIES}

_FOLDER_MIME = "application/vnd.google-apps.folder"
# Google-native types we export to PDF (mimeType -> (export mime, extension)).
_EXPORT_MIMES = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

_FILE_FIELDS = (
    "nextPageToken, files(id,name,mimeType,modifiedTime,md5Checksum,size,"
    "parents,webViewLink)"
)


# ── small helpers ────────────────────────────────────────────────────────────
def _int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        v = int(config.get(key))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _safe_segment(s: str, limit: int = 120) -> str:
    """A string reduced to a safe S3 path / filename segment (no separators)."""
    s = (s or "").strip()
    s = re.sub(r"[^\w\-. ()]+", "_", s)   # keep word chars, dash, dot, space, parens
    s = re.sub(r"_{2,}", "_", s).strip(" _")
    return (s[:limit].strip(" _") or "file")


def _safe_category(raw: str | None) -> str:
    """Force a model-returned label into the fixed taxonomy (else 'Other')."""
    return _CATEGORY_LOOKUP.get((raw or "").strip().lower(), "Other")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _unchanged(prev: dict[str, Any], f: dict[str, Any]) -> bool:
    """Whether a file is unchanged since its last sync — so it can be skipped.

    Prefers md5Checksum (exact, present for real binaries); falls back to
    modifiedTime for Google-native files (which have no checksum). A previously
    errored row is always treated as changed so it gets retried."""
    if prev.get("status") == "error":
        return False
    md5 = f.get("md5Checksum")
    if md5 and prev.get("drive_md5"):
        return md5 == prev["drive_md5"]
    a, b = _parse_dt(prev.get("drive_modified_time")), _parse_dt(f.get("modifiedTime"))
    return bool(a and b and a == b)


def _folder_name(client: MatonClient, folder_id: str | None, cache: dict[str, str]) -> str:
    """Resolve a Drive folder id to its name (cached per run), for classification
    context. Best-effort — an unreachable parent just yields ''."""
    if not folder_id:
        return ""
    if folder_id in cache:
        return cache[folder_id]
    name = ""
    try:
        r = client.request(
            "google-drive", "GET", f"drive/v3/files/{folder_id}",
            query={"fields": "name", "supportsAllDrives": "true"},
        )
        if r.get("ok"):
            name = (r.get("data") or {}).get("name", "") or ""
    except MatonError as exc:
        log.debug("folder name %s failed: %s", folder_id, exc)
    cache[folder_id] = name
    return name


def _classify_batch(items: list[dict[str, Any]]) -> dict[str, str]:
    """Classify a batch of files into the fixed taxonomy in one LLM call.

    `items` are {id, name, mimeType, folder}. Returns {file_id: category};
    anything the model omits or mislabels falls back to 'Other'."""
    if not items:
        return {}
    lines = [
        f'- id="{it["id"]}" name="{it.get("name", "")}" '
        f'type="{it.get("mimeType", "")}" drive_folder="{it.get("folder", "")}"'
        for it in items
    ]
    system = (
        "You file business documents into folders. Assign each file to exactly ONE "
        "of these categories: " + ", ".join(_CATEGORIES) + ". Use the filename, MIME "
        "type, and the Drive folder it lives in as clues. Prefer a specific category "
        "over 'Other' when reasonable (e.g. a bill/invoice PDF -> Invoices, a signed "
        "agreement -> Contracts, a photo/scan image -> Images, a bank/tax document -> "
        "Financial). Respond with ONLY a JSON object mapping each file id to its "
        'category, e.g. {"<id>": "Invoices", "<id2>": "Contracts"}. No prose.'
    )
    result = call_claude_json(
        system=system,
        user_message="Classify these files:\n" + "\n".join(lines),
        max_tokens=1200,
        fallback_key="_raw",
        model=_CLASSIFY_MODEL,
    )
    if "_raw" in result and len(result) == 1:  # parse failed → all Other
        return {it["id"]: "Other" for it in items}
    return {it["id"]: _safe_category(result.get(it["id"])) for it in items}


# ── Drive listing / download ─────────────────────────────────────────────────
def _list_page(
    client: MatonClient, page_token: str | None, all_drives: bool
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "fields": _FILE_FIELDS,
        "pageSize": str(_PAGE_SIZE),
        "q": "trashed = false",
    }
    if all_drives:
        query.update({
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        })
    if page_token:
        query["pageToken"] = page_token
    return client.request("google-drive", "GET", "drive/v3/files", query=query)


def _download(client: MatonClient, file_id: str, mime: str) -> tuple[bytes, str, bool]:
    """Fetch a file's bytes through Maton. Google-native types are exported to PDF.

    Returns (raw_bytes, effective_mime, exported_pdf). Raises MatonError-ish via a
    RuntimeError on a non-ok download so the caller records it as an error row."""
    if mime in _EXPORT_MIMES:
        export_mime, _ext = _EXPORT_MIMES[mime]
        dl = client.download_binary(
            "google-drive", "GET", f"drive/v3/files/{file_id}/export",
            query={"mimeType": export_mime, "supportsAllDrives": "true"},
        )
        if not dl.get("ok"):
            raise RuntimeError(f"export failed (status {dl.get('status')})")
        return dl.get("raw") or b"", export_mime, True

    dl = client.download_binary(
        "google-drive", "GET", f"drive/v3/files/{file_id}",
        query={"alt": "media", "supportsAllDrives": "true"},
    )
    if not dl.get("ok"):
        raise RuntimeError(f"download failed (status {dl.get('status')})")
    return dl.get("raw") or b"", (mime or "application/octet-stream"), False


def _process_one(
    slug: str, client: MatonClient, cand: dict[str, Any], category: str,
    max_file_bytes: int,
) -> dict[str, Any]:
    """Download one candidate, put it in S3 under its category, record the ledger
    row, and RETURN an outcome dict (no shared state) so callers can run this
    concurrently and tally the results themselves.

    Outcome: {"outcome": "synced"|"updated"|"restored"|"skipped"|"error",
              "exported": bool}. `reason` on the candidate ("new"/"changed"/
              "missing_s3") decides which success label a stored file gets."""
    fid, name, mime = cand["id"], cand.get("name", "untitled"), cand.get("mimeType", "")
    reason = cand.get("reason", "new")
    success_label = {"new": "synced", "changed": "updated", "missing_s3": "restored"}.get(
        reason, "updated")
    base = {
        "business_slug": slug, "drive_file_id": fid, "drive_name": name,
        "mime_type": mime, "drive_modified_time": cand.get("modifiedTime"),
        "drive_md5": cand.get("md5Checksum"), "category": category,
        "metadata": {"web_view_link": cand.get("webViewLink")},
    }
    tag = {"name": name, "category": category}
    try:
        raw, put_mime, exported = _download(client, fid, mime)
    except Exception as exc:  # noqa: BLE001 — record the failure, keep going
        ledger.upsert_synced(**base, status="error", error=str(exc)[:500])
        return {"outcome": "error", "exported": False, **tag}

    if len(raw) > max_file_bytes:
        ledger.upsert_synced(
            **base, size_bytes=len(raw), exported_pdf=exported, status="skipped",
            error=f"exceeds {max_file_bytes // (1024 * 1024)}MB limit",
        )
        return {"outcome": "skipped", "exported": exported, **tag}

    ext = ".pdf" if exported else (f".{name.rsplit('.', 1)[-1]}" if "." in name else "")
    safe_name = _safe_segment(name.rsplit(".", 1)[0] if "." in name else name)
    key = f"drive-files/{slug}/{_safe_segment(category, 60)}/{fid}-{safe_name}{ext}"
    s3_uri = storage.put(key, raw, put_mime)

    ledger.upsert_synced(
        **base, s3_key=key, s3_uri=s3_uri, size_bytes=len(raw),
        exported_pdf=exported, status="synced",
    )
    return {"outcome": success_label, "exported": exported, **tag}


# ── per-business sweep ───────────────────────────────────────────────────────
def _collect_candidates(
    slug: str, client: MatonClient, budget: int, smap: dict[str, dict[str, Any]],
    s3_present: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    """Page the whole Drive ONCE and return up to `budget` files that need copying.

    A file is a candidate when it's new, changed since last sync, OR its ledger row
    says 'synced' but the S3 object is missing (deleted out from under us). This is
    where dedup happens — so classification/downloads only touch real work. Returns
    (candidates, {scanned, unchanged}, list_error)."""
    candidates: list[dict[str, Any]] = []
    scanned = unchanged = 0
    list_error = False
    page_token: str | None = None
    all_drives = True

    while True:
        resp = _list_page(client, page_token, all_drives)
        if not resp.get("ok"):
            # Some Drive connections reject the allDrives params — retry the first
            # page against the user's own Drive before giving up.
            if all_drives and page_token is None:
                all_drives = False
                continue
            list_error = True
            log.warning("Drive list for %s failed: status %s", slug, resp.get("status"))
            break

        data = resp.get("data") or {}
        for f in data.get("files") or []:
            mime = f.get("mimeType") or ""
            if mime == _FOLDER_MIME:
                continue
            if mime.startswith("application/vnd.google-apps.") and mime not in _EXPORT_MIMES:
                continue  # forms, scripts, shortcuts, … — not syncable bytes
            scanned += 1

            prev = smap.get(f["id"])
            if prev and _unchanged(prev, f):
                # Content unchanged — but re-copy if the DB says it's in S3 and it
                # isn't actually there anymore. (skipped/error rows have no object
                # to verify, so leave them to the change check.)
                if prev.get("status") == "synced" and prev.get("s3_key") \
                        and prev["s3_key"] not in s3_present:
                    f["reason"] = "missing_s3"
                else:
                    unchanged += 1
                    continue
            else:
                f["reason"] = "changed" if prev is not None else "new"

            candidates.append(f)
            if len(candidates) >= budget:
                return candidates, {"scanned": scanned, "unchanged": unchanged}, list_error

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return candidates, {"scanned": scanned, "unchanged": unchanged}, list_error


_ADDED_OUTCOMES = {"synced", "updated", "restored"}


def _sync_business(
    slug: str, client: MatonClient, budget: int, max_file_bytes: int,
    classify_batch: int, concurrency: int, cancel_check: Callable[[], bool] | None,
) -> tuple[dict[str, int], list[dict[str, str]], bool]:
    """Sweep one business's Drive up to `budget` copied files, in three phases:
    (1) list once + dedup (incl. S3-presence) → candidates, (2) classify the whole
    set in one pass (minimal LLM calls), (3) download+upload, optionally in parallel.
    Returns (counts, added_files, cancelled) — added_files lists every file copied
    this run as {name, category, outcome}."""
    counts = {"scanned": 0, "synced": 0, "updated": 0, "restored": 0, "skipped": 0,
              "exported": 0, "errors": 0, "unchanged": 0}
    added: list[dict[str, str]] = []

    # Phase 1 — one Drive sweep + dedup against the ledger AND the actual S3 keys.
    smap = ledger.synced_map(slug)
    s3_present = {o["key"] for o in storage.list_objects(f"drive-files/{slug}/")}
    candidates, scan_counts, list_error = _collect_candidates(
        slug, client, budget, smap, s3_present)
    counts["scanned"], counts["unchanged"] = scan_counts["scanned"], scan_counts["unchanged"]
    if list_error:
        counts["errors"] += 1
    if not candidates:
        return counts, added, False

    # Phase 2 — classify the entire candidate set (one LLM call per classify_batch).
    folder_cache: dict[str, str] = {}
    cats: dict[str, str] = {}
    for start in range(0, len(candidates), classify_batch):
        chunk = candidates[start:start + classify_batch]
        for c in chunk:
            c["folder"] = _folder_name(client, (c.get("parents") or [None])[0], folder_cache)
        cats.update(_classify_batch(chunk))

    # Phase 3 — download + upload. Parallel when concurrency > 1; the DB pool and
    # requests/boto3 clients are all thread-safe, and _process_one shares no state.
    cancelled = False

    def _work(c: dict[str, Any]) -> dict[str, Any]:
        return _process_one(slug, client, c, cats.get(c["id"], "Other"), max_file_bytes)

    def _tally(res: dict[str, Any]) -> None:
        # _process_one reports the SINGULAR "error"; the counter key is "errors".
        # Any unrecognised label is counted there too rather than raising: a
        # KeyError here escapes into run()'s per-business handler, which drops
        # the whole sweep — so one unreadable file would strand every file
        # behind it, and report the business as 0 synced.
        counts[res["outcome"] if res["outcome"] in counts else "errors"] += 1
        if res.get("exported"):
            counts["exported"] += 1
        if res["outcome"] in _ADDED_OUTCOMES:
            added.append({"name": res.get("name", ""), "category": res.get("category", ""),
                          "outcome": res["outcome"]})

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {}
            for c in candidates:
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                futures[pool.submit(_work, c)] = c
            for fut in as_completed(futures):
                _tally(fut.result())
    else:
        for c in candidates:
            if cancel_check and cancel_check():
                cancelled = True
                break
            _tally(_work(c))

    return counts, added, cancelled


# ── entry point ──────────────────────────────────────────────────────────────
def run(config: dict[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Sync every connected business's Drive to S3. Returns a summary dict.

    config keys (all optional):
      businesses         — explicit slug list; default Config.connected_business_slugs()
      max_files_per_run  — total files copied this run, split across businesses (150)
      max_file_mb        — skip files bigger than this (40)
      classify_batch     — files per classification LLM call (50)
      concurrency        — parallel download+upload workers per business (4)
    """
    config = config or {}
    if not storage.enabled():
        return {"status": "error", "error": "S3 is not configured (S3_BUCKET unset)."}

    slugs = config.get("businesses") or Config.connected_business_slugs()
    slugs = [s for s in slugs if Config.maton_key_for_slug(s)]
    if not slugs:
        return {"status": "error", "error": "No businesses with a configured Maton key."}

    max_files = _int(config, "max_files_per_run", _DEFAULT_MAX_FILES)
    max_file_bytes = _int(config, "max_file_mb", _DEFAULT_MAX_FILE_MB) * 1024 * 1024
    classify_batch = _int(config, "classify_batch", _DEFAULT_CLASSIFY_BATCH)
    concurrency = _int(config, "concurrency", _DEFAULT_CONCURRENCY)
    per_business = max(1, math.ceil(max_files / len(slugs)))

    _ADDED_FILES_CAP = 300   # keep the run row's jsonb bounded

    by_business: dict[str, dict[str, int]] = {}
    added_by_business: dict[str, int] = {}   # files copied this run, per business
    added_files: list[dict[str, str]] = []   # {business, name, category, outcome}
    categories: set[str] = set()
    cancelled = False

    for slug in slugs:
        if cancel_check and cancel_check():
            cancelled = True
            break
        try:
            client = MatonClient(api_key=Config.maton_key_for_slug(slug))
            counts, added, biz_cancelled = _sync_business(
                slug, client, per_business, max_file_bytes, classify_batch,
                concurrency, cancel_check,
            )
        except credits.CreditsExhausted as exc:
            # Every remaining business would fail the same way for the same
            # reason, so stop rather than logging the identical billing error
            # once per business.
            log.warning("sync stopped at %s: no API credit", slug)
            by_business[slug] = {"error": str(exc)}
            cancelled = True
            break
        except Exception as exc:  # noqa: BLE001 — one business failing shouldn't sink the run
            log.warning("sync for %s failed: %s", slug, exc)
            by_business[slug] = {"error": str(exc)[:300]}
            continue
        by_business[slug] = counts
        added_by_business[slug] = len(added)
        for a in added:
            categories.add(a["category"])
            if len(added_files) < _ADDED_FILES_CAP:
                added_files.append({"business": slug, **a})
        if biz_cancelled:
            cancelled = True
            break

    totals = {k: 0 for k in ("scanned", "synced", "updated", "restored",
                             "skipped", "errors", "unchanged")}
    for counts in by_business.values():
        for k in totals:
            totals[k] += counts.get(k, 0)
    added_total = totals["synced"] + totals["updated"] + totals["restored"]

    return {
        "status": "cancelled" if cancelled else "success",
        "cancelled": cancelled,
        "businesses": slugs,
        "per_business_budget": per_business,
        "added_total": added_total,
        "added_by_business": added_by_business,
        "added_files": added_files,
        "added_files_truncated": added_total > len(added_files),
        "categories": sorted(categories),
        "totals": totals,
        "by_business": by_business,
    }
