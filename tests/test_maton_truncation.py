"""Tests for STRUCTURE-PRESERVING, LOUD truncation of Maton tool results.

Regression lock for the 25 Jul 2026 fabrication incident: a Gmail response was
sliced at 20,000 chars mid-payload, the model never saw the tail, and it
narrated questions that lay past the cut as if it had read them.

The old `_truncate` serialise-then-sliced, which:
  * turned a truncated list into a dict (type destruction),
  * produced invalid mid-object JSON in `preview`,
  * left NO in-band signal at the cut, and
  * wrote `_truncated` that nothing ever read.

These tests prove the replacement keeps types indexable, stays valid JSON,
marks every cut in-band, hoists a report to the top of the envelope, and — the
whole point — physically drops the omitted content so it cannot be "read".

Run directly:

    python tests/test_maton_truncation.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("HERMES_DISABLE_DB", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes import maton  # noqa: E402

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


class _FakeResp:
    """Minimal stand-in for requests.Response for _envelope()."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload

    @property
    def text(self):
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)


def test_small_payload_is_untouched():
    data = {"messages": [{"id": "1", "snippet": "hi"}]}
    out, report = maton._truncate(data)
    check("small payload passes through unchanged", out == data)
    check("small payload has no report", report is None)


def test_over_cap_dict_stays_indexable_dict():
    # A Gmail-style body whose single big field blows the cap.
    big = "Q. " + ("word " * 60_000)  # ~300k chars, well over the ceiling
    data = {"messages": [{"id": "abc", "payload": {"body": {"data": big}}}]}
    out, report = maton._truncate(data)
    check("stays a dict", isinstance(out, dict))
    check("nested access still works", out["messages"][0]["id"] == "abc")
    check("report is present", report is not None and report["truncated"] is True)
    check("valid JSON round-trip", json.loads(json.dumps(out, default=str)) is not None)


def test_over_cap_top_level_list_stays_a_list():
    # The `resources[0]["id"]` case from the ADR: a truncated list must NOT
    # become a dict.
    data = [{"id": str(i), "blob": "x" * 5_000} for i in range(1200)]
    out, report = maton._truncate(data)
    check("top-level list stays a list", isinstance(out, list))
    check("first element still indexable as a dict", out[0]["id"] == "0")
    check("list length capped", len(out) <= maton._MAX_LIST_ITEMS)
    check("a list cut is recorded", any(c["kind"] == "list" for c in report["cuts"]))


def test_marker_is_in_band_and_content_past_it_is_gone():
    secret = "KES 74000 chilli refund due today to 120 staff"
    big = ("filler " * 40_000) + secret  # secret sits past the leaf cap
    data = {"email": {"body": big}}
    out, report = maton._truncate(data)
    # Serialise the way dispatch() does (ensure_ascii=True) — the marker must
    # survive legibly on the real wire, so it is ASCII.
    blob = json.dumps(out, default=str)
    check("in-band TRUNCATED marker present", "<<TRUNCATED" in blob)
    check("content past the cut is physically absent", secret not in blob)
    check("report warns against reading past the cut", "NOT seen" in report["warning"])
    check("a string cut is recorded with byte counts",
          any(c["kind"] == "string" and c["omitted"] > 0 for c in report["cuts"]))


def test_pathological_fanout_stays_under_ceiling():
    # Thousands of medium strings that each clear the leaf cap individually.
    data = {"rows": [{"text": "y" * 1_000} for _ in range(400)]}
    # Force the branch by making the whole thing exceed the ceiling.
    data["rows"].extend({"text": "z" * 50_000} for _ in range(10))
    out, _ = maton._truncate(data)
    size = len(json.dumps(out, default=str, ensure_ascii=False))
    check("reduced payload is under the ceiling", size <= maton._MAX_RESULT_CHARS)


def test_envelope_hoists_truncation_to_top():
    big = "w" * 300_000
    resp = _FakeResp({"data": big})
    env = maton._envelope(resp)
    keys = list(env.keys())
    check("_truncation is the first key", keys[0] == "_truncation")
    check("envelope keeps status/ok/data", {"status", "ok", "data"} <= set(keys))
    check("data survives and stays a dict", isinstance(env["data"], dict))


def test_envelope_untruncated_has_no_report_key():
    resp = _FakeResp({"messages": [{"id": "1"}]})
    env = maton._envelope(resp)
    check("no _truncation key when nothing was cut", "_truncation" not in env)
    check("data passes through", env["data"] == {"messages": [{"id": "1"}]})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} checks passed across {len(tests)} tests.")
