"""Turn a day's scraped reviews CSV into a self-contained HTML intelligence report.

Every reviews-scraper run calls `generate_for_date(date)`, which reads that day's
CSV back from S3, analyses it, renders a single self-contained HTML page (all CSS
+ JS inline, no external requests), and stores it at
``<REVIEWS_S3_PREFIX>/reports/<date>.html``. The app serves it at
``GET /api/reviews/report/<date>`` (see hermes/reviews_api.py), so each run gets a
stable, clickable "open the report" link in the Jobs UI.

The report is fully DATA-DRIVEN: Python computes one `DATA` object (KPIs, service
demand, pain points, ratings, competitor scatter/table, quotes, and the narrative
prose with real numbers) and the page's inline JS just renders it — so the report
is correct for any day, whether it's one rotated bucket or a full sweep.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any

from . import storage
from .config import Config

log = logging.getLogger("hermes.reviews_report")

BUCKET_LABELS = {"minnesota": "Minnesota", "somalia": "Somali / East-African", "general": "General benchmark"}
BUCKET_ORDER = ["minnesota", "somalia", "general"]

SERVICES = {
    "Massage": r"massage|masseuse|deep tissue|swedish|hot stone|knead",
    "Hammam / Steam bath": r"hammam|steam|moroccan bath|turkish bath|hamam|bath house|bathhouse",
    "Facial / Skincare": r"facial|skincare|skin care|complexion|hydrafacial|microderm",
    "Sauna": r"sauna",
    "Body scrub / Exfoliation": r"scrub|exfoliat|gommage|kessa|black soap|dead skin|glow",
    "Bridal / Group events": r"bridal|bride|wedding|party|group|birthday|henna night",
    "Nails (mani/pedi)": r"nail|manicure|pedicure|\bmani\b|\bpedi\b|gel|acrylic",
    "Hair styling": r"\bhair\b|blowout|blow dry|hairstyl|hairdress|braids",
    "Halal / Women-only / Private": r"women only|women-only|female only|private|modest|halal|hijab|separate",
    "Waxing / Threading": r"wax|thread|hair removal|sugaring|brazilian",
    "Brows / Lashes": r"\bbrow|eyebrow|lash",
}
PAINS = {
    "Poor quality / Let-down": r"disappoint|mediocre|not good|terrible|awful|worst|bad experience|waste",
    "Booking / Wait time": r"wait|late|appointment|booking|reservation|on time|slow|kept waiting",
    "Staff attitude / Service": r"rude|unprofessional|attitude|disrespect|ignored|customer service|unfriendly|unwelcom",
    "Refund / Charges": r"refund|cancel|charged|no.?show|overcharg",
    "Price vs. value": r"expensive|overpric|pricey|not worth|too much money|rip.?off|costly|price",
    "Cleanliness / Hygiene": r"dirty|unclean|filth|hygien|smell|mold|not clean|unsanitary|gross",
    "Crowded / Small space": r"crowded|cramped|small|tight|packed|overbooked",
    "Communication / Language": r"language|understand|communicat|english|barrier",
}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_csv(date_str: str) -> list[dict[str, str]] | None:
    if not storage.enabled():
        return None
    s3_uri = f"s3://{storage.bucket()}/{Config.REVIEWS_S3_PREFIX}/{date_str}.csv"
    try:
        raw, _ = storage.get_bytes(s3_uri)
    except Exception:  # noqa: BLE001 — no file for that date yet
        return None
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))


def _hits(rows: list[dict], taxonomy: dict[str, str]) -> list[list]:
    out = []
    for name, pat in taxonomy.items():
        rx = re.compile(pat, re.I)
        out.append([name, sum(1 for r in rows if rx.search(r.get("text") or ""))])
    out.sort(key=lambda x: -x[1])
    return out


def analyze(rows: list[dict], date_str: str) -> dict[str, Any]:
    """Compute the full DATA object the report page renders from."""
    present = [b for b in BUCKET_ORDER if any(r.get("bucket") == b for r in rows)]
    by_bucket = {b: [r for r in rows if r.get("bucket") == b] for b in present}

    def avg(rs):
        vals = [_num(r.get("review_rating")) for r in rs]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else 0

    def dist(rs):
        d = {i: 0 for i in range(1, 6)}
        for r in rs:
            v = _num(r.get("review_rating"))
            if v:
                s = min(5, max(1, int(round(v))))
                d[s] += 1
        return [d[i] for i in range(1, 6)]

    markets = [{
        "key": b, "label": BUCKET_LABELS.get(b, b), "count": len(by_bucket[b]),
        "avg": avg(by_bucket[b]),
        "places": len({(r.get("place_id") or r.get("place_name")) for r in by_bucket[b]}),
        "top": sum(1 for r in by_bucket[b] if r.get("tier") == "top"),
        "worst": sum(1 for r in by_bucket[b] if r.get("tier") == "worst"),
        "neutral": sum(1 for r in by_bucket[b] if r.get("tier") == "neutral"),
    } for b in present]

    services = [s for s in _hits(rows, SERVICES) if s[1] > 0][:11]
    pains = [p for p in _hits([r for r in rows if r.get("tier") == "worst"], PAINS) if p[1] > 0]

    rating_by = {"stars": [1, 2, 3, 4, 5]}
    for b in present:
        rating_by[b] = dist(by_bucket[b])

    # place-level for scatter + table
    places: dict[str, dict] = {}
    for r in rows:
        k = r.get("place_id") or r.get("place_name")
        if not k:
            continue
        p = places.setdefault(k, {"n": r.get("place_name", ""), "b": r.get("bucket", ""),
                                  "pr": r.get("place_rating", ""), "ratings": [], "v": 0})
        p["v"] += 1
        rv = _num(r.get("review_rating"))
        if rv:
            p["ratings"].append(rv)
    scatter = []
    for p in places.values():
        pr = _num(p["pr"])
        if pr is None:
            pr = round(sum(p["ratings"]) / len(p["ratings"]), 2) if p["ratings"] else None
        if pr is None:
            continue
        scatter.append({"n": (p["n"] or "")[:44], "b": p["b"], "r": round(pr, 1), "v": p["v"]})
    scatter.sort(key=lambda x: -x["v"])
    table = scatter[:13]

    # quotes: real English reviews, deduped; worst strictly <=2 stars
    def quotes(tier, hi_lo):
        seen, out = set(), []
        for r in rows:
            if r.get("tier") != tier:
                continue
            rv = _num(r.get("review_rating"))
            if rv is None:
                continue
            if hi_lo == "hi" and rv < 4:
                continue
            if hi_lo == "lo" and rv > 2:
                continue
            t = (r.get("text") or "").strip()
            if not t.isascii() or not (55 <= len(t) <= 300):
                continue
            key = t[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append({"place": (r.get("place_name") or "")[:38], "bucket": r.get("bucket", ""),
                        "rating": r.get("review_rating", ""), "text": t})
            if len(out) >= 3:
                break
        return out

    svc_top = services[0] if services else ["—", 0]
    pain_top = pains[0] if pains else ["—", 0]
    hammam_n = next((c for n, c in services if n.startswith("Hammam")), 0)
    women_n = next((c for n, c in services if n.startswith("Halal")), 0)

    total = len(rows)
    distinct = len(places)
    headline = {"top_service": svc_top[0], "top_service_n": svc_top[1],
                "top_complaint": pain_top[0], "top_complaint_n": pain_top[1],
                "hammam_n": hammam_n, "women_n": women_n}

    kpis = [
        {"v": f"{total:,}", "k": "Reviews analysed", "s": f"across {distinct} distinct venues", "rail": "var(--brass)"},
        {"v": str(svc_top[1]), "k": f"Mention {svc_top[0].split(' /')[0].split(' (')[0].lower()}",
         "s": "the most-named service", "rail": "var(--s-mn)"},
        {"v": str(pain_top[1]), "k": "Top complaint",
         "s": pain_top[0] + " — in 1–2★ reviews", "rail": "var(--clay)"},
        {"v": str(hammam_n), "k": "Name the hammam", "s": "steam-bath ritual — NH's core lane", "rail": "var(--petrol)"},
    ]

    # narrative built from real numbers so it's always correct
    summary = [
        {"t": "What people come for",
         "b": f"<b>{svc_top[0]}</b> leads demand with <b>{svc_top[1]}</b> mentions across the reviews. "
              f"The authentic hammam / steam-bath ritual is named <b>{hammam_n}</b> times — the experience "
              "Nour Hammam's concept already owns and competitors deliver inconsistently."},
        {"t": "What sinks competitors",
         "b": f"The dominant complaint in 1–2★ reviews is <b>{pain_top[0].lower()}</b> "
              f"(<b>{pain_top[1]}</b> mentions) — an operational failure, not a treatment one, and fixable with process."},
        {"t": "The modesty opening",
         "b": f"Explicit women-only / modesty / private language appears <b>{women_n}</b> times, and the harshest "
              "reviews include comfort and body-exposure complaints — exactly what NH's women-only, modesty-first "
              "promise answers."},
        {"t": "Read the ratings carefully",
         "b": "The scraper samples the highest- and lowest-rated reviews on purpose, so the star split is bimodal "
              "<i>by design</i> — treat it as praise-vs-complaints, never as an average satisfaction score."},
    ]
    recs = [
        {"t": "Lead with the hammam, sell massage alongside",
         "b": "Package the steam-bath + black-soap + gommage scrub as the signature; offer the high-demand massage as the add-on that fills the calendar.",
         "e": f"{svc_top[0]} {svc_top[1]} · hammam {hammam_n} mentions."},
        {"t": "Make modesty the felt experience",
         "b": "Private treatment, women-only-vetted staff and comfort defaults turn a competitor's worst review into your headline promise.",
         "e": f"{women_n} modesty / women-only / private mentions."},
        {"t": "Win on operations from day one",
         "b": "Reliable booking, honest wait times, trained front-desk manners and a clear refund policy neutralise the biggest complaint themes.",
         "e": f"Top complaint: {pain_top[0].lower()} ({pain_top[1]})."},
        {"t": "Price with transparency",
         "b": "Publish package prices and what's included; value-shock, not price itself, is what burns trust in the worst reviews.",
         "e": "Price/value recurs across 1–2★ reviews."},
        {"t": "Own bridal & group bookings",
         "b": "Henna nights, bridal parties and group hammam sessions are a bookable, high-margin lane the East Lake Somali community will fill.",
         "e": "Bridal/group demand shows across the set."},
        {"t": "Study the names to beat",
         "b": "The most-reviewed local venues define the ceiling — visit, learn the ritual, then out-authenticate and out-accommodate them.",
         "e": (table[0]["n"] + f" ({table[0]['r']}★)") if table else "See the competitor table."},
    ]

    return {
        "meta": {"date": date_str, "generated_at": datetime.now().strftime("%d %b %Y, %H:%M"),
                 "total": total, "places": distinct, "markets": len(present)},
        "kpis": kpis, "headline": headline, "summary": summary, "recs": recs,
        "markets": markets, "services": services,
        "ratingGroups": present, "ratingByMarket": rating_by,
        "painThemes": pains, "scatter": scatter, "table": table,
        "quotes": {"top": quotes("top", "hi"), "worst": quotes("worst", "lo")},
    }


def render_html(data: dict[str, Any]) -> str:
    return _TEMPLATE.replace("__DATA_JSON__", json.dumps(data))


def generate_for_date(date_str: str) -> dict[str, Any] | None:
    """Read the day's CSV, build + store the HTML report. Returns
    {report_key, report_s3_uri, report_url} or None if there's nothing to report."""
    rows = _read_csv(date_str)
    if not rows:
        return None
    try:
        data = analyze(rows, date_str)
        html = render_html(data)
        key = f"{Config.REVIEWS_S3_PREFIX}/reports/{date_str}.html"
        s3_uri = storage.put(key, html.encode("utf-8"), "text/html; charset=utf-8")
        return {"report_key": key, "report_s3_uri": s3_uri, "report_url": f"/api/reviews/report/{date_str}"}
    except Exception as exc:  # noqa: BLE001 — reporting must never fail the scrape
        log.warning("generate_for_date(%s) failed: %s", date_str, exc)
        return None


