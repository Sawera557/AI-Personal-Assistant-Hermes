"""Google-reviews scraper — the first code-driven Job (see hermes/job_runner.py).

Gathers Google reviews of hammams & spas across three equal-weight buckets —
Minnesota, Somalia, and a generic "general" search — labels each review top /
worst / neutral by its star rating, and writes them to a date-named CSV in S3,
appending if that day's file already exists.

Built for VOLUME within a hard budget:
* **Wide place discovery** — several queries per bucket (cities + spa-type terms)
  surface many distinct places, paginated via next_page_token.
* **Deep review pagination** — each place's reviews are paged (highest- and
  lowest-rated passes) to pull far more than the first page.
* **Dedup** — the same review reached via two sorts/pages is counted once.
* **Budget + target caps** — a run stops at `target_per_bucket` reviews OR when
  `max_searches` (a hard per-run SerpAPI budget) is spent, whichever comes first,
  so it can never silently blow the plan's monthly quota.

Provider is pluggable (SerpAPI today): swapping providers is confined to
`find_places` / `fetch_reviews`. Reaching very large targets needs a paid SerpAPI
plan — the free tier is 250 searches/mo (~2k reviews/mo total) — and is bounded
by what Google actually has (Somalia in particular is sparse).
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import storage
from .config import Config

log = logging.getLogger("hermes.reviews_scraper")


class ReviewsError(RuntimeError):
    """A hard failure that should fail the whole job (e.g. no API key)."""


# ═════════════════════════════════════════════════════════════════════════════
#  TUNABLES — edit these
# ═════════════════════════════════════════════════════════════════════════════
# How many reviews to collect PER LOCATION per run. Raise these when you get more
# SerpAPI credits (e.g. all to 10000 on a paid plan). SEARCH_BUDGET_PER_LOCATION
# below still caps the actual SerpAPI spend, so a big target here is safe — a run
# stops at whichever limit it hits first (target reached, budget spent, or the
# location simply has no more reviews — Somalia in particular is data-limited).
#
# A job's config can still override these per run:
#   {"target_per_bucket": N}         → same target for every bucket
#   {"search_budget_per_bucket": N}  → same budget for every bucket
TARGET_REVIEWS_PER_LOCATION: dict[str, int] = {
    "minnesota": 10000,
    "somalia":   10000,
    "general":   10000,
}

# Hard SerpAPI search budget PER LOCATION per run — the real cost control.
# Free plan = 250 searches/month total, so keep this small for scheduled daily
# runs (e.g. 20 → ~one bucket ≈ 20 searches/day ≈ 600/mo, so pair with the
# one-bucket-per-day rotation, or raise it for a paid plan / manual full sweep).
SEARCH_BUDGET_PER_LOCATION: int = 20
# ═════════════════════════════════════════════════════════════════════════════


# ── Buckets (equal weight) — each bucket runs the SAME budget/target ─────────
# Multiple queries per bucket widen place discovery (a single "hammam spa X"
# query finds only a handful of places; spas/day-spas/baths across cities find
# far more). Override per-bucket via config["queries"].
BUCKET_QUERIES: dict[str, list[str]] = {
    "minnesota": [
        "hammam spa Minnesota", "moroccan bath Minneapolis", "day spa Minneapolis MN",
        "spa Saint Paul MN", "massage spa Bloomington MN", "spa Rochester MN",
        "spa Saint Cloud MN", "turkish bath Minnesota",
    ],
    # "Somalia" is defined broadly as Somali / East African — Google has little
    # in-country spa review data, so this includes the Somali diaspora (which is
    # Nour Hammam's actual customer base) plus Somalia cities.
    "somalia": [
        "spa Mogadishu", "spa Hargeisa", "beauty spa Somalia", "spa Bosaso",
        "Somali spa Minneapolis", "Somali owned spa Minnesota", "hammam Minneapolis Somali",
        "spa Eastleigh Nairobi", "Somali spa Nairobi", "East African spa Minneapolis",
        "hammam Dubai Deira", "Somali beauty salon spa",
    ],
    "general": [
        "hammam spa", "moroccan hammam", "turkish bath spa", "hammam",
        "traditional hammam", "day spa", "moroccan bath house",
    ],
}
BUCKET_ORDER = ["minnesota", "somalia", "general"]

# Star rating → tier. We paginate ALL reviews and label each, so a single pass
# captures both the good feedback ("top") and the complaints ("worst").
def _tier_for(rating: Any) -> str:
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return "neutral"
    if r >= 4:
        return "top"
    if r <= 2:
        return "worst"
    return "neutral"


# CSV schema (also the append header). Flat & stable so a day's file can be
# appended to across runs and opened directly in Sheets/Excel.
CSV_COLUMNS = [
    "scraped_date", "bucket", "tier", "place_name", "place_id",
    "place_rating", "review_rating", "author", "review_date", "text",
    "source", "query",
]

_TIMEOUT = (5, 30)   # (connect, read)
_RETRIES = 1
_TEXT_MAX = 2000     # trim a runaway review body

# Operational knobs (target + budget live in the TUNABLES block above).
_DEFAULTS = {
    "places_per_query": 20,     # places taken from each discovery query
    "max_place_pages": 1,       # pages of place-discovery results per query
    "max_pages_per_place": 3,   # review pages per (place, sort)
    "sorts": ["ratingHigh", "ratingLow"],  # capture both extremes
}
_DEFAULT_TARGET = 300           # fallback if a bucket isn't in TARGET_REVIEWS_PER_LOCATION


def target_for(bucket: str, config: dict) -> int:
    """Reviews to collect for a bucket: config override wins, else the per-location
    target from the TUNABLES block, else the fallback."""
    if config.get("target_per_bucket") is not None:
        try:
            return max(1, int(config["target_per_bucket"]))
        except (TypeError, ValueError):
            pass
    return int(TARGET_REVIEWS_PER_LOCATION.get(bucket, _DEFAULT_TARGET))


def budget_for(config: dict) -> int:
    """SerpAPI search budget per bucket: config override (search_budget_per_bucket
    or legacy max_searches) wins, else the SEARCH_BUDGET_PER_LOCATION constant."""
    for key in ("search_budget_per_bucket", "max_searches"):
        if config.get(key) is not None:
            try:
                return max(1, int(config[key]))
            except (TypeError, ValueError):
                pass
    return SEARCH_BUDGET_PER_LOCATION


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or Config.SCHEDULER_DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Chicago")


def _cancelled(cancel_check: Callable[[], bool] | None) -> bool:
    try:
        return bool(cancel_check and cancel_check())
    except Exception:  # noqa: BLE001
        return False


# ── SerpAPI provider ─────────────────────────────────────────────────────────
def _serpapi_get(params: dict[str, Any]) -> dict[str, Any]:
    """One SerpAPI GET with a couple of retries on transient blips. Raises
    ReviewsError only when the key is missing/rejected (nothing can proceed)."""
    key = Config.SERPAPI_API_KEY
    if not key:
        raise ReviewsError(
            "SERPAPI_API_KEY is not set — the reviews scraper needs a SerpAPI key "
            "(serpapi.com) to fetch Google reviews."
        )
    query = {**params, "api_key": key, "hl": "en"}
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.get(Config.SERPAPI_BASE_URL, params=query, timeout=_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403):
                raise ReviewsError(f"SerpAPI rejected the key (HTTP {resp.status_code}).")
            if resp.status_code == 429:
                raise ReviewsError("SerpAPI rate/quota limit hit (HTTP 429) — plan searches exhausted.")
            log.warning("SerpAPI HTTP %s for %s", resp.status_code, params.get("engine"))
        except ReviewsError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("SerpAPI request error (%s): %s", params.get("engine"), exc)
        if attempt < _RETRIES:
            time.sleep(0.4 * (attempt + 1))
    if last_exc:
        log.warning("SerpAPI gave up after retries: %s", last_exc)
    return {}


def _next_token(data: dict[str, Any]) -> str | None:
    return (data.get("serpapi_pagination") or {}).get("next_page_token")


def find_places(query: str, limit: int, max_pages: int, spend: Callable[[], int]) -> tuple[list[dict[str, Any]], int]:
    """Discover up to `limit` places for a query, paging up to `max_pages` result
    pages. `spend()` returns the remaining search budget; we stop when it's 0.
    Returns (places, searches_used). Each place carries the data_id reviews need."""
    out: list[dict[str, Any]] = []
    used = 0
    token: str | None = None
    for _ in range(max(1, max_pages)):
        if spend() <= 0 or len(out) >= limit:
            break
        params = {"engine": "google_maps", "type": "search", "q": query}
        if token:
            params["next_page_token"] = token
        data = _serpapi_get(params)
        used += 1
        raw = data.get("local_results") or []
        if not raw and data.get("place_results"):
            raw = [data["place_results"]]
        for p in raw:
            data_id = p.get("data_id")
            if not data_id:
                continue
            out.append({
                "data_id": data_id,
                "place_id": p.get("place_id") or "",
                "name": p.get("title") or "",
                "rating": p.get("rating") or "",
            })
            if len(out) >= limit:
                break
        token = _next_token(data)
        if not token:
            break
    return out, used


def fetch_reviews(data_id: str, sort_by: str, max_pages: int, spend: Callable[[], int]) -> tuple[list[dict[str, Any]], int]:
    """Fetch a place's reviews sorted by `sort_by`, paging up to `max_pages`
    (bounded by the remaining budget via `spend()`). Returns (reviews, used)."""
    out: list[dict[str, Any]] = []
    used = 0
    token: str | None = None
    for _ in range(max(1, max_pages)):
        if spend() <= 0:
            break
        params = {"engine": "google_maps_reviews", "data_id": data_id, "sort_by": sort_by}
        if token:
            params["next_page_token"] = token
        data = _serpapi_get(params)
        used += 1
        reviews = data.get("reviews") or []
        for rv in reviews:
            user = rv.get("user") or {}
            text = (rv.get("snippet") or rv.get("extracted_snippet") or "").strip()
            out.append({
                "review_rating": rv.get("rating") or "",
                "author": user.get("name") or "",
                "review_date": rv.get("iso_date") or rv.get("date") or "",
                "text": text[:_TEXT_MAX],
            })
        token = _next_token(data)
        if not token:
            break
    return out, used


# ── CSV → S3 (append via read-modify-write; S3 has no native append) ─────────
def _rows_to_csv(rows: list[dict[str, Any]], include_header: bool) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if include_header:
        writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([r.get(c, "") for c in CSV_COLUMNS])
    return buf.getvalue()


def write_csv_append(rows: list[dict[str, Any]], date_str: str) -> str | None:
    """Append `rows` to s3://<bucket>/<REVIEWS_S3_PREFIX>/<date>.csv (or create it
    with a header). Returns the s3:// URI, or None if S3 is disabled."""
    if not rows:
        return None
    if not storage.enabled():
        log.warning("S3 disabled (S3_BUCKET unset) — %d review rows not persisted.", len(rows))
        return None
    key = f"{Config.REVIEWS_S3_PREFIX}/{date_str}.csv"
    s3_uri = f"s3://{storage.bucket()}/{key}"
    existing = ""
    try:
        existing_bytes, _ = storage.get_bytes(s3_uri)
        existing = existing_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — first write of the day (NoSuchKey) or unreadable
        existing = ""
    body = _rows_to_csv(rows, include_header=not existing)
    combined = (existing + body) if existing else body
    storage.put(key, combined.encode("utf-8"), "text/csv")
    return s3_uri


