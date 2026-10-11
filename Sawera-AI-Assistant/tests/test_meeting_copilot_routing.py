"""Meeting Copilot guidance must follow the background model policy.

The regression this guards: get_guidance / get_guidance_comprehensive called
Anthropic directly with Config.GUIDANCE_MODEL — an NVIDIA model id — so live
meeting guidance could never succeed. Offline: search, DB and providers stubbed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import credits, llm_json, meeting_copilot, models


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    credits.clear()
    monkeypatch.setattr(meeting_copilot, "semantic_search", lambda *a, **kw: [])
    monkeypatch.setattr(meeting_copilot, "_fetch_business_data", lambda *a, **kw: {})
    monkeypatch.setattr(
        llm_json, "_call_anthropic",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("guidance must never call Anthropic")),
    )
    yield
    credits.clear()


def _fake_nvidia(seen):
    def fake(provider, model_id, system, user_message, max_tokens):
        seen.append((provider, model_id))
        return "- Ask for the signed lease before quoting rent\n- Confirm the move-in date"
    return fake


@pytest.mark.parametrize("fn", ["get_guidance", "get_guidance_comprehensive"])
def test_guidance_runs_on_a_background_model_and_returns_bullets(monkeypatch, fn):
    seen = []
    monkeypatch.setattr(llm_json, "_call_openai_protocol", _fake_nvidia(seen))

    out = getattr(meeting_copilot, fn)(
        entity_id="nour_hammam", business_name="Nour Hammam LLC",
        latest_text="They asked when they can move in.",
    )

    assert out["bullets"] == ["Ask for the signed lease before quoting rent", "Confirm the move-in date"]
    assert seen and all(p == "nvidia" for p, _ in seen)
    assert seen[0][1] in models.BACKGROUND_MODELS


def test_guidance_falls_through_when_the_first_model_is_throttled(monkeypatch):
    import httpx
    import openai

    seen = []
    ok = _fake_nvidia(seen)

    def flaky(provider, model_id, system, user_message, max_tokens):
        if not seen:
            seen.append((provider, model_id))
            raise openai.RateLimitError(
                "throttled", response=httpx.Response(429, request=httpx.Request("POST", "https://x")), body=None,
            )
        return ok(provider, model_id, system, user_message, max_tokens)

    monkeypatch.setattr(llm_json, "_call_openai_protocol", flaky)

    out = meeting_copilot.get_guidance(
        entity_id="nour_hammam", business_name="Nour Hammam LLC", latest_text="Any update on the invoice?",
    )
    assert len(out["bullets"]) == 2
    assert seen[0][1] != seen[1][1]  # moved to another model, not a blind retry


def test_comprehensive_guidance_never_presents_placeholders_as_business_data(monkeypatch):
    """Unwired sources must read as unavailable — not as "(database lookup: …)"
    stand-ins the model then treats as real Q3 goals."""
    monkeypatch.undo()  # use the real _fetch_business_data
    monkeypatch.setattr(meeting_copilot, "semantic_search", lambda *a, **kw: [])
    monkeypatch.setattr(
        llm_json, "_call_anthropic",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("never Anthropic")),
    )
    prompts = []

    def fake(provider, model_id, system, user_message, max_tokens):
        prompts.append(system)
        return "- Ask what the tenant is proposing in exchange"

    monkeypatch.setattr(llm_json, "_call_openai_protocol", fake)
    meeting_copilot.get_guidance_comprehensive(
        entity_id="nour_hammam", business_name="Nour Hammam LLC", latest_text="Can we delay the increase?",
    )
    assert "(business data unavailable)" in prompts[0]
    assert "database lookup" not in prompts[0]