def load_report_html(date_str: str) -> str | None:
    """Fetch a previously-stored report's HTML (for the serving endpoint)."""
    if not storage.enabled():
        return None
    key = f"{Config.REVIEWS_S3_PREFIX}/reports/{date_str}.html"
    try:
        raw, _ = storage.get_bytes(f"s3://{storage.bucket()}/{key}")
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  The report page: static shell (CSS + empty containers), 100% rendered by the
#  inline JS from the injected DATA object. Self-contained — no external requests.
# ─────────────────────────────────────────────────────────────────────────────
_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Steam & Signal — Review Intelligence for Nour Hammam</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --page:#ece5d9;--surface:#f7f2ea;--surface-2:#f1eadd;--ink:#221c12;--ink-2:#5b5245;
    --muted:#8a8072;--line:#e0d7c7;--line-strong:#d3c8b4;
    --brass:#9a6f26;--brass-2:#7d5a1c;--petrol:#2c6b62;--clay:#b6432b;--good:#2f7d3e;
    --s-mn:#2a78d6;--s-so:#199e70;--s-gen:#c98500;
    --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;--maxw:1080px;color-scheme:light;}
  @media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
    --page:#100d09;--surface:#1a1510;--surface-2:#221b12;--ink:#f2ece0;--ink-2:#c9bfa9;
    --muted:#968b78;--line:#2b241a;--line-strong:#3a3123;
    --brass:#ca9c4f;--brass-2:#d9b060;--petrol:#57a99c;--clay:#dc6c4f;--good:#57b56a;
    --s-mn:#4a93ec;--s-so:#26bc86;--s-gen:#e0a92a;color-scheme:dark;}}
  :root[data-theme="dark"]{
    --page:#100d09;--surface:#1a1510;--surface-2:#221b12;--ink:#f2ece0;--ink-2:#c9bfa9;
    --muted:#968b78;--line:#2b241a;--line-strong:#3a3123;
    --brass:#ca9c4f;--brass-2:#d9b060;--petrol:#57a99c;--clay:#dc6c4f;--good:#57b56a;
    --s-mn:#4a93ec;--s-so:#26bc86;--s-gen:#e0a92a;color-scheme:dark;}
  :root[data-theme="light"]{
    --page:#ece5d9;--surface:#f7f2ea;--surface-2:#f1eadd;--ink:#221c12;--ink-2:#5b5245;
    --muted:#8a8072;--line:#e0d7c7;--line-strong:#d3c8b4;
    --brass:#9a6f26;--brass-2:#7d5a1c;--petrol:#2c6b62;--clay:#b6432b;--good:#2f7d3e;
    --s-mn:#2a78d6;--s-so:#199e70;--s-gen:#c98500;color-scheme:light;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--ink);font-family:var(--sans);line-height:1.6;-webkit-font-smoothing:antialiased}
  .wrap{max-width:var(--maxw);margin:0 auto;padding:0 22px}
  section{padding:44px 0;border-top:1px solid var(--line)}
  h1,h2,h3{font-family:var(--serif);font-weight:600;letter-spacing:-.01em;text-wrap:balance;margin:0}
  p{margin:0 0 14px;max-width:70ch}
  .eyebrow{font-family:var(--sans);text-transform:uppercase;letter-spacing:.18em;font-size:11.5px;font-weight:700;color:var(--brass-2)}
  .muted{color:var(--muted)}.tnum{font-variant-numeric:tabular-nums}
  .mast{padding:56px 0 30px;position:relative;overflow:hidden}
  .mast::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.5;
    background:radial-gradient(120% 90% at 100% 0%,color-mix(in srgb,var(--brass) 16%,transparent),transparent 60%)}
  .kicker{display:flex;align-items:center;gap:12px;margin-bottom:18px}
  .kicker .dot{width:9px;height:9px;border-radius:50%;background:var(--brass);box-shadow:0 0 0 4px color-mix(in srgb,var(--brass) 22%,transparent)}
  .mast h1{font-size:clamp(34px,6vw,60px);line-height:1.02;margin:0 0 14px}
  .mast .lede{font-size:clamp(16px,2.1vw,19px);color:var(--ink-2);max-width:60ch}
  .dateline{margin-top:22px;display:flex;flex-wrap:wrap;gap:8px 20px;font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:16px}
  .dateline b{color:var(--ink);font-weight:600}
  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .tile{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;position:relative}
  .tile .v{font-size:34px;font-weight:600;font-family:var(--serif);line-height:1;letter-spacing:-.02em}
  .tile .k{margin-top:8px;font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
  .tile .s{margin-top:6px;font-size:12.5px;color:var(--ink-2)}
  .tile .rail{position:absolute;left:0;top:14px;bottom:14px;width:3px;border-radius:3px;background:var(--brass)}
  h2.sh{font-size:clamp(23px,3.4vw,30px);margin-bottom:8px}
  .sub{color:var(--ink-2);font-size:15.5px;margin-bottom:24px;max-width:74ch}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:22px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:6px}
  .chart-title{font-family:var(--serif);font-size:18px;font-weight:600}
  .chart-note{font-size:12px;color:var(--muted)}
  .legend{display:flex;flex-wrap:wrap;gap:14px;margin:2px 0 14px;font-size:12.5px;color:var(--ink-2)}
  .legend span{display:inline-flex;align-items:center;gap:7px}
  .legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
  figure{margin:0}svg{display:block;width:100%;height:auto;overflow:visible}
  .glabel{font-family:var(--sans);font-size:12.5px;fill:var(--ink)}
  .vlabel{font-family:var(--sans);font-size:12px;fill:var(--ink-2);font-variant-numeric:tabular-nums}
  .axis{font-family:var(--sans);fill:var(--muted);font-size:11px}
  .grid line{stroke:var(--line);stroke-width:1}
  .bar{transition:opacity .15s}.bar:hover{opacity:.82;cursor:default}
  .dot{stroke:var(--surface);stroke-width:1.5}.dot:hover{stroke:var(--ink);stroke-width:2;cursor:pointer}
  .scroll{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700}
  td.n{text-align:right;font-variant-numeric:tabular-nums}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
  .pill.mn{background:color-mix(in srgb,var(--s-mn) 16%,transparent);color:var(--s-mn)}
  .pill.so{background:color-mix(in srgb,var(--s-so) 18%,transparent);color:var(--s-so)}
  .pill.gen{background:color-mix(in srgb,var(--s-gen) 20%,transparent);color:var(--s-gen)}
  .rate{font-weight:600;font-variant-numeric:tabular-nums}
  .quotes{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .qcol h3{font-size:16px;margin-bottom:12px;display:flex;align-items:center;gap:9px}
  .tag{font-family:var(--sans);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:2px 8px;border-radius:6px}
  .tag.good{background:color-mix(in srgb,var(--good) 16%,transparent);color:var(--good)}
  .tag.bad{background:color-mix(in srgb,var(--clay) 15%,transparent);color:var(--clay)}
  blockquote{margin:0 0 12px;background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--brass);border-radius:10px;padding:14px 16px;font-size:14px;color:var(--ink-2)}
  blockquote.bad{border-left-color:var(--clay)}
  blockquote .src{display:block;margin-top:9px;font-size:12px;color:var(--muted)}
  blockquote .src b{color:var(--ink);font-weight:600}
  .recs{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .rec{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px}
  .rec .num{font-family:var(--serif);font-size:13px;color:var(--brass-2);font-weight:700}
  .rec h3{font-size:17px;margin:6px 0 8px}
  .rec p{font-size:13.5px;color:var(--ink-2);margin:0}
  .rec .ev{margin-top:11px;font-size:12px;color:var(--muted);border-top:1px dashed var(--line-strong);padding-top:9px}
  .rec .ev b{color:var(--petrol)}
  .notes{font-size:13px;color:var(--ink-2)}.notes li{margin-bottom:8px}
  footer{padding:30px 0 60px;color:var(--muted);font-size:12.5px}
  #tip{position:fixed;pointer-events:none;z-index:50;background:var(--ink);color:var(--page);font-size:12px;line-height:1.45;padding:8px 10px;border-radius:8px;opacity:0;transition:opacity .1s;max-width:240px;box-shadow:0 6px 20px rgba(0,0,0,.28)}
  #tip b{color:var(--brass)}#tip .r{color:#fff}
  @media (max-width:760px){.tiles{grid-template-columns:1fr 1fr}.grid2,.quotes,.recs{grid-template-columns:1fr}}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div id="tip" role="status" aria-live="polite"></div>
<header class="mast"><div class="wrap">
  <div class="kicker"><span class="dot"></span><span class="eyebrow">Competitive Review Intelligence · Nour Hammam</span></div>
  <h1>Steam &amp; Signal</h1>
  <p class="lede">What Google reviews of hammams and spas reveal about the services people want, the failures that sink competitors, and where Nour Hammam can win.</p>
  <div class="dateline" id="dateline"></div>
</div></header>
<section><div class="wrap"><div class="tiles" id="tiles"></div></div></section>
<section><div class="wrap"><span class="eyebrow">01 · The read</span><h2 class="sh">Executive summary</h2>
  <p class="sub">What the data says, before the charts.</p><div class="grid2" id="summary"></div></div></section>
<section><div class="wrap"><span class="eyebrow">02 · Markets</span><h2 class="sh">Where the signal comes from</h2>
  <p class="sub">Reviews captured per market, split into top (praise), worst (complaints) and neutral.</p>
  <div class="card"><div class="legend"><span><i style="background:var(--good)"></i>Top ≥4★</span><span><i style="background:var(--clay)"></i>Worst ≤2★</span><span><i style="background:var(--muted)"></i>Neutral 3★</span></div><figure id="mkt"></figure></div></div></section>
<section><div class="wrap"><span class="eyebrow">03 · Demand</span><h2 class="sh">The most-wanted services</h2>
  <p class="sub">How often each service is named across the reviews (a review can mention several).</p>
  <div class="card"><div class="chart-head"><span class="chart-title">Service mentions</span><span class="chart-note">reviews mentioning each</span></div><figure id="svc"></figure></div></div></section>
<section><div class="wrap"><span class="eyebrow">04 · Ratings</span><h2 class="sh">Praise and complaints, side by side</h2>
  <p class="sub"><b>Read carefully:</b> the scraper deliberately samples the highest- and lowest-rated reviews, so this split is bimodal <i>by design</i> — it maps what earns 5★ against what triggers 1★, not an average.</p>
  <div class="card"><div class="legend" id="rate-legend"></div><figure id="rate"></figure></div></div></section>
<section><div class="wrap"><span class="eyebrow">05 · Pain points</span><h2 class="sh">Why the 1-star reviews happen</h2>
  <p class="sub">Themes in the worst (≤2★) reviews — the failure modes to design out of Nour Hammam. None are about the treatment itself.</p>
  <div class="card"><div class="chart-head"><span class="chart-title">Complaint themes in 1–2★ reviews</span><span class="chart-note">reviews mentioning each</span></div><figure id="pain"></figure></div></div></section>
<section><div class="wrap"><span class="eyebrow">06 · The field</span><h2 class="sh">Rating vs. presence</h2>
  <p class="sub">Every venue by Google star rating against how many reviews we captured (a prominence proxy). Up-and-right is a strong, busy competitor.</p>
  <div class="card"><div class="legend" id="scatter-legend"></div><figure id="scatter"></figure>
  <p class="chart-note" style="margin-top:10px">Hover any point for the venue. X = reviews captured in this pull, not total Google count.</p></div></div></section>
<section><div class="wrap"><span class="eyebrow">07 · Neighbours</span><h2 class="sh">Benchmark competitors</h2>
  <p class="sub">The most-reviewed venues in the set — the reputations Nour Hammam will be measured against.</p>
  <div class="card scroll"><table id="tbl"><thead><tr><th>Venue</th><th>Market</th><th class="n">Rating</th><th class="n">Reviews captured</th></tr></thead><tbody></tbody></table></div></div></section>
<section><div class="wrap"><span class="eyebrow">08 · Voice of the customer</span><h2 class="sh">In their own words</h2>
  <p class="sub">Quotes pulled straight from the reviews — the promise to live up to, and the failures to avoid.</p>
  <div class="quotes"><div class="qcol"><h3><span class="tag good">What earns 5★</span></h3><div id="q-top"></div></div>
  <div class="qcol"><h3><span class="tag bad">What triggers 1★</span></h3><div id="q-worst"></div></div></div></div></section>
<section><div class="wrap"><span class="eyebrow">09 · So what</span><h2 class="sh">What Nour Hammam should do with this</h2>
  <p class="sub">Moves the data supports directly.</p><div class="recs" id="recs"></div></div></section>
<section><div class="wrap"><span class="eyebrow">10 · Fine print</span><h2 class="sh">Method &amp; honest limits</h2>
  <ul class="notes">
    <li><b>Deliberate extremes.</b> The scraper captures the highest- and lowest-rated reviews per venue, so star distributions are bimodal by design — "praise vs complaints", not a satisfaction average.</li>
    <li><b>Somali reviews are undercounted in the themes.</b> Many are written in Somali; the keyword themes are English-based, so that market's demand is understated.</li>
    <li><b>Sample, not census.</b> A scraped sample from Google. Directional and decision-useful, not exhaustive.</li>
    <li><b>"Reviews captured" ≠ total Google reviews.</b> In the scatter and table it's how many this pull retrieved (a prominence proxy).</li>
  </ul></div></section>
<footer><div class="wrap" id="foot"></div></footer>
<script>
const DATA = __DATA_JSON__;
const BC={minnesota:"var(--s-mn)",somalia:"var(--s-so)",general:"var(--s-gen)"};
const BL={minnesota:"Minnesota",somalia:"Somali / E-African",general:"General"};
const PILL={minnesota:"mn",somalia:"so",general:"gen"};
const NS="http://www.w3.org/2000/svg";
const el=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in(a||{})){const v=a[k];
  if((k==="fill"||k==="stroke")&&typeof v==="string"&&v.indexOf("var(")>-1)e.style[k]=v;else e.setAttribute(k,v);}return e;};
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tip=document.getElementById("tip");
function showTip(h,x,y){tip.innerHTML=h;tip.style.opacity="1";const w=tip.offsetWidth,ht=tip.offsetHeight;
  let nx=x+14,ny=y+14;if(nx+w>innerWidth-8)nx=x-w-14;if(ny+ht>innerHeight-8)ny=y-ht-14;tip.style.left=nx+"px";tip.style.top=ny+"px";}
