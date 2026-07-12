"""World Bank Open Data — authoritative agriculture & livestock indicators.

Why this source: for data-driven farming decisions (crop strategy, livestock
benchmarking, export context) Hermes needs machine-readable, *reliable* numbers,
not scraped dashboards. The World Bank Indicators API returns clean JSON, needs
no API key, and carries decades-long series for Kenya (and every other country)
on both crops and livestock — so a recommendation can cite a real, defensible
figure. It's the working backbone; FAOSTAT/ILRI were the research front-runners
but their public APIs are currently down (521) / forbidden (403) for automated
fetching, so they can be layered in later behind the same tool.

Endpoint shape (v2):
    https://api.worldbank.org/v2/country/{iso3;iso3}/indicator/{code}
        ?format=json&date=YYYY:YYYY&per_page=N
Response: [ <page-meta>, [ {indicator, country, date, value, unit}, ... ] ].
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

log = logging.getLogger("hermes.worldbank")

_BASE = "https://api.worldbank.org/v2"
# (connect, read). Connect is near-instant (~0.03s). A COLD World Bank query
# computes in ~8-23s normally (and can spike to ~35s+ under load) before the
# server caches it (a warm repeat is ~0.1s). The read timeout must exceed the
# server's compute time or the server aborts on client disconnect, never caches,
# and every attempt then fails — so 40s is chosen to clear a realistic cold
# query rather than clip a valid slow one. This rarely bites in practice: the
# canonical-window design (see fetch()) + the 12h cache + the startup prewarm()
# mean live turns almost always hit the warm cache, so the generous timeout is a
# safety net for the occasional first-ever/uncached fetch, not the common path.
_TIMEOUT = (5, 40)
_RETRIES = 1  # rides out an occasional transient 400 / warms-then-hits on a timeout
# The background prewarm() can afford to wait longer than a live turn, since no
# user is blocked on it — a patient timeout ensures the cache actually populates
# even when the API is slow, which is the whole point of pre-warming.
_PREWARM_TIMEOUT = (5, 60)

# In-process cache. World Bank data is annual, so a fetched series is good to
# reuse for hours — this makes the 15-25s cold cost a once-per-indicator event
# instead of paying it on every agent turn. Keyed by the exact request.
_CACHE_TTL = 12 * 3600
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()

# One canonical API window is fetched per indicator (see fetch()); every
# temporal question (latest / past-N / range) is then sliced from it client-
# side, so they all share a single cache entry. 30 years covers any realistic
# ask ("since 2000", "past 20 years", trends) while staying one small page.
_CANON_YEARS = 30
# Default slice when the caller asks for neither a range nor most_recent.
_DEFAULT_TREND_YEARS = 13

# Sentinel: distinguishes "the fetch failed (timeout/error)" from "the fetch
# succeeded but the World Bank has no data for this series" — the two need
# different messages to the user (retry-later vs genuinely-unavailable).
FETCH_FAILED = object()

# A curated, agriculture/livestock-focused catalog so the agent can ask for
# "cereal_yield" instead of memorising "AG.YLD.CREL.KG". Any raw World Bank
# indicator code the agent already knows is still accepted verbatim.
# key -> (indicator_code, human label). All verified live for Kenya.
CATALOG: dict[str, tuple[str, str]] = {
    # ── Production & yield ──────────────────────────────────────────────
    "crop_production_index": ("AG.PRD.CROP.XD", "Crop production index (2014-16 = 100)"),
    "livestock_production_index": ("AG.PRD.LVSK.XD", "Livestock production index (2014-16 = 100)"),
    "food_production_index": ("AG.PRD.FOOD.XD", "Food production index (2014-16 = 100)"),
    "cereal_yield": ("AG.YLD.CREL.KG", "Cereal yield (kg per hectare)"),
    "cereal_production": ("AG.PRD.CREL.MT", "Cereal production (metric tons)"),
    # ── Land & inputs ───────────────────────────────────────────────────
    "agricultural_land_pct": ("AG.LND.AGRI.ZS", "Agricultural land (% of land area)"),
    "arable_land_pct": ("AG.LND.ARBL.ZS", "Arable land (% of land area)"),
    "irrigated_land_pct": ("AG.LND.IRIG.AG.ZS", "Agricultural irrigated land (% of total agri land)"),
    "fertilizer_use": ("AG.CON.FERT.ZS", "Fertilizer consumption (kg per hectare of arable land)"),
    # ── Economy & people ────────────────────────────────────────────────
    "agri_pct_gdp": ("NV.AGR.TOTL.ZS", "Agriculture, forestry & fishing, value added (% of GDP)"),
    "agri_value_added_usd": ("NV.AGR.TOTL.CD", "Agriculture, forestry & fishing, value added (current US$)"),
    "employment_in_agri_pct": ("SL.AGR.EMPL.ZS", "Employment in agriculture (% of total employment)"),
    "rural_population_pct": ("SP.RUR.TOTL.ZS", "Rural population (% of total)"),
    # ── Trade / export context ──────────────────────────────────────────
    "food_exports_pct": ("TX.VAL.FOOD.ZS.UN", "Food exports (% of merchandise exports)"),
    "agri_raw_exports_pct": ("TX.VAL.AGRI.ZS.UN", "Agricultural raw materials exports (% of merchandise exports)"),
}

# Friendly country names → World Bank codes. Focused on Kenya, its East-African
# neighbours/benchmarks, key export-market comparators, and the aggregates most
# useful for "how does Kenya compare" questions. Any 3-letter ISO3 (e.g. "KEN")
# or aggregate code (e.g. "WLD", "SSF") is also accepted as-is.
_COUNTRY_ALIASES: dict[str, str] = {
    "kenya": "KEN", "ethiopia": "ETH", "tanzania": "TZA", "uganda": "UGA",
    "rwanda": "RWA", "nigeria": "NGA", "south africa": "ZAF", "egypt": "EGY",
    "india": "IND", "netherlands": "NLD", "brazil": "BRA", "china": "CHN",
    "world": "WLD", "sub-saharan africa": "SSF", "africa": "SSF",
    "low income": "LIC", "lower middle income": "LMC",
}

_DEFAULT_COUNTRY = "KEN"  # Kenya — Hermes' home context


def resolve_country(name_or_code: str) -> str:
    """Normalise a country name or code to a World Bank country/aggregate code.

    Accepts a friendly name ("kenya"), an ISO3 code ("KEN"), or a World Bank
    aggregate ("WLD", "SSF"). Unknown 3-letter tokens are passed through
    uppercased (the API simply returns no rows for a bad one, surfaced as such).
    """
    token = (name_or_code or "").strip()
    if not token:
        return _DEFAULT_COUNTRY
    alias = _COUNTRY_ALIASES.get(token.lower())
    if alias:
        return alias
    return token.upper()


def resolve_indicator(key_or_code: str) -> tuple[str, str | None]:
    """A catalog key ("cereal_yield") or a raw code ("AG.YLD.CREL.KG") →
    (indicator_code, catalog_label_or_None)."""
    token = (key_or_code or "").strip()
    if token in CATALOG:
        return CATALOG[token]
    # Also tolerate someone passing the raw code that happens to be in CATALOG.
    for code, label in CATALOG.values():
        if token == code:
            return code, label
    return token, None


def _cache_get(key: str) -> list[dict[str, Any]] | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL:
            return hit[1]
        if hit:  # expired
            _cache.pop(key, None)
    return None


def _fetch_series(indicator_code: str, countries: str, *, date: str | None = None, timeout=None):
    """One API call: all requested countries for a single indicator.

    Returns the raw World Bank rows on success ([] if the API genuinely has no
    data for the series), or the FETCH_FAILED sentinel if every attempt errored
    or timed out — the caller turns that into a distinct "temporarily
    unavailable" note rather than misreporting it as "no data".

    Results are cached per exact request for `_CACHE_TTL`, so the one-time cold
    cost is not repaid on later turns. (Only a plain `date` range is used —
    the API's `mrnev`/`mrv` params return HTTP 400 here, so "most recent" is
    handled by fetching a recent window and trimming client-side in `fetch`.)
    """
    cache_key = f"{indicator_code}|{countries}|d={date or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {"format": "json", "per_page": 1000}
    if date:
        params["date"] = date
    url = f"{_BASE}/country/{countries}/indicator/{indicator_code}"

    for attempt in range(_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout or _TIMEOUT)
            if resp.status_code == 200:
                payload = resp.json()
                rows = payload[1] if isinstance(payload, list) and len(payload) >= 2 and payload[1] else []
                with _cache_lock:
                    _cache[cache_key] = (time.time(), rows)
                return rows
            # A genuinely bad indicator also returns 4xx — but so do transient
            # blips, so retry a bounded number of times before giving up.
            log.warning(
                "World Bank %s -> HTTP %s (attempt %d/%d)",
                indicator_code, resp.status_code, attempt + 1, _RETRIES + 1,
            )
        except requests.RequestException as exc:
            log.warning("World Bank %s request error (attempt %d): %s", indicator_code, attempt + 1, exc)
        if attempt < _RETRIES:
            time.sleep(0.3 * (attempt + 1))
    return FETCH_FAILED


def fetch(
    indicators: list[str],
    countries: list[str] | None = None,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    most_recent: int | None = None,
    _timeout=None,
) -> dict[str, Any]:
    """Fetch World Bank indicator series and return a compact, model-friendly shape.

    Args:
        indicators: catalog keys (see CATALOG) and/or raw World Bank codes.
        countries: names/ISO3 codes; defaults to Kenya. Pass several to compare.
        start_year / end_year: inclusive year range. Defaults to the last ~12
            years when neither `most_recent` nor a range is given.
        most_recent: if set, keep only the N most recent non-null points per
            series (e.g. 1 for "the latest figure").

    Returns:
        {
          "source": "World Bank Open Data",
          "countries": ["KEN", ...],
          "series": [
             {"indicator": "AG.YLD.CREL.KG", "label": "...", "country": "KEN",
              "country_name": "Kenya", "unit": "", "observations": [
                 {"year": "2023", "value": 1757.5}, ...]},
             ...
          ],
          "notes": [...],   # any indicator/country that returned nothing
        }
    """
    country_codes = [resolve_country(c) for c in (countries or [_DEFAULT_COUNTRY])]
    countries_param = ";".join(country_codes)

    # ONE canonical API window per (indicator, countries), regardless of whether
    # the caller wants the latest point, the past 5 years, or a custom range —
    # the specific slice is applied client-side below. This is deliberate: the
    # cache (and the startup prewarm) is keyed by the exact request, so if every
    # question shape built its own window ("2016:2026" for latest, "2015:2026"
    # for past-5, ...) they'd each miss the cache and pay the slow ~10-30s cold
    # API cost separately. Sharing one window means the cold cost is paid at most
    # once per indicator and every temporal question then hits the warm cache.
    this_year = datetime.date.today().year
    canon_start = this_year - _CANON_YEARS
    # Only widen the API window if the caller explicitly asks for older data.
    api_start = min(canon_start, start_year) if start_year else canon_start
    date = f"{api_start}:{this_year}"

    resolved = [resolve_indicator(i) for i in indicators]

    def _one(pair: tuple[str, str | None]):
        code, _label = pair
        try:
            return pair, _fetch_series(code, countries_param, date=date, timeout=_timeout)
        except Exception as exc:  # noqa: BLE001 — degrade per-series, never fail the whole call
            log.warning("World Bank fetch failed for %s: %s", code, exc)
            return pair, FETCH_FAILED

    raw: list = []
    if resolved:
        with ThreadPoolExecutor(max_workers=min(6, len(resolved))) as pool:
            raw = list(pool.map(_one, resolved))

    series: list[dict[str, Any]] = []
    notes: list[str] = []
    for (code, label), rows in raw:
        if rows is FETCH_FAILED:
            notes.append(
                f"'{label or code}' could not be fetched right now (the World Bank API "
                "timed out). It's a temporary issue — try again shortly."
            )
            continue
        # Group this indicator's rows by country.
        by_country: dict[str, list[dict]] = {}
        indicator_name = label
        unit = ""
        for r in rows:
            if r.get("value") is None:
                continue
            ciso = r.get("countryiso3code") or (r.get("country") or {}).get("id") or "?"
            by_country.setdefault(ciso, []).append(r)
            indicator_name = indicator_name or (r.get("indicator") or {}).get("value")
            unit = unit or (r.get("unit") or "")

        if not by_country:
            notes.append(f"No data for '{code}' ({label or 'unknown indicator'}) in {country_codes}.")
            continue

        for ciso in country_codes:
            crows = by_country.get(ciso)
            if not crows:
                continue
            crows.sort(key=lambda x: x["date"], reverse=True)  # newest first
            # Apply the caller's requested slice CLIENT-SIDE (the API call always
            # used the shared canonical window). Explicit range wins; else the
            # most-recent-N; else a sensible recent-history default.
            if start_year or end_year:
                lo, hi = start_year or 0, end_year or 9999
                crows = [r for r in crows if lo <= int(r["date"]) <= hi]
            elif most_recent:
                crows = crows[:most_recent]
            else:
                crows = [r for r in crows if int(r["date"]) >= this_year - _DEFAULT_TREND_YEARS]
            if not crows:
                continue
            country_name = (crows[0].get("country") or {}).get("value") or ciso
            series.append(
                {
                    "indicator": code,
                    "label": indicator_name or code,
                    "country": ciso,
                    "country_name": country_name,
                    "unit": unit,
                    "observations": [
                        {"year": r["date"], "value": r["value"]} for r in crows
                    ],
                }
            )

    return {
        "source": "World Bank Open Data (api.worldbank.org)",
        "countries": country_codes,
        "series": series,
        "notes": notes,
    }


# The indicators most likely to be asked about — warmed at startup so a live
# chat turn hits the in-process (and World Bank server-side) cache instead of
# paying the one-time 8-25s cold cost mid-conversation.
_PREWARM_KEYS = [
    "cereal_yield", "livestock_production_index", "crop_production_index",
    "food_production_index", "agri_pct_gdp", "agricultural_land_pct",
    "fertilizer_use", "employment_in_agri_pct",
]


def prewarm() -> int:
    """Best-effort: pre-fetch Kenya's core indicators into the cache at boot, so
    a live chat turn hits the warm cache instead of the slow cold API. One pass
    is enough now that every temporal question (latest / past-N / range) shares
    the single canonical API window per indicator. Returns the number of series
    warmed. Safe in a background thread; never raises.

    Uses a more patient timeout than live turns (see `_PREWARM_TIMEOUT`): nobody
    is blocked on it, and warming reliably even against a slow API is the point.
    """
    try:
        out = fetch(_PREWARM_KEYS, [_DEFAULT_COUNTRY], _timeout=_PREWARM_TIMEOUT)
        return len(out.get("series") or [])
    except Exception as exc:  # noqa: BLE001 — warming is optional
        log.warning("World Bank prewarm failed: %s", exc)
        return 0
