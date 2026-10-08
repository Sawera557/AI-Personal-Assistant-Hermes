"""Agent-only tools — offered ONLY to a Multi-agents turn whose agent lists them.

Kept out of hermes.tools.build_tools on purpose: an ordinary chat turn's tool
schemas (and the golden payload snapshots that pin them) stay byte-identical.
hermes.agent appends these schemas after build_tools when the active agent asks
for them, and hermes.tools.dispatch routes their calls here.

  reviews_data      the scraped Google-reviews corpus (S3 google-reviews/*.csv,
                    written by hermes.reviews_scraper) as aggregates — Nour
                    Hammam only. read_document can't reach that prefix.
  weather_forecast  Open-Meteo daily forecast (free, no key). Anthropic's
                    server-side web_fetch can't construct an API URL itself.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger("hermes.agent_tools")

# Loitokitok, Kajiado County, Kenya — the AfroFarms farm (context pack: afk).
FARM_LAT, FARM_LON = -2.9167, 37.5167

_MAX_ROWS_PER_FILE = 20000
_MAX_QUOTES = 12
_REVIEWS_BUSINESSES = {"nh", "all"}

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "reviews_data",
        "description": (
            "Nour Hammam's scraped Google-reviews corpus of hammams & spas (daily SerpAPI pulls "
            "stored as CSV). action='dates' lists the available pull dates. action='analyze' "
            "loads a date range (default: every date), de-duplicates reviews repeated across "
            "pulls, optionally filters by market and/or venue, and returns aggregates: per-market "
            "counts and average stars, star histogram, a per-venue table (Google rating, reviews "
            "captured, 1–2★ share), complaint themes from 1–2★ reviews, most-mentioned services, "
            "and a capped sample of verbatim quotes. Markets: 'minnesota', 'somalia' (Somali / "
            "East-African, incl. some Nairobi and Dubai venues), 'general'. The scraper samples "
            "the highest AND lowest rated reviews on purpose, so averages are not overall "
            "satisfaction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["dates", "analyze"]},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive. Omit for the earliest."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive. Omit for the latest."},
                "market": {"type": "string", "enum": ["minnesota", "somalia", "general"],
                           "description": "Omit for all markets."},
                "venue": {"type": "string", "description": "Case-insensitive substring of a venue name."},
                "quotes": {"type": "integer", "description": f"Quotes per tier (top/worst), 0–{_MAX_QUOTES}. Default 4."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "weather_forecast",
        "description": (
            "Daily weather forecast from Open-Meteo for up to 16 days: rain (mm), rain "
            "probability, min/max temperature (°C), max wind (km/h) and reference "
            "evapotranspiration (mm). Defaults to the AfroFarms farm at Loitokitok, Kenya; "
            "pass latitude/longitude for anywhere else. Returns the exact source URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "1–16. Default 14."},
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
            },
        },
    },
]

NAMES = frozenset(s["name"] for s in SCHEMAS)


def schemas_for(names: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    return [s for s in SCHEMAS if s["name"] in names]


def dispatch(name: str, tool_input: dict[str, Any], active_business: str | None) -> tuple[str, bool]:
    """(content_json, is_error) — same contract as hermes.tools.dispatch's first two."""
    try:
        if name == "reviews_data":
            out = _reviews_data(tool_input or {}, active_business)
        elif name == "weather_forecast":
            out = _weather(tool_input or {})
        else:
            return json.dumps({"error": f"Unknown agent tool {name}."}), True
    except Exception as exc:  # noqa: BLE001 — surface a readable error to the model
        log.warning("agent tool %s failed: %s", name, exc)
        return json.dumps({"error": f"{name} failed: {exc}"}), True
    is_error = bool(out.get("error"))
    return json.dumps(out, default=str, ensure_ascii=False), is_error


# ---- reviews_data -----------------------------------------------------------
_DATE_KEY = re.compile(r"/(\d{4}-\d{2}-\d{2})\.csv$")


