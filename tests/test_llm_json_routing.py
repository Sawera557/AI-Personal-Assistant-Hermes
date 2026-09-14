"""Background JSON calls must not be pinned to one provider's billing page.

The regression these guard: memory extraction, conversation summaries, meeting
guidance and Drive classification all name Config.GUIDANCE_MODEL (a Claude
model). When that account emptied, every one of them stopped for a month while
chat ran fine on NVIDIA. Offline — all provider calls are stubbed.
"""
import anthropic
import httpx
import pytest

from hermes import credits, llm_json, models


def _credit_error():
    return anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
        body=None,
    )


@pytest.fixture(autouse=True)
def _clean():
    credits.clear()
    yield
    credits.clear()


def test_uses_the_requested_model_when_its_provider_is_funded():
    assert models.with_credit("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"


def test_falls_off_a_provider_that_is_out_of_credit():
    credits.note_failure("anthropic", _credit_error())
    chosen = models.with_credit("claude-haiku-4-5-20251001")
    assert models.provider_of(chosen) != "anthropic"


def test_the_configured_preference_is_never_rewritten():
    """Substitution is per-call. A top-up must restore the intended model with
    no restart and no config edit."""
    credits.note_failure("anthropic", _credit_error())
    assert models.with_credit("claude-opus-5") != "claude-opus-5"
    credits.clear()
    assert models.with_credit("claude-opus-5") == "claude-opus-5"


def test_json_call_routes_to_a_working_provider(monkeypatch):
    credits.note_failure("anthropic", _credit_error())
    seen = {}

    def fake_openai(provider, model_id, system, user_message, max_tokens):
        seen["provider"], seen["model"] = provider, model_id
        return '{"memories": ["teal"]}'

    def fake_anthropic(*a, **kw):
        raise AssertionError("must not call Anthropic while it is out of credit")

    monkeypatch.setattr(llm_json, "_call_openai_protocol", fake_openai)
    monkeypatch.setattr(llm_json, "_call_anthropic", fake_anthropic)

    out = llm_json.call_json(
        system="s", user_message="u", max_tokens=50, fallback_key="_raw",
        model="claude-haiku-4-5-20251001",
    )
    assert out == {"memories": ["teal"]}
    assert seen["provider"] != "anthropic"


def test_background_never_calls_anthropic_even_when_asked_to(monkeypatch):
    """Operator policy: background work runs on NVIDIA only. A stale
    HERMES_GUIDANCE_MODEL=claude-... in a deployed .env must not drag per-turn
    jobs back onto a metered account."""
    def fake_anthropic(*a, **kw):
        raise AssertionError("background work must never call Anthropic")

    seen = {}

    def fake_openai(provider, model_id, system, user_message, max_tokens):
        seen["provider"], seen["model"] = provider, model_id
        return '{"ok": true}'

    monkeypatch.setattr(llm_json, "_call_anthropic", fake_anthropic)
    monkeypatch.setattr(llm_json, "_call_openai_protocol", fake_openai)

    out = llm_json.call_json(
        system="s", user_message="u", max_tokens=50, fallback_key="_raw",
        model="claude-haiku-4-5-20251001",
    )
    assert out == {"ok": True}
    assert seen["provider"] == "nvidia"


def test_a_dry_background_model_retries_on_another_nvidia_model(monkeypatch):
    """The first caller to discover an exhausted model still gets an answer,
    rather than becoming the sacrificial request — and stays on NVIDIA."""
    calls = []

    def fake_openai(provider, model_id, system, user_message, max_tokens):
        calls.append(model_id)
        if len(calls) == 1:
            raise _credit_error()
        return '{"ok": true}'

    monkeypatch.setattr(llm_json, "_call_openai_protocol", fake_openai)
    monkeypatch.setattr(
        llm_json, "_call_anthropic",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("never Anthropic")),
    )

    out = llm_json.call_json(
        system="s", user_message="u", max_tokens=50, fallback_key="_raw",
    )
    assert out == {"ok": True}
    assert len(calls) == 2
    assert calls[0] != calls[1]  # moved to a different model, not a blind retry
    assert all(models.provider_of(m) == "nvidia" for m in calls)


def test_one_dry_nim_key_does_not_disable_the_other_models():
    """NIM issues a key per model, so exhaustion is per key. A dry Kimi balance
    must not take DeepSeek and Nemotron down with it."""
    kimi = "moonshotai/kimi-k3"
    credits.note_failure(credits.scope_for("nvidia", kimi), _credit_error())
    assert not models.is_available(kimi)
    assert models.is_available("deepseek-ai/deepseek-v4-flash-0731")
    assert models.background_model() != kimi


