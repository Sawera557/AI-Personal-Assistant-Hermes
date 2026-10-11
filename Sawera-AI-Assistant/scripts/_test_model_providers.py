"""Functional tests for the multi-provider agent loop (Claude / OpenAI / NIM).

Covers what the endpoint sweep can't: tool round-trips, multi-turn history
replay, switching provider mid-conversation, the truncation/continuation path,
and the shape of what actually lands in stored history.

Live calls cost money. Anthropic turns are skipped automatically when that
account has no credit, so a Claude billing problem doesn't masquerade as a
regression in this change.
"""
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import openai  # noqa: E402

from hermes import agent, models  # noqa: E402
from hermes.config import Config  # noqa: E402
from hermes.memory import store  # noqa: E402

NEMO = "nvidia/nemotron-3.5-lightning-30b-a3b"
OSS = "openai/gpt-oss-20b"
GEM = "google/gemma-4-31b-it"
OAI = "gpt-4o-mini"
CLAUDE = "claude-haiku-4-5-20251001"

WEATHER = [{
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}]

checks = []
timeouts = []


def _ascii(s):
    """Model output is full of typographic punctuation; this console is cp1252."""
    return str(s).encode("ascii", "replace").decode("ascii")


def check(name, ok, detail=""):
    checks.append((bool(ok), name, _ascii(detail)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {_ascii(detail)}" if detail else ""))


def run(model, tools, system, messages, max_tokens=4000, continuation=False):
    """Drive one provider call, returning (events_by_type, final_event).

    Returns (None, None) if the provider timed out. build.nvidia.com's free
    endpoints are shared capacity and do saturate; that is an infrastructure
    condition, not a defect in this code, so callers record it as TIMEOUT rather
    than letting one slow endpoint abort the whole suite.
    """
    provider = models.provider_of(model)
    if provider == "anthropic":
        gen = agent._anthropic_turn(model, tools, system, messages,
                                    None, max_tokens, continuation)
    else:
        gen = agent._openai_turn(model, tools, system, messages,
                                 max_tokens, continuation, provider)
    seen, final = {}, None
    try:
        for ev in gen:
            if ev["type"] == "_final":
                final = ev
            else:
                seen[ev["type"]] = seen.get(ev["type"], 0) + 1
    except openai.APITimeoutError:
        return None, None
    return seen, final


def skipped(model, *names):
    """Record checks we couldn't reach because the endpoint timed out."""
    for n in names:
        timeouts.append(f"{model}: {n}")
        print(f"  [TIME] {model}: {n} - endpoint timed out")


def claude_has_credit():
    try:
        agent._anthropic().messages.create(
            model=CLAUDE, max_tokens=4,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True
    except anthropic.BadRequestError as exc:
        if "credit balance" in str(exc):
            return False
        raise


# ── 1. Basic completion on every reachable provider ────────────────────────
print("\n1. Basic completion per provider")
CLAUDE_OK = claude_has_credit()
if not CLAUDE_OK:
    print("  (Anthropic account has no credit — Claude turns skipped, not failed)")

targets = [NEMO, OSS, GEM, OAI] + ([CLAUDE] if CLAUDE_OK else [])
for m in targets:
    try:
        seen, fin = run(m, [], "You are Hermes. Be terse.",
                        [{"role": "user", "content": [{"type": "text", "text": "Say OK"}]}])
        if fin is None:
            skipped(m, "returns text")
            continue
        text = agent._text_of(fin["content"])
        check(f"{m}: returns text", bool(text.strip()),
              f"reply={text[:30]!r} stop={fin['stop_reason']}")
    except Exception as exc:  # noqa: BLE001
        check(f"{m}: returns text", False, repr(exc)[:120])

# ── 2. No unsigned thinking block reaches shared history ───────────────────
print("\n2. Stored history stays Anthropic-safe")
for m in [NEMO, OSS, GEM]:
    _, fin = run(m, [], "You are Hermes.",
                 [{"role": "user", "content": [{"type": "text", "text": "Say OK"}]}])
    if fin is None:
        skipped(m, "no unsigned thinking block in content", "reasoning returned out of band")
        continue
    types = [b["type"] for b in fin["content"]]
    bad = [b for b in fin["content"] if b.get("type") == "thinking" and not b.get("signature")]
    check(f"{m}: no unsigned thinking block in content", not bad, f"blocks={types}")
    # All three NIM models here reason: Nemotron and Gemma once enable_thinking
    # is set, gpt-oss unconditionally. None may leak it into content (above).
    got = len(fin.get("thinking") or "")
    check(f"{m}: reasoning returned out of band", got > 0, f"{got} chars")

# ── 3. Tool round-trip: call -> result -> final answer ─────────────────────
print("\n3. Full tool round-trip")
for m in [NEMO, OSS, GEM]:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Weather in Karachi? Use the tool."}]}]
    _, fin = run(m, WEATHER, "You are Hermes. Use tools when asked.", msgs)
    if fin is None:
        skipped(m, "emits tool_use", "answers from tool result")
        continue
    calls = [b for b in fin["content"] if b["type"] == "tool_use"]
    check(f"{m}: emits tool_use", bool(calls) and fin["stop_reason"] == "tool_use",
          f"{[(c['name'], c['input']) for c in calls]}")
    if not calls:
        continue
    # Feed a tool result back and confirm the model finishes the turn.
    msgs.append({"role": "assistant", "content": fin["content"]})
    msgs.append({"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": calls[0]["id"],
        "content": '{"temp_c": 31, "conditions": "hazy sunshine"}',
    }]})
    _, fin2 = run(m, WEATHER, "You are Hermes. Use tools when asked.", msgs)
    if fin2 is None:
        skipped(m, "answers from tool result")
        continue
    answer = agent._text_of(fin2["content"])
    check(f"{m}: answers from tool result", "31" in answer or "haz" in answer.lower(),
          f"reply={answer[:70]!r}")

# ── 4. Multi-turn history replay ───────────────────────────────────────────
print("\n4. Multi-turn conversation")
for m in [NEMO, OSS, GEM]:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "My favourite colour is teal. Reply OK."}]}]
    _, f1 = run(m, [], "You are Hermes. Be terse.", msgs)
    if f1 is None:
        skipped(m, "recalls turn 1 across turns")
        continue
    msgs.append({"role": "assistant", "content": f1["content"]})
    msgs.append({"role": "user", "content": [{"type": "text", "text": "What is my favourite colour? One word."}]})
    _, f2 = run(m, [], "You are Hermes. Be terse.", msgs)
    if f2 is None:
        skipped(m, "recalls turn 1 across turns")
        continue
    recalled = agent._text_of(f2["content"])
    check(f"{m}: recalls turn 1 across turns", "teal" in recalled.lower(), f"reply={recalled[:40]!r}")