def _available() -> dict[str, str]:
    """{date: version} for every daily CSV. The version (last_modified + size)
    keys the per-process cache, so a re-scraped day is re-read."""
    from . import storage
    from .config import Config

    out: dict[str, str] = {}
    for o in storage.list_objects(Config.REVIEWS_S3_PREFIX + "/"):
        m = _DATE_KEY.search(str(o.get("key") or ""))
        if m:
            out[m.group(1)] = f"{o.get('last_modified')}|{o.get('size')}"
    return out


def _available_dates() -> list[str]:
    return sorted(_available())


# (date, version) -> rows. Daily files are small (~10–650 KB); a warm process
# answers follow-up questions without touching S3 again.
_ROWS_CACHE: dict[tuple[str, str], list[dict[str, str]]] = {}


def _load_rows(date_str: str, version: str = "") -> list[dict[str, str]]:
    from . import storage
    from .config import Config

    hit = _ROWS_CACHE.get((date_str, version))
    if hit is not None:
        return hit
    uri = f"s3://{storage.bucket()}/{Config.REVIEWS_S3_PREFIX}/{date_str}.csv"
    raw, _ = storage.get_bytes(uri)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))
    rows = []
    for i, r in enumerate(reader):
        if i >= _MAX_ROWS_PER_FILE:
            break
        rows.append(r)
    if version:
        _ROWS_CACHE[(date_str, version)] = rows
    return rows