# ── Bucket selection ─────────────────────────────────────────────────────────
def todays_bucket(tz_name: str | None = None) -> str:
    """Rotate one bucket per day so the free SerpAPI quota gives each bucket equal
    turns over time (day-of-year modulo the bucket list)."""
    doy = datetime.now(_tz(tz_name)).timetuple().tm_yday
    return BUCKET_ORDER[doy % len(BUCKET_ORDER)]


def _int(config: dict, key: str) -> int:
    try:
        return max(1, int(config.get(key, _DEFAULTS[key])))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


# ── Scrape one location (its own target + search budget) ─────────────────────
def _scrape_bucket(
    bucket: str, *, target: int, budget: int, queries: list[str],
    places_per_query: int, max_place_pages: int, max_pages_per_place: int,
    sorts: list[str], today: str, cancel_check: Callable[[], bool] | None,
) -> tuple[list[dict[str, Any]], int, dict[str, int], bool]:
    """Collect up to `target` unique reviews for one bucket, spending at most
    `budget` SerpAPI searches. Returns (rows, searches_used, counts, cancelled)."""
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen: set[tuple] = set()
    used = 0
    collected = 0
    cancelled = False

    def left() -> int:
        return budget - used

    for query in queries:
        if cancelled or left() <= 0 or collected >= target:
            break
        if _cancelled(cancel_check):
            cancelled = True
            break
        try:
            places, u = find_places(query, places_per_query, max_place_pages, left)
            used += u
        except ReviewsError:
            raise  # missing/invalid key or quota exhausted — abort the run
        except Exception as exc:  # noqa: BLE001 — one query failing shouldn't kill the bucket
            log.warning("find_places(%s / %r) failed: %s", bucket, query, exc)
            continue
        for place in places:
            if cancelled or left() <= 0 or collected >= target:
                break
            if _cancelled(cancel_check):
                cancelled = True
                break
            for sort_by in sorts:
                if cancelled or left() <= 0 or collected >= target:
                    break
                if _cancelled(cancel_check):
                    cancelled = True
                    break
                pages = min(max_pages_per_place, left())
                try:
                    reviews, u = fetch_reviews(place["data_id"], sort_by, pages, left)
                    used += u
                except ReviewsError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("fetch_reviews(%s) failed: %s", bucket, exc)
                    continue
                for rv in reviews:
                    key = (place.get("place_id") or place.get("name"),
                           rv.get("author"), (rv.get("text") or "")[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    tier = _tier_for(rv.get("review_rating"))
                    rows.append({
                        "scraped_date": today,
                        "bucket": bucket,
                        "tier": tier,
                        "place_name": place.get("name", ""),
                        "place_id": place.get("place_id", ""),
                        "place_rating": place.get("rating", ""),
                        "review_rating": rv.get("review_rating", ""),
                        "author": rv.get("author", ""),
                        "review_date": rv.get("review_date", ""),
                        "text": rv.get("text", ""),
                        "source": "google/serpapi",
                        "query": query,
                    })
                    counts[f"{bucket}:{tier}"] = counts.get(f"{bucket}:{tier}", 0) + 1
                    collected += 1
                    if collected >= target:
                        break
    counts[f"{bucket}:_total"] = collected
    return rows, used, counts, cancelled


# ── The job entry point ──────────────────────────────────────────────────────
def run(config: dict[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Scrape reviews and append them to today's S3 CSV. Returns a summary dict.

    Each location gets its OWN target + search budget (so full_sweep gives every
    bucket a fair share, not first-come-first-served). Targets come from
    TARGET_REVIEWS_PER_LOCATION and the budget from SEARCH_BUDGET_PER_LOCATION
    (both at the top of this file), each overridable per run via config.

    config keys (all optional):
      full_sweep              — scrape all 3 buckets this run (else today's one)
      buckets                 — explicit list, e.g. ["minnesota"]; overrides rotation
      queries                 — {bucket: [query, ...]} to override discovery queries
      target_per_bucket       — override the per-location target for every bucket
      search_budget_per_bucket— override the per-location search budget
      places_per_query        — places taken per discovery query (default 20)
      max_place_pages         — discovery result pages per query (default 1)
      max_pages_per_place     — review pages per (place, sort) (default 3)
      sorts                   — review sorts (default ["ratingHigh","ratingLow"])
      timezone                — IANA tz for the date/rotation (default owner tz)

    `cancel_check()` is polled between queries/places; on cancel we still persist
    what was gathered and return {"cancelled": True, ...}.
    """
    config = config or {}
    full_sweep = bool(config.get("full_sweep"))
    places_per_query = _int(config, "places_per_query")
    max_place_pages = _int(config, "max_place_pages")
    max_pages_per_place = _int(config, "max_pages_per_place")
    sorts = config.get("sorts") or _DEFAULTS["sorts"]
    queries_map = config.get("queries") or BUCKET_QUERIES
    tz_name = config.get("timezone") or Config.SCHEDULER_DEFAULT_TZ
    budget = budget_for(config)

    buckets = config.get("buckets") or (BUCKET_ORDER if full_sweep else [todays_bucket(tz_name)])
    today = datetime.now(_tz(tz_name)).date().isoformat()

    all_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    targets: dict[str, int] = {}
    searches = 0
    cancelled = False

    for bucket in buckets:
        if cancelled:
            break
        target = target_for(bucket, config)
        targets[bucket] = target
        queries = queries_map.get(bucket) or [f"hammam spa {bucket}"]
        rows, used, bcounts, bcancel = _scrape_bucket(
            bucket, target=target, budget=budget, queries=queries,
            places_per_query=places_per_query, max_place_pages=max_place_pages,
            max_pages_per_place=max_pages_per_place, sorts=sorts, today=today,
            cancel_check=cancel_check,
        )
        all_rows.extend(rows)
        searches += used
        counts.update(bcounts)
        if bcancel:
            cancelled = True

    s3_uri = write_csv_append(all_rows, today)

    # Build the day's HTML intelligence report (reads the cumulative CSV back),
    # so every run leaves a clickable "open report" link. Best-effort — a report
    # failure never fails the scrape.
    report = None
    try:
        from . import reviews_report
        report = reviews_report.generate_for_date(today)
    except Exception as exc:  # noqa: BLE001
        log.warning("report generation failed: %s", exc)

    result = {
        "date": today,
        "buckets": list(buckets),
        "rows_written": len(all_rows),
        "counts": counts,
        "searches_used": searches,
        "search_budget_per_bucket": budget,
        "targets": targets,
        "s3_uri": s3_uri,
        "s3_enabled": storage.enabled(),
        "cancelled": cancelled,
    }
    if report:
        result["report_url"] = report.get("report_url")
        result["report_key"] = report.get("report_key")
    return result