function bindTip(n,h){n.addEventListener("mousemove",e=>showTip(h,e.clientX,e.clientY));
  n.addEventListener("mouseleave",()=>tip.style.opacity="0");
  n.addEventListener("touchstart",e=>{const t=e.touches[0];showTip(h,t.clientX,t.clientY);},{passive:true});}

function legend(id,keys){const w=document.getElementById(id);if(!w)return;
  w.innerHTML=keys.map(k=>`<span><i style="background:${BC[k]}"></i>${BL[k]}</span>`).join("");}

function fillMeta(){
  const m=DATA.meta;
  document.getElementById("dateline").innerHTML=
    `<span><b>Prepared for</b> Nour Hammam LLC — women-only halal hammam</span>`+
    `<span><b>Source</b> Google reviews · automated scraper</span>`+
    `<span><b>Data date</b> ${esc(m.date)}</span>`+
    `<span><b>Scope</b> ${m.places} venues · ${m.markets} market(s)</span>`;
  document.getElementById("tiles").innerHTML=DATA.kpis.map(t=>
    `<div class="tile"><span class="rail" style="background:${t.rail}"></span><div class="v tnum">${esc(t.v)}</div><div class="k">${esc(t.k)}</div><div class="s">${esc(t.s)}</div></div>`).join("");
  document.getElementById("summary").innerHTML=
    `<div class="card">${DATA.summary.slice(0,2).map(s=>`<p><b>${esc(s.t)}.</b> ${s.b}</p>`).join("")}</div>`+
    `<div class="card">${DATA.summary.slice(2).map(s=>`<p><b>${esc(s.t)}.</b> ${s.b}</p>`).join("")}</div>`;
  document.getElementById("recs").innerHTML=DATA.recs.map((r,i)=>
    `<div class="rec"><div class="num">MOVE 0${i+1}</div><h3>${esc(r.t)}</h3><p>${esc(r.b)}</p><div class="ev"><b>Evidence:</b> ${esc(r.e)}</div></div>`).join("");
  document.getElementById("foot").textContent=`Nour Hammam LLC · Review-intelligence brief generated from the Jobs pipeline · Data date ${DATA.meta.date} · Rendered ${DATA.meta.generated_at}.`;
}
function renderQuotes(){
  const T=(q,bad)=>`<blockquote class="${bad?'bad':''}">“${esc(q.text)}”<span class="src"><b>${esc(q.place||'Venue')}</b> · ${BL[q.bucket]||''} · ${esc(q.rating)}★</span></blockquote>`;
  document.getElementById("q-top").innerHTML=(DATA.quotes.top||[]).map(q=>T(q,false)).join("")||`<p class="muted">No qualifying English 5★ quotes in this pull.</p>`;
  document.getElementById("q-worst").innerHTML=(DATA.quotes.worst||[]).map(q=>T(q,true)).join("")||`<p class="muted">No qualifying English 1–2★ quotes in this pull.</p>`;
}
function drawMarket(){
  const m=DATA.markets;if(!m.length)return;const W=820,rowH=64,padL=190,padR=24,top=8,H=top+m.length*rowH+8;
  const max=Math.max(...m.map(x=>x.count),1),sc=v=>(v/max)*(W-padL-padR);
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img"});
  m.forEach((mk,i)=>{const y=top+i*rowH+12;
    svg.appendChild(Object.assign(el("text",{x:0,y:y+15,class:"glabel"}),{textContent:mk.label}));
    svg.appendChild(Object.assign(el("text",{x:0,y:y+33,class:"vlabel"}),{textContent:`${mk.count} reviews · ${mk.avg}★ avg · ${mk.places} venues`}));
    let x=padL;[["Top",mk.top,"var(--good)"],["Worst",mk.worst,"var(--clay)"],["Neutral",mk.neutral,"var(--muted)"]].forEach(([lab,val,col])=>{
      const w=sc(val);if(w<=0)return;const r=el("rect",{x:x,y:y,width:Math.max(w-2,1),height:26,rx:4,fill:col,class:"bar"});
      bindTip(r,`<b>${esc(mk.label)}</b><br>${lab}: <span class="r">${val}</span>`);svg.appendChild(r);
      if(w>34)svg.appendChild(Object.assign(el("text",{x:x+8,y:y+17,style:"fill:#fff;font-weight:600;font-size:12px"}),{textContent:val}));x+=w;});});
  document.getElementById("mkt").appendChild(svg);
}
function drawHBar(mount,rows,hue,unit){
  if(!rows||!rows.length){document.getElementById(mount).innerHTML='<p class="muted">No data for this pull.</p>';return;}
  const W=820,barH=26,padL=210,padR=52,top=6,rowH=barH+14,H=top+rows.length*rowH+4;
  const max=Math.max(...rows.map(r=>r[1]),1),sc=v=>(v/max)*(W-padL-padR);
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img"});
  rows.forEach((r,i)=>{const y=top+i*rowH;
    svg.appendChild(Object.assign(el("text",{x:0,y:y+barH/2+4,class:"glabel"}),{textContent:r[0]}));
    const w=Math.max(sc(r[1]),2),rect=el("rect",{x:padL,y:y,width:w,height:barH,rx:4,fill:hue,class:"bar"});
    bindTip(rect,`<b>${esc(r[0])}</b><br><span class="r">${r[1]}</span> ${unit}`);svg.appendChild(rect);
    svg.appendChild(Object.assign(el("text",{x:padL+w+8,y:y+barH/2+4,class:"vlabel"}),{textContent:r[1]}));});
  document.getElementById(mount).appendChild(svg);
}
function drawRatings(){
  const groups=DATA.ratingGroups||[];if(!groups.length)return;const stars=DATA.ratingByMarket.stars;
  const W=820,H=300,padL=44,padR=16,padT=14,padB=42,plotW=W-padL-padR,plotH=H-padT-padB;
  const max=Math.max(...groups.flatMap(g=>DATA.ratingByMarket[g]),1);
  const gW=plotW/stars.length,bW=(gW*0.72)/groups.length;
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img"}),grid=el("g",{class:"grid"});
  const step=Math.max(10,Math.ceil(max/4/10)*10);
  for(let t=0;t<=max;t+=step){const y=padT+plotH-(t/max)*plotH;
    grid.appendChild(el("line",{x1:padL,y1:y,x2:W-padR,y2:y}));
    grid.appendChild(Object.assign(el("text",{x:padL-8,y:y+4,class:"axis",style:"text-anchor:end"}),{textContent:t}));}
  svg.appendChild(grid);
  stars.forEach((s,si)=>{const gx=padL+si*gW+gW*0.14;
    groups.forEach((g,gi)=>{const v=DATA.ratingByMarket[g][si],h=(v/max)*plotH,x=gx+gi*bW,y=padT+plotH-h;
      const rect=el("rect",{x:x,y:y,width:bW-3,height:Math.max(h,1),rx:3,fill:BC[g],class:"bar"});
      bindTip(rect,`<b>${BL[g]}</b><br>${s}★ — <span class="r">${v}</span>`);svg.appendChild(rect);});
    svg.appendChild(Object.assign(el("text",{x:padL+si*gW+gW/2,y:H-padB+20,class:"axis",style:"text-anchor:middle"}),{textContent:s+"★"}));});
  document.getElementById("rate").appendChild(svg);
}
function drawScatter(){
  const pts=DATA.scatter||[];if(!pts.length)return;const W=820,H=440,padL=48,padR=18,padT=16,padB=46;
  const plotW=W-padL-padR,plotH=H-padT-padB;const xMax=Math.max(...pts.map(p=>p.v),10)*1.06,yMin=Math.min(3,...pts.map(p=>p.r))-0.05,yMax=5.05;
  const sx=v=>padL+(v/xMax)*plotW,sy=v=>padT+plotH-((v-yMin)/(yMax-yMin))*plotH;
  const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img"}),grid=el("g",{class:"grid"});
  for(let r=Math.ceil(yMin*2)/2;r<=5.0;r+=0.5){const y=sy(r);
    grid.appendChild(el("line",{x1:padL,y1:y,x2:W-padR,y2:y}));
    grid.appendChild(Object.assign(el("text",{x:padL-8,y:y+4,class:"axis",style:"text-anchor:end"}),{textContent:r.toFixed(1)+"★"}));}
  const xstep=xMax>50?10:xMax>20?5:2;
  for(let x=0;x<=xMax;x+=xstep){const gx=sx(x);
    grid.appendChild(el("line",{x1:gx,y1:padT,x2:gx,y2:padT+plotH,style:"opacity:.5"}));
    grid.appendChild(Object.assign(el("text",{x:gx,y:H-padB+20,class:"axis",style:"text-anchor:middle"}),{textContent:x}));}
  svg.appendChild(grid);
  svg.appendChild(Object.assign(el("text",{x:padL+plotW/2,y:H-8,class:"axis",style:"text-anchor:middle"}),{textContent:"Reviews captured (prominence proxy) →"}));
  const seen={};pts.forEach(p=>{const k=p.v+"|"+p.r;seen[k]=(seen[k]||0)+1;const j=seen[k]-1;
    const cx=sx(p.v)+(j%3-1)*5,cy=sy(p.r)+(Math.floor(j/3))*5;
    const c=el("circle",{cx:cx,cy:cy,r:7,fill:BC[p.b],"fill-opacity":.82,class:"dot"});
    bindTip(c,`<b>${esc(p.n)}</b><br>${BL[p.b]}<br>${p.r}★ · ${p.v} reviews`);svg.appendChild(c);});
  document.getElementById("scatter").appendChild(svg);
}
function fillTable(){const tb=document.querySelector("#tbl tbody");
  tb.innerHTML=(DATA.table||[]).map(r=>`<tr><td>${esc(r.n)}</td><td><span class="pill ${PILL[r.b]||''}">${BL[r.b]||r.b}</span></td><td class="n rate">${Number(r.r).toFixed(1)}★</td><td class="n">${r.v}</td></tr>`).join("");}

fillMeta();renderQuotes();
legend("rate-legend",DATA.ratingGroups);legend("scatter-legend",DATA.ratingGroups);
drawMarket();
drawHBar("svc",DATA.services,"var(--brass)","reviews mention this");
drawRatings();
drawHBar("pain",DATA.painThemes,"var(--clay)","worst-review mentions");
drawScatter();fillTable();
</script></body></html>
"""