def _load_many(picked: list[str], versions: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = pool.map(lambda d: (d, _load_rows(d, versions.get(d, ""))), picked)
        return dict(loaded)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reviews_data(ti: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    if active_business not in _REVIEWS_BUSINESSES:
        return {"error": (
            "reviews_data is Nour Hammam's market data; this conversation is scoped to "
            f"'{active_business}'. Switch to Nour Hammam to use it."
        )}
    from . import storage
    if not storage.enabled():
        return {"error": "File storage (S3) isn't configured, so the reviews corpus can't be read."}

    versions = _available()
    dates = sorted(versions)
    if not dates:
        return {"error": "No scraped review files were found in storage."}
    if ti.get("action") == "dates":
        return {"dates": dates, "count": len(dates), "first": dates[0], "last": dates[-1]}

    start = str(ti.get("start_date") or dates[0])
    end = str(ti.get("end_date") or dates[-1])
    picked = [d for d in dates if start <= d <= end]
    if not picked:
        return {"error": f"No pulls between {start} and {end}. Available: {dates[0]} … {dates[-1]}."}

    market = (ti.get("market") or "").strip().lower() or None
    venue_q = (ti.get("venue") or "").strip().lower() or None
    n_quotes = max(0, min(_MAX_QUOTES, int(ti.get("quotes") if ti.get("quotes") is not None else 4)))

    raw_rows = 0
    seen: set[tuple] = set()
    rows: list[dict[str, str]] = []
    loaded = _load_many(picked, versions)
    for d in picked:
        for r in loaded[d]:
            raw_rows += 1
            if market and (r.get("bucket") or "").lower() != market:
                continue
            if venue_q and venue_q not in (r.get("place_name") or "").lower():
                continue
            key = (r.get("place_id") or r.get("place_name"), r.get("author"),
                   (r.get("text") or "")[:200], r.get("review_rating"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)

    scope = {
        "dates_used": [picked[0], picked[-1]], "pulls": len(picked),
        "market": market or "all", "venue_filter": venue_q,
        "rows_read": raw_rows, "unique_reviews": len(rows),
        "note": ("Reviews repeated across daily pulls are counted once. The scraper samples "
                 "top- and worst-rated reviews on purpose — star averages are not overall "
                 "customer satisfaction."),
    }
    if not rows:
        return {"scope": scope, "error": "No reviews match those filters."}

    from .reviews_report import analyze

    data = analyze(rows, f"{picked[0]}..{picked[-1]}")
    venues = _venue_table(rows)
    quotes = data.get("quotes") or {}
    return {
        "scope": scope,
        "markets": data.get("markets"),
        "rating_histogram_by_market": data.get("ratingByMarket"),
        "venues": venues,
        "complaint_themes_1_2_star": data.get("painThemes"),
        "services_mentioned": data.get("services"),
        "quotes": {
            "praise": (quotes.get("top") or [])[:n_quotes],
            "complaints": (quotes.get("worst") or [])[:n_quotes],
        },
        "source": "S3 google-reviews/<date>.csv (hermes.reviews_scraper, Google reviews via SerpAPI)",
    }


def _venue_table(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = (r.get("place_name") or "").strip() or "(unnamed)"
        v = by.setdefault(name, {"venue": name, "market": r.get("bucket"),
                                 "google_rating": None, "reviews": 0, "low_1_2": 0, "_stars": []})
        pr = _num(r.get("place_rating"))
        if pr is not None:
            v["google_rating"] = pr
        v["reviews"] += 1
        s = _num(r.get("review_rating"))
        if s is not None:
            v["_stars"].append(s)
            if s <= 2:
                v["low_1_2"] += 1
    out = []
    for v in by.values():
        stars = v.pop("_stars")
        v["avg_review_stars"] = round(sum(stars) / len(stars), 2) if stars else None
        v["low_share_pct"] = round(100 * v["low_1_2"] / v["reviews"], 1) if v["reviews"] else None
        out.append(v)
    out.sort(key=lambda v: (-(v["google_rating"] or 0), -v["reviews"]))
    return out[:60]


# ---- weather_forecast -------------------------------------------------------
_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_DAILY = ("precipitation_sum,precipitation_probability_max,temperature_2m_max,"
          "temperature_2m_min,wind_speed_10m_max,et0_fao_evapotranspiration")


# Daily-total bands, so "is 3.9 mm heavy?" is answered by the tool, not guessed.
_RAIN_BANDS_NOTE = ("Daily total: dry <0.2 mm · light <5 mm · moderate 5–20 mm · "
                    "heavy 20–50 mm · very heavy ≥50 mm")


def _rain_band(mm: Any) -> str | None:
    v = _num(mm)
    if v is None:
        return None
    if v < 0.2:
        return "dry"
    if v < 5:
        return "light"
    if v < 20:
        return "moderate"
    return "heavy" if v < 50 else "very heavy"


def _weather(ti: dict[str, Any]) -> dict[str, Any]:
    days = max(1, min(16, int(ti.get("days") or 14)))
    lat = float(ti["latitude"]) if ti.get("latitude") is not None else FARM_LAT
    lon = float(ti["longitude"]) if ti.get("longitude") is not None else FARM_LON
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"error": "latitude/longitude out of range."}
    params = {"latitude": lat, "longitude": lon, "daily": _DAILY,
              "forecast_days": days, "timezone": "auto"}
    resp = requests.get(_OPEN_METEO, params=params, timeout=(5, 20))
    if resp.status_code != 200:
        return {"error": f"Open-Meteo returned HTTP {resp.status_code}."}
    daily = (resp.json() or {}).get("daily") or {}
    keys = ["precipitation_sum", "precipitation_probability_max", "temperature_2m_max",
            "temperature_2m_min", "wind_speed_10m_max", "et0_fao_evapotranspiration"]
    table = [
        {"date": d, **{k: (daily.get(k) or [None] * len(daily.get("time", [])))[i] for k in keys}}
        for i, d in enumerate(daily.get("time") or [])
    ]
    for row in table:
        row["rain_intensity"] = _rain_band(row.get("precipitation_sum"))
    return {
        "location": {"latitude": lat, "longitude": lon,
                     "label": "AfroFarms, Loitokitok, Kenya" if (lat, lon) == (FARM_LAT, FARM_LON) else None},
        "units": {"precipitation_sum": "mm", "precipitation_probability_max": "%",
                  "temperature_2m_max": "°C", "temperature_2m_min": "°C",
                  "wind_speed_10m_max": "km/h", "et0_fao_evapotranspiration": "mm"},
        "days": table,
        "rain_intensity_bands": _RAIN_BANDS_NOTE,
        "source": resp.url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
