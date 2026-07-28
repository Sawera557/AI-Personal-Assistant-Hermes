"""END-TO-END test of truncation through the real MatonClient code path.

This does not test `_truncate` in isolation — it drives the actual production
call chain the agent uses:

    MatonClient.request()  ->  _envelope()  ->  _truncate()
       -> json.dumps(result)   # exactly what dispatch() at tools.py:2172 sends
                               #   to the model

The HTTP layer (`requests.request`) is monkeypatched to return an oversized
Gmail-style body, so no network or API key is involved. We then assert on the
*serialised bytes the model would actually receive* — the real question the 25
Jul incident turned on.

Run directly:

    python tests/test_maton_truncation_integration.py
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


class _FakeHTTPResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


# A unique token that lives ONLY in "question 27", placed deep past the leaf cap.
SECRET = "SECRET_Q27::120_staff_names_phones_ID_numbers_wage_rates::KES74000_chilli_refund_due_today"


def _gmail_body(num_questions: int, per_question_chars: int) -> dict:
    """Build a Gmail messages.get-style body with an embedded secret in Q27."""
    lines = []
    for i in range(1, num_questions + 1):
        tail = f"  {SECRET}" if i == 27 else ""
        lines.append(f"Question {i}: " + ("detail " * (per_question_chars // 7)) + tail)
    body_text = "\n".join(lines)
    return {
        "id": "msg_incident_2026_07_25",
        "threadId": "thread_abc",
        "payload": {"headers": [{"name": "Subject", "value": "31 questions"}],
                    "body": {"size": len(body_text), "data": body_text}},
        "sizeEstimate": len(body_text),
    }


def _run_request(payload):
    """Drive the real MatonClient.request with the HTTP layer stubbed out."""
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["url"] = url
        return _FakeHTTPResponse(payload)

    orig = maton.requests.request
    maton.requests.request = fake_request
    try:
        client = maton.MatonClient(api_key="test-key", base_url="https://api.maton.ai")
        return client.request("google-mail", "GET",
                              "/gmail/v1/users/me/messages/msg_incident_2026_07_25")
    finally:
        maton.requests.request = orig


def test_incident_replay_email_now_returned_complete():
    # ~2k chars x 31 questions ~= 62k serialised: the incident size, now UNDER
    # the 120k ceiling, so it comes back whole.
    payload = _gmail_body(num_questions=31, per_question_chars=2_000)
    result = _run_request(payload)
    wire = json.dumps(result, default=str)  # what dispatch() sends to the model
    check("no truncation on a 62k email", "_truncation" not in result)
    check("no <<TRUNCATED>> marker present", "<<TRUNCATED" not in wire)
    check("question 27's content IS present (model really read it)", SECRET in wire)
    check("envelope shape intact", result["ok"] is True and result["status"] == 200)
    check("body stays an indexable dict", isinstance(result["data"]["payload"]["body"]["data"], str))


def test_huge_email_truncates_honestly_and_drops_the_secret():
    # 31 questions x ~12k chars ~= 370k serialised: over the ceiling. The secret
    # in Q27 sits far past the 40k leaf cap and must be physically dropped.
    payload = _gmail_body(num_questions=31, per_question_chars=12_000)
    result = _run_request(payload)
    wire = json.dumps(result, default=str)
    check("_truncation report is the first key of the envelope",
          list(result.keys())[0] == "_truncation")
    check("in-band <<TRUNCATED>> marker present at the cut", "<<TRUNCATED" in wire)
    check("question 27's content is PHYSICALLY ABSENT (cannot be fabricated from)",
          SECRET not in wire)
    check("report tells the model it has not seen the rest",
          "NOT seen" in result["_truncation"]["warning"])
    check("body is STILL an indexable dict (type preserved, not a preview blob)",
          isinstance(result["data"], dict) and "payload" in result["data"])
    check("what the model receives stays under the ceiling",
          len(wire) <= maton._MAX_RESULT_CHARS + 5_000)  # + small envelope overhead


def test_upstream_error_body_still_passes_through():
    # A 404 error body should be surfaced intact, not swallowed by truncation.
    err = {"error": {"code": 404, "message": "Not Found"}}
    captured = {}

    def fake_request(method, url, **kwargs):
        return _FakeHTTPResponse(err, status_code=404)

    orig = maton.requests.request
    maton.requests.request = fake_request
    try:
        client = maton.MatonClient(api_key="k", base_url="https://api.maton.ai")
        result = client.request("google-mail", "GET", "/gmail/v1/users/me/messages/nope")
    finally:
        maton.requests.request = orig
    check("error status surfaced", result["status"] == 404 and result["ok"] is False)
    check("error body intact", result["data"]["error"]["code"] == 404)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\nPASS: {_passed} checks passed across {len(tests)} tests.")
