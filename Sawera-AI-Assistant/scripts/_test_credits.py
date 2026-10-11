"""Behaviour when a provider runs out of API credit.

Two guarantees, both requested explicitly:
  1. a provider with no credit is not used by default;
  2. when one is hit anyway, the user gets "API credits are out ..." rather
     than a 500 and a stack trace.

Section 1 uses synthetic provider errors so detection is tested without needing
a broke account. Section 2 runs real turns; it only exercises the live path when
the Anthropic account actually is out of credit, and says so otherwise.
"""
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import httpx  # noqa: E402
import openai  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
from hermes import credits, models  # noqa: E402

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    d = str(detail).encode("ascii", "replace").decode("ascii")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {d}" if d else ""), flush=True)


_req = httpx.Request("POST", "https://example.invalid/v1")


def _resp(code):
    return httpx.Response(code, request=_req, json={})


ANTH_CREDIT = anthropic.BadRequestError(
    "Error code: 400 - {'type':'error','error':{'type':'invalid_request_error',"
    "'message':'Your credit balance is too low to access the Anthropic API.'}}",
    response=_resp(400), body=None,
)
OAI_QUOTA = openai.RateLimitError(
    "Error code: 429 - {'error':{'message':'You exceeded your current quota,"
    " please check your plan and billing details.','type':'insufficient_quota'}}",
    response=_resp(429), body=None,
)

# ── 1. Detection, memory, expiry, wording ──────────────────────────────────
print("1. Detection across providers")
check("anthropic 'credit balance too low'", credits.is_credit_error(ANTH_CREDIT))
check("openai 'insufficient_quota'", credits.is_credit_error(OAI_QUOTA))
check("402 payment required",
      credits.is_credit_error(openai.APIStatusError("x", response=_resp(402), body=None)))
# The important negatives: these must NOT disable a provider.
check("ordinary 429 throttle is NOT credit exhaustion",
      not credits.is_credit_error(openai.RateLimitError(
          "Error code: 429 - {'error':{'message':'Rate limit reached for requests',"
          "'type':'requests'}}", response=_resp(429), body=None)))
check("timeout is NOT credit exhaustion",
      not credits.is_credit_error(openai.APITimeoutError(request=_req)))
check("bad api key is NOT credit exhaustion",
      not credits.is_credit_error(Exception("invalid x-api-key")))

print("\n2. Remembering it, and forgetting it again")
credits.clear()
check("starts clean", not credits.exhausted("anthropic"))
check("note_failure reports a credit error", credits.note_failure("anthropic", ANTH_CREDIT))
check("provider now flagged", credits.exhausted("anthropic"))
check("other providers unaffected",
      not credits.exhausted("nvidia") and not credits.exhausted("openai"))
check("note_failure ignores unrelated errors", not credits.note_failure("nvidia", Exception("boom")))
try:
    credits.check("anthropic")
    check("check() raises", False)
except credits.CreditsExhausted as exc:
    check("check() raises CreditsExhausted", True, f"provider={exc.provider}")
check("check() stays silent for a healthy provider", credits.check("nvidia") is None)
_ttl = credits.TTL_SECONDS
credits.TTL_SECONDS = 0
check("state expires on its own (no restart needed)", not credits.exhausted("anthropic"))
credits.TTL_SECONDS = _ttl

print("\n3. The message is actionable")
msg = credits.message("anthropic")
check("leads with 'API credits are out'", msg.startswith("API credits are out"), repr(msg[:40]))
check("names the provider", "Anthropic" in msg)
check("says what to do about it", "Add credit" in msg and "Settings" in msg)

print("\n4. Default selection avoids the dead provider")
credits.clear()
models.set_active_model("claude-haiku-4-5-20251001")
check("healthy: honours the stored pick", models.active_model() == "claude-haiku-4-5-20251001")
credits.note_failure("anthropic", ANTH_CREDIT)
got = models.active_model()
check("exhausted: picks a different provider", models.provider_of(got) != "anthropic", f"-> {got}")
check("stored preference is NOT overwritten", models._configured() == "claude-haiku-4-5-20251001")
credits.clear()
check("reverts to the stored pick after top-up",
      models.active_model() == "claude-haiku-4-5-20251001")

print("\n5. Picker marks unavailable models")
credits.note_failure("anthropic", ANTH_CREDIT)
opts = models.options()
anth = [o for o in opts if o["provider"] == "anthropic"]
rest = [o for o in opts if o["provider"] != "anthropic"]
check("anthropic options unavailable", all(not o["available"] for o in anth), f"{len(anth)} options")
check("label explains why", all("no API credit" in o["label"] for o in anth))
check("other providers still available", all(o["available"] for o in rest), f"{len(rest)} options")
credits.clear()

