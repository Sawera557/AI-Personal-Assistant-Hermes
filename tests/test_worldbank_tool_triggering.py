"""Eval harness: does the agent select `worldbank_data` when it should (and not
when it shouldn't)?

This measures TOOL SELECTION — the thing "is the tool called" actually means —
by replaying the agent's *first decision* per question: same system prompt
(hermes.agent._system_prompt) and same tool list (hermes.tools.build_tools) the
live agent uses, same extended-thinking config as hermes.agent._anthropic_turn,
one Messages API call each, no tools executed. It then checks whether
`worldbank_data` is among the tool_use blocks the model emitted.

Run:
    PYTHONPATH=. python tests/test_worldbank_tool_triggering.py
Optional env:
    HERMES_TEST_MODEL   force a model id (default: hermes.models.active_model())
    HERMES_TEST_SCOPE   business slug to scope the turn to (default: afk / Afro Farm)
    HERMES_TEST_WORKERS concurrency (default 6)

Writes tests/worldbank_tool_results.json and prints a summary + every failure.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

# Windows consoles default to cp1252 and choke on non-Latin-1 output; force UTF-8
# so the summary (and any unicode in questions) prints regardless of terminal.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — older streams without reconfigure
    pass

from hermes import models
from hermes.agent import _system_prompt
from hermes.config import Config
from hermes.tools import build_tools

_HERE = Path(__file__).resolve().parent
_QUESTIONS = _HERE / "worldbank_tool_questions.json"
_RESULTS = _HERE / "worldbank_tool_results.json"

TOOL = "worldbank_data"
SCOPE = os.getenv("HERMES_TEST_SCOPE", "afk")
MODEL = os.getenv("HERMES_TEST_MODEL") or models.active_model()
WORKERS = int(os.getenv("HERMES_TEST_WORKERS", "6"))

_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
_TOOLS = build_tools(SCOPE)
_SYSTEM = _system_prompt(voice=False, active_business=SCOPE)


def _first_decision(question: str) -> tuple[list[str], str]:
    """Return (tool_names_selected, text_preview) for the model's first turn."""
    extra: dict = {}
    if "haiku" not in MODEL.lower():
        extra["thinking"] = {"type": "adaptive", "display": "summarized"}
        extra["output_config"] = {"effort": Config.EFFORT}
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        tools=_TOOLS,
        messages=[{"role": "user", "content": question}],
        **extra,
    )
    tools_used = [b.name for b in resp.content if getattr(b, "type", None) == "tool_use"]
    text = " ".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    return tools_used, text[:200]


def _verdict(category: str, expected: str, triggered: bool) -> str:
    want = {"positive": "yes", "negative": "no"}.get(category, expected)
    if want == "either":
        return "INFO"
    if want == "yes":
        return "PASS" if triggered else "FAIL"
    return "PASS" if not triggered else "FAIL"  # want == "no"


def _evaluate(item: dict, category: str) -> dict:
    expected = item.get("expected", {"positive": "yes", "negative": "no"}.get(category, "either"))
    try:
        tools_used, preview = _first_decision(item["question"])
        triggered = TOOL in tools_used
        return {
            "id": item["id"],
            "category": category,
            "question": item["question"],
            "expected": expected,
            "triggered": triggered,
            "tools_selected": tools_used,
            "verdict": _verdict(category, expected, triggered),
            "note": item.get("note", ""),
            "text_preview": preview,
        }
    except Exception as exc:  # noqa: BLE001 — record, don't abort the whole run
        return {
            "id": item["id"], "category": category, "question": item["question"],
            "expected": expected, "triggered": None, "tools_selected": [],
            "verdict": "ERROR", "note": str(exc), "text_preview": "",
        }


def main() -> None:
    data = json.loads(_QUESTIONS.read_text(encoding="utf-8"))
    jobs = (
        [(it, "positive") for it in data["positive"]]
        + [(it, "negative") for it in data["negative"]]
        + [(it, "edge") for it in data["edge"]]
    )
    print(f"Model: {MODEL}   Scope: {SCOPE}   Questions: {len(jobs)}   Workers: {WORKERS}\n")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda j: _evaluate(*j), jobs))
    results.sort(key=lambda r: r["id"])

    # Persist the artifact FIRST, so a later print/encoding hiccup can't lose it.
    _RESULTS.write_text(
        json.dumps({"model": MODEL, "scope": SCOPE, "results": results}, indent=2),
        encoding="utf-8",
    )

    # ---- Summary ----
    cats = ("positive", "negative", "edge")
    print("=" * 78)
    for cat in cats:
        rows = [r for r in results if r["category"] == cat]
        p = sum(r["verdict"] == "PASS" for r in rows)
        f = sum(r["verdict"] == "FAIL" for r in rows)
        info = sum(r["verdict"] == "INFO" for r in rows)
        err = sum(r["verdict"] == "ERROR" for r in rows)
        scored = p + f
        rate = f"{p}/{scored}" + (f" ({100*p//scored}%)" if scored else "")
        extra = f"  [{info} either, {err} err]" if (info or err) else ""
        print(f"  {cat:9s}: PASS {rate}{extra}")

    fails = [r for r in results if r["verdict"] in ("FAIL", "ERROR")]
    print("=" * 78)
    if fails:
        print(f"\n{len(fails)} FAILURE(S):")
        for r in fails:
            print(f"  [{r['id']} {r['verdict']}] {r['question']}")
            print(f"        expected={r['expected']} triggered={r['triggered']} "
                  f"tools={r['tools_selected']}")
    else:
        print("\nNo failures. All scored questions passed.")

    # ---- 'either' edge cases: report what happened (not scored) ----
    infos = [r for r in results if r["verdict"] == "INFO"]
    if infos:
        print(f"\n'EITHER' edge cases (informational — {len(infos)}):")
        for r in infos:
            print(f"  [{r['id']}] triggered={r['triggered']} tools={r['tools_selected']}")
            print(f"        {r['question']}")

    print(f"\nFull results -> {_RESULTS}")


if __name__ == "__main__":
    main()