def test_raises_rather_than_borrowing_a_forbidden_provider(monkeypatch):
    """With every permitted model dry it must fail loudly, NOT quietly fall back
    to Claude. Callers here store what they get back, so a fabricated result
    would write a wrong record — and a silent provider switch breaches policy."""
    for model_id in models.BACKGROUND_MODELS:
        credits.note_failure(credits.scope_for("nvidia", model_id), _credit_error())
    monkeypatch.setattr(
        llm_json, "_call_anthropic",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("never Anthropic")),
    )
    with pytest.raises(credits.CreditsExhausted):
        llm_json.call_json(
            system="s", user_message="u", max_tokens=50, fallback_key="_raw",
            model="claude-haiku-4-5-20251001",
        )


def test_background_model_list_excludes_metered_providers():
    assert all(models.provider_of(m) == "nvidia" for m in models.BACKGROUND_MODELS)
    assert all(models.provider_of(m) == "nvidia" for m in models.BACKGROUND_VISION_MODELS)


def test_preferred_can_reorder_the_allowed_list_but_never_extend_it():
    """gpt-oss is NIM-served (allowed provider) yet OpenAI-lineage (excluded
    model) — checking the provider alone would let it slip through."""
    assert models.background_model(preferred="openai/gpt-oss-20b") != "openai/gpt-oss-20b"
    allowed = "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert models.background_model(preferred=allowed) == allowed


def test_vision_background_models_can_actually_see():
    """OCR needs an image-capable model; the text list is ordered for speed and
    its fastest entry is text-only."""
    assert set(models.BACKGROUND_VISION_MODELS) <= set(models.BACKGROUND_MODELS)
    assert models.background_model(vision=True) in models.BACKGROUND_VISION_MODELS


def test_parses_json_wrapped_in_a_code_fence():
    assert llm_json._parse('```json\n{"a": 1}\n```', "_raw") == {"a": 1}


def test_parses_json_with_prose_around_it():
    assert llm_json._parse('Sure! {"a": 1} hope that helps', "_raw") == {"a": 1}


def test_unparseable_output_degrades_instead_of_raising():
    assert llm_json._parse("not json at all", "_raw") == {"_raw": "not json at all"}


def test_legacy_name_still_works():
    """Four modules still import call_claude_json; the rename isn't a flag day."""
    assert llm_json.call_claude_json is llm_json.call_json


def test_a_throttled_model_falls_through_to_the_next(monkeypatch):
    """Free-tier NIM endpoints time out and 429 routinely — measured repeatedly.
    That is the dominant failure mode here, so it must fall through like credit
    exhaustion does, or one slow model kills every background job."""
    import openai as _openai

    tried = []

    def fake_openai(provider, model_id, system, user_message, max_tokens):
        tried.append(model_id)
        if len(tried) == 1:
            raise _openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
        if len(tried) == 2:
            raise _openai.RateLimitError(
                "429 Too Many Requests",
                response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                body=None,
            )
        return '{"ok": true}'

    monkeypatch.setattr(llm_json, "_call_openai_protocol", fake_openai)
    out = llm_json.call_json(
        system="s", user_message="u", max_tokens=50, fallback_key="_raw")
    assert out == {"ok": True}
    assert len(tried) == 3 and len(set(tried)) == 3  # three DIFFERENT models


def test_a_throttle_does_not_mark_a_model_out_of_credit(monkeypatch):
    """Throttled and broke are different conditions with different remedies."""
    import openai as _openai

    monkeypatch.setattr(
        llm_json, "_call_openai_protocol",
        lambda *a, **kw: (_ for _ in ()).throw(
            _openai.APITimeoutError(request=httpx.Request("POST", "https://x"))),
    )
    with pytest.raises(RuntimeError):
        llm_json.call_json(system="s", user_message="u", max_tokens=50, fallback_key="_raw")
    assert all(models.is_available(m) for m in models.BACKGROUND_MODELS)


def test_a_genuine_bug_is_raised_not_retried_across_every_model(monkeypatch):
    """A TypeError in our own code must surface immediately, not be disguised as
    'all models unavailable' after five pointless round trips."""
    calls = []

    def boom(provider, model_id, system, user_message, max_tokens):
        calls.append(model_id)
        raise TypeError("bug in our code")

    monkeypatch.setattr(llm_json, "_call_openai_protocol", boom)
    with pytest.raises(TypeError):
        llm_json.call_json(system="s", user_message="u", max_tokens=50, fallback_key="_raw")
    assert len(calls) == 1