# ── 5. Switching provider mid-conversation ─────────────────────────────────
print("\n5. Provider switch mid-conversation (shared history format)")
chain = [NEMO, OSS, GEM, OAI] + ([CLAUDE] if CLAUDE_OK else [])
msgs = [{"role": "user", "content": [{"type": "text", "text": "Remember the number 4271. Reply OK."}]}]
_, f = run(chain[0], [], "You are Hermes. Be terse.", msgs)
if f is None:
    skipped(chain[0], "handoff seed turn")
    chain = []
else:
    msgs.append({"role": "assistant", "content": f["content"]})
for nxt in chain[1:]:
    msgs.append({"role": "user", "content": [{"type": "text", "text": "What number did I ask you to remember?"}]})
    try:
        _, f = run(nxt, [], "You are Hermes. Be terse.", msgs)
        if f is None:
            skipped(nxt, "handoff")
            continue
        out = agent._text_of(f["content"])
        check(f"handoff -> {nxt}", "4271" in out, f"reply={out[:45]!r}")
        msgs.append({"role": "assistant", "content": f["content"]})
    except Exception as exc:  # noqa: BLE001
        check(f"handoff -> {nxt}", False, repr(exc)[:130])

# ── 6. Truncation + continuation recovery ──────────────────────────────────
print("\n6. Truncation is reported, not silently swallowed")
for m in [NEMO, OSS, GEM]:
    _, fin = run(m, [], "You are Hermes.",
                 [{"role": "user", "content": [{"type": "text", "text":
                   "Write a detailed 600-word essay about the history of shipping containers."}]}],
                 max_tokens=600)
    if fin is None:
        skipped(m, "cut-off turn reports max_tokens")
        continue
    check(f"{m}: cut-off turn reports max_tokens", fin["stop_reason"] == "max_tokens",
          f"stop={fin['stop_reason']}")

# ── 7. Continuation call omits the reasoning toggle ────────────────────────
print("\n7. Continuation path")
for m in [NEMO, OSS, GEM]:
    seen, fin = run(m, [], "You are Hermes.",
                    [{"role": "user", "content": [{"type": "text", "text": "Say OK"}]},
                     {"role": "assistant", "content": [{"type": "text", "text": "O"}]}],
                    max_tokens=500, continuation=True)
    if fin is None:
        skipped(m, "continuation call succeeds")
        continue
    check(f"{m}: continuation call succeeds", fin is not None,
          f"thinking_events={seen.get('thinking', 0)} stop={fin['stop_reason'] if fin else '-'}")

# ── 8. Config / registry integrity ─────────────────────────────────────────
print("\n8. Registry and key wiring")
check("gpt-oss routes to nvidia", models.provider_of(OSS) == "nvidia")
check("gemma routes to nvidia", models.provider_of(GEM) == "nvidia")
check("nemotron routes to nvidia", models.provider_of(NEMO) == "nvidia")
check("bare gpt-* still routes to openai",
      models.provider_of("gpt-4o-mini") == "openai" and models.provider_of("gpt-5.4-mini") == "openai")
check("claude ids still route to anthropic", models.provider_of(CLAUDE) == "anthropic")
check("per-model keys all differ",
      len({Config.nvidia_key_for(x) for x in (OSS, NEMO, GEM)}) == 3)
check("unknown NIM model falls back to shared key",
      Config.nvidia_key_for("nvidia/does-not-exist") == Config.NVIDIA_API_KEY)
check("every option is priced",
      all(models._rates_for(o["id"]) for o in models.MODEL_OPTIONS),
      f"{len(models.MODEL_OPTIONS)} options")
check("distinct NIM clients per key",
      agent._nvidia(OSS) is not agent._nvidia(NEMO))
try:
    models.set_active_model("nope")
    check("unknown model rejected", False)
except ValueError:
    check("unknown model rejected", True)

# ── Report ─────────────────────────────────────────────────────────────────
failed = [c for c in checks if not c[0]]
print("\n" + "=" * 70)
for ok, name, detail in failed:
    print(f"FAIL  {name} — {detail}")
print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
if timeouts:
    print(f"{len(timeouts)} check(s) unreachable - NIM free endpoint timed out:")
    for t in timeouts:
        print(f"  - {t}")
if not CLAUDE_OK:
    print("NOTE: Anthropic turns skipped — that account is out of credit.")
sys.exit(1 if failed else 0)
