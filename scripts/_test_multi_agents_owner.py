"""Owner-value tests for Multi-agents (docs/hermes/MULTI_AGENTS_PLAN.md §9.2).

Runs REAL agent turns (live model, live tools, real data) and checks each reply
against ground truth computed independently of the agent:
  * O1/O2 — the raw review CSVs (agent_tools) and the Steam & Signal workbook
  * O4    — a scan of every CSV for the asked-about place
  * O11   — a direct Open-Meteo call made in the same run

Writes docs/reports/MULTI_AGENTS_TEST_RESULTS.md. Each turn runs in a throwaway
session that is reset afterwards (same as hermes.scheduler), so nothing lands
in the owner's chat history.

    .venv/Scripts/python scripts/_test_multi_agents_owner.py [O1 O3 ...]
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import openpyxl  # noqa: E402

from hermes import agent_tools, models  # noqa: E402
from hermes.agent import run_turn, store  # noqa: E402
from hermes.db import agents as agents_db  # noqa: E402

WORKBOOK = ROOT / "downloads" / "Steam & Signal - Consolidated Review Intelligence (Nour Hammam, Aug 2-21 2026).xlsx"
OUT = ROOT / "docs" / "reports" / "MULTI_AGENTS_TEST_RESULTS.md"
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def tool_json(name, ti, biz):
    content, err = agent_tools.dispatch(name, ti, biz)
    return json.loads(content), err


# ---- ground truth -----------------------------------------------------------
def gt_mn_venues() -> dict[str, float]:
    wb = openpyxl.load_workbook(WORKBOOK, read_only=True)
    out = {}
    for r in wb["Venue Roll-up"].iter_rows(values_only=True):
        if r and len(r) > 7 and r[1] == "minnesota":
            out[str(r[0])] = float(r[7])  # rating — last seen
    return out


def all_review_rows() -> list[dict]:
    versions = agent_tools._available()
    loaded = agent_tools._load_many(sorted(versions), versions)
    return [r for rows in loaded.values() for r in rows]


# ---- checks -----------------------------------------------------------------
def check_o1(reply: str, _calls) -> list[tuple[bool, str]]:
    gt = gt_mn_venues()
    res = []
    lines = reply.splitlines()
    named = 0
    for venue, rating in gt.items():
        key = venue.lower()[:14]
        hit = [ln for ln in lines if key in ln.lower()]
        if not hit:
            continue
        named += 1
        ok = any(re.search(rf"(?<![\d.]){re.escape(f'{rating:.1f}')}(?![\d])", ln) for ln in hit)
        res.append((ok, f"{venue}: Google rating {rating:.1f} {'matches' if ok else 'NOT found on its line'}"))
    res.insert(0, (named >= 6, f"{named}/{len(gt)} workbook Minnesota venues named"))
    invented = [ln for ln in lines if re.search(r"\|\s*[A-Za-z].*\|\s*[1-5]\.\d\s*\|", ln)
                and not any(v.lower()[:10] in ln.lower() for v in gt)]
    res.append((not invented, f"rows naming venues not in the data: {len(invented)}"))
    return res


def check_o2(reply: str, _calls) -> list[tuple[bool, str]]:
    data, _ = tool_json("reviews_data", {"action": "analyze", "market": "general", "quotes": 0}, "nh")
    themes = data.get("complaint_themes_1_2_star") or []
    res = []
    for name, count in themes[:3]:
        line = next((ln for ln in reply.splitlines() if name.split(" /")[0].lower() in ln.lower()), "")
        ok = bool(line) and str(count) in line
        res.append((ok, f"theme '{name}' = {count} {'reported correctly' if ok else 'missing or wrong count'}"))
    texts = [(r.get("text") or "") for r in all_review_rows()]
    quotes = _quotes(reply)
    verified = [q for q in quotes if any(q[:40].lower().rstrip(".… ") in t.lower() for t in texts)]
    res.append((bool(quotes) and len(verified) == len(quotes),
                f"{len(verified)}/{len(quotes)} quotes found verbatim in the review CSVs"))
    res.append(check_numbers_traceable(reply))
    return res


_DATE_RE = re.compile(r"\b20\d\d[-‑/]\d\d[-‑/]\d\d\b|\b20\d\d\b")


def _traceable_numbers() -> set[int]:
    """Every integer the reviews tool reports for the whole corpus, plus sums of
    2–3 complaint themes (the arithmetic an agent may legitimately show)."""
    from itertools import combinations

    data, _ = tool_json("reviews_data", {"action": "analyze", "quotes": 0}, "nh")
    nums: set[int] = set()

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)) and float(x).is_integer():
            nums.add(int(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(data)
    for mk in ("minnesota", "somalia", "general"):
        walk(tool_json("reviews_data", {"action": "analyze", "market": mk, "quotes": 0}, "nh")[0])
    themes = [c for _, c in data.get("complaint_themes_1_2_star") or []]
    for k in (2, 3):
        nums.update(sum(c) for c in combinations(themes, k))
    nums.add(sum(themes))
    # Facts from the business context pack (addresses, acreage, staff count…)
    # are a legitimate source for any agent.
    pack = (ROOT / "HERMES_CONTEXT_PACKS.md").read_text(encoding="utf-8")
    nums.update(int(n) for n in re.findall(r"(?<![\d.])\d{2,5}(?![\d.])", pack))
    return nums


# A quotation opens after whitespace / start / ( : > and stays on one line, so
# the closing quote of "Booking / Wait time" never pairs with a later opener.
_QUOTE_RE = re.compile(r'(?:^|(?<=[\s(:>—-]))["“]([^"”\n]{25,}?)["”]', re.M)
_ADDRESS_RE = re.compile(r"\b\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+\.?\s+(?:St|Ave|Avenue|Street|Rd|Blvd)\b")
_ARITH_RE = re.compile(r"\b(\d[\d,]*)\s*\(\s*=\s*([\d,\s+]+)\)")


def _quotes(reply: str) -> list[str]:
    return [q.strip() for q in _QUOTE_RE.findall(reply)]


def check_numbers_traceable(reply: str) -> tuple[bool, str]:
    """Counts ≥10 must come from the data. Excluded: text inside quotations
    (verbatim review text), street addresses, and shown arithmetic whose
    parts add up — a correct `77 (= 20 + 18 + 39)` is the rule working."""
    text = _QUOTE_RE.sub(" ", reply)
    text = re.sub(r"^\s*>.*$", " ", text, flags=re.M)
    text = _ADDRESS_RE.sub(" ", text)
    bad_arith = []
    for total, parts in _ARITH_RE.findall(text):
        nums = [int(p.replace(",", "")) for p in re.findall(r"\d[\d,]*", parts)]
        if sum(nums) != int(total.replace(",", "")):
            bad_arith.append(f"{total} ≠ {parts.strip()}")
    text = _ARITH_RE.sub(" ", text)
    text = _DATE_RE.sub(" ", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*%", " ", text)        # percentages checked separately
    found = {int(n.replace(",", "")) for n in re.findall(r"(?<![\d.])\d{1,3}(?:,\d{3})+|(?<![\d.,])\d{2,}(?![\d.])", text)}
    found = {n for n in found if n >= 10}
    untraced = sorted(found - _traceable_numbers())
    msg = f"every count ≥10 traces to the data (untraced: {untraced or 'none'})"
    if bad_arith:
        msg += f"; wrong arithmetic: {bad_arith}"
    return (not untraced and not bad_arith, msg)


def check_o3(reply: str, _calls) -> list[tuple[bool, str]]:
    low = reply.lower()
    return [
        check_numbers_traceable(reply),
        (not re.search(r"ebitda|profit margin|net margin|\bmargin of\b", low), "no EBITDA / margin figure"),
        (not re.search(r"\b(cures?|heals?|treats? (anxiety|depression|pain)|detox(es|ifies) your)\b", low),
         "no medical claims"),
        (bool(re.search(r"women|female|modest", low)), "fits women-only / modesty model"),
        (len(re.findall(r"\b\d+\b", reply)) >= 3, "promises cite counts from the data"),
    ]


def check_o4(reply: str, _calls) -> list[tuple[bool, str]]:
    tokyo = [r for r in all_review_rows()
             if "tokyo" in ((r.get("place_name") or "") + (r.get("query") or "")).lower()]
    low = reply.lower()
    claims_count = re.search(r"\b[1-9]\d*\s+(reviews?|venues?)\b[^.\n]{0,40}tokyo", low)
    says_none = bool(re.search(r"\b(no|none|zero|not|isn't|doesn't|don't)\b", low))
    return [
        (len(tokyo) == 0, f"ground truth: {len(tokyo)} Tokyo rows in the corpus"),
        (says_none and not claims_count, "agent says there is no Tokyo data and invents no count"),
    ]


def check_research(min_urls: int):
    def _check(reply: str, calls) -> list[tuple[bool, str]]:
        urls = set(URL_RE.findall(reply))
        # Anthropic runs web_search/web_fetch server-side; they never appear in
        # tool_calls, so cited URLs are the evidence of web research.
        used_web = any(c.get("tool") in ("web_search", "web_fetch") for c in calls) or bool(urls)
        return [
            (len(urls) >= min_urls, f"{len(urls)} distinct source URLs cited (need ≥{min_urls})"),
            (used_web, "used web research"),
            (bool(re.search(r"confidence|gaps|uncertain|verify", reply, re.I)), "states confidence / gaps"),
        ]
    return _check


def check_o11(reply: str, _calls) -> list[tuple[bool, str]]:
    wx, _ = tool_json("weather_forecast", {"days": 14}, "afk")
    days = wx.get("days") or []
    res = [(len(days) == 14, f"direct Open-Meteo call returned {len(days)} days")]
    hits = 0
    for d in days[:5]:
        vals = {f"{d['temperature_2m_max']}", f"{d['precipitation_sum']}"}
        if any(v in reply for v in vals):
            hits += 1
    res.append((hits >= 3, f"{hits}/5 of the first days' rain/temperature values match the direct call"))
    return res


TESTS = [
    ("O1", "nh-market-analyst", "scorecard",
     "Rank the Minnesota venues by Google rating and reviews captured, for the pulls from 2026-08-02 to 2026-08-21.",
     check_o1),
    ("O2", "nh-market-analyst", "themes",
     "What are the top complaint themes in 1–2★ reviews for the General benchmark market? Give counts and quote two reviews.",
     check_o2),
    ("O3", "nh-market-analyst", "launch-promises",
     "What 3 promises should Nour Hammam make at launch?", check_o3),
    ("O4", "nh-market-analyst", "scorecard",
     "How many reviews do we have from spas in Tokyo?", check_o4),
    ("O9", "research", "regulation",
     "What Minnesota and Minneapolis licences does a women-only hammam spa that offers massage and body scrubs need?",
     check_research(3)),
    ("O10", "research", "deep",
     "GLOBALG.A.P. IFA certification for a Kenyan horticulture farm: steps, typical timeline and typical cost.",
     check_research(3)),
    ("O11", "afk-farm-ops", "weather",
     "Weather risk at the farm for the next 14 days.", check_o11),
]


def run_one(test):
    tid, slug, mode, prompt, check = test
    a = agents_db.get_agent(slug)
    key = f"{a['business'] or 'all'}:__agenttest__:{uuid.uuid4()}"
    t0 = time.time()
    try:
        result = run_turn(key, prompt, False, a["business"] or "all", agent=a, agent_mode=mode)
    except Exception as exc:  # noqa: BLE001 — a provider failure is a result, not a crash
        result = {"reply": "", "tool_calls": [], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            store.reset(key)
        except Exception:  # noqa: BLE001
            pass
    secs = round(time.time() - t0, 1)
    reply = result.get("reply") or ""
    calls = result.get("tool_calls") or []
    if result.get("error"):
        checks = [(False, f"turn failed: {result['error'][:200]}")]
    else:
        try:
            checks = check(reply, calls)
        except Exception as exc:  # noqa: BLE001
            checks = [(False, f"check crashed: {exc}")]
    return {
        "id": tid, "agent": a["name"], "mode": mode, "prompt": prompt, "secs": secs,
        "model": result.get("model_used"), "cost": result.get("cost_usd"),
        "tools": [c.get("tool") for c in calls], "reply": reply, "checks": checks,
        "passed": all(ok for ok, _ in checks) and bool(reply),
    }


def write_report(results):
    lines = [
        "# Multi-agents — owner-value test results",
        "",
        f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')} · model: "
        f"`{results[0]['model'] if results else models.resolve_active_model()[1]}` · "
        "plan: [MULTI_AGENTS_PLAN.md](../hermes/MULTI_AGENTS_PLAN.md) §9.2",
        "",
        "Each test is a real agent turn (live model, live tools, real data). Checks compare the "
        "reply with ground truth computed **independently of the agent**.",
        "",
        "| Test | Agent · mode | Result | Checks passed | Model that answered | Tools used | Time | Cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        n_ok = sum(1 for ok, _ in r["checks"] if ok)
        cost = f"${r['cost']:.4f}" if isinstance(r["cost"], (int, float)) else "—"
        # The model that ACTUALLY served the turn — a credit fallback can swap it
        # silently, which once made a "Claude" run's web tests run on Gemini.
        lines.append(
            f"| {r['id']} | {r['agent']} · {r['mode']} | {'✅ PASS' if r['passed'] else '❌ FAIL'} | "
            f"{n_ok}/{len(r['checks'])} | `{r['model'] or '—'}` | "
            f"{', '.join(dict.fromkeys(r['tools'])) or '—'} | {r['secs']}s | {cost} |")
    for r in results:
        lines += ["", f"## {r['id']} — {r['agent']} · {r['mode']}", "", f"**Prompt:** {r['prompt']}", ""]
        lines += [f"- {'✅' if ok else '❌'} {msg}" for ok, msg in r["checks"]]
        lines += ["", "<details><summary>Full reply</summary>", "", r["reply"] or "_(empty)_", "", "</details>"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    global OUT
    args = sys.argv[1:]
    if "--model" in args:
        # Pin the model for THIS process only (the app's platform setting is untouched).
        i = args.index("--model")
        model_id = args[i + 1]
        del args[i:i + 2]
        models._configured = lambda: model_id  # type: ignore[assignment]
        OUT = OUT.with_name(f"MULTI_AGENTS_TEST_RESULTS_{re.sub(r'[^a-z0-9]+', '-', model_id.lower()).strip('-')}.md")
    only = set(args)
    results = []
    for t in TESTS:
        if only and t[0] not in only:
            continue
        print(f"→ {t[0]} …", flush=True)
        r = run_one(t)
        print(f"  {'PASS' if r['passed'] else 'FAIL'} in {r['secs']}s · tools={r['tools']}", flush=True)
        for ok, msg in r["checks"]:
            print(f"    {'✓' if ok else '✗'} {msg}", flush=True)
        results.append(r)
        write_report(results)
    print(f"\nReport: {OUT}")


if __name__ == "__main__":
    main()