# ── 2. Live behaviour through the HTTP API ─────────────────────────────────
client = TestClient(A.app)


def anthropic_is_broke():
    try:
        from hermes import agent
        agent._anthropic().messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=4,
            messages=[{"role": "user", "content": "hi"}])
        return False
    except Exception as exc:  # noqa: BLE001
        return credits.is_credit_error(exc)


credits.clear()
BROKE = anthropic_is_broke()
print(f"\n6. Live turn on a real out-of-credit account "
      f"({'Anthropic is out of credit' if BROKE else 'SKIPPED - Anthropic has credit'})")
if BROKE:
    from hermes.db import settings as ds
    models._override = None
    ds.set("active_model", "claude-haiku-4-5-20251001")  # as if credit ran out after picking it
    r = client.post("/api/chat",
                    json={"session_id": "cred-" + uuid.uuid4().hex[:6], "message": "Hello"})
    check("200, not a 500", r.status_code == 200, f"status={r.status_code}")
    d = r.json() if r.status_code == 200 else {}
    check("reply explains the billing state",
          "API credits are out" in str(d.get("reply") or ""), repr(str(d.get("reply"))[:60]))
    check("stop_reason records it", d.get("stop_reason") == "credits_exhausted", d.get("stop_reason"))
    check("charged nothing", d.get("cost_usd") == 0.0, d.get("cost_usd"))
    check("provider flagged for next time", credits.exhausted("anthropic"))

    print("\n7. Once flagged, that model stops being used at all")
    r2 = client.post("/api/chat", json={"session_id": "cred-" + uuid.uuid4().hex[:6],
                                        "message": "Reply with exactly: PONG"})
    d2 = r2.json()
    check("answers for real instead of repeating the error",
          "API credits are out" not in str(d2.get("reply") or ""), repr(str(d2.get("reply"))[:40]))
    check("ran on a working provider",
          models.provider_of(str(d2.get("model_used"))) != "anthropic",
          f"model_used={d2.get('model_used')}")
    check("stored preference still Claude", models._configured() == "claude-haiku-4-5-20251001")

    print("\n8. Streaming endpoint behaves the same")
    r3 = client.post("/api/chat/stream", json={"session_id": "cred-" + uuid.uuid4().hex[:6],
                                               "message": "Name one colour. One word."})
    kinds, done_reply = {}, ""
    for line in r3.text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:])
        except ValueError:
            continue
        kinds[ev.get("type")] = kinds.get(ev.get("type"), 0) + 1
        if ev.get("type") == "done":
            done_reply = str(ev.get("reply") or (ev.get("result") or {}).get("reply") or "")
    check("no raw 'error' event reaches the client", "error" not in kinds, str(kinds))
    check("stream produces a real answer", bool(done_reply.strip()), repr(done_reply[:40]))

    print("\n9. Picker refuses a model with no credit")
    r4 = client.post("/api/settings/model", json={"model": "claude-opus-5"})
    check("400 with a readable reason",
          r4.status_code == 400 and "API credits are out" in str(r4.json().get("error", "")),
          f"{r4.status_code}")
    check("stored setting unchanged by the refusal",
          models._configured() == "claude-haiku-4-5-20251001")
    api_opts = client.get("/api/settings/model").json()["options"]
    check("API exposes availability", all("available" in o for o in api_opts))
    check("a funded model is still selectable",
          client.post("/api/settings/model",
                      json={"model": "nvidia/nemotron-3.5-lightning-30b-a3b"}).status_code == 200)

    print("\n10. Funded models are untouched by any of this")
    r5 = client.post("/api/chat", json={"session_id": "cred-" + uuid.uuid4().hex[:6],
                                        "message": "Reply with exactly: PONG"})
    d5 = r5.json()
    check("normal reply", "PONG" in str(d5.get("reply") or ""), repr(str(d5.get("reply"))[:30]))
    check("normal billing", (d5.get("cost_usd") or 0) > 0, d5.get("cost_usd"))

print("\n" + "=" * 66)
print(f"{len(ok)}/{len(ok) + len(bad)} checks passed")
for b in bad:
    print("FAIL:", b)
if not BROKE:
    print("NOTE: live sections skipped - the Anthropic account has credit.")
sys.exit(1 if bad else 0)
