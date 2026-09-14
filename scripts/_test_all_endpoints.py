"""Sweep every HTTP route after the NVIDIA/Nemotron model addition.

Read-only GETs are called for real. Mutating routes are probed with an invalid
body so routing + validation are exercised without creating or destroying data;
DELETEs use a bogus UUID so they must 404 rather than remove anything real.

Deliberately NOT called (side effects the caller must opt into):
  POST /api/admin/migrate          applies pending SQL migrations to the live DB
  POST /api/bills/scan             reaches into Gmail/Maton
  POST /api/bills/check-payments   same
  POST /api/voice/speak|transcribe billable audio calls needing real media
"""
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as A

client = TestClient(A.app)
BOGUS = str(uuid.uuid4())
SESSION = f"endpoint-sweep-{uuid.uuid4().hex[:8]}"

results = []


def call(method, path, *, expect, label="", **kw):
    try:
        r = client.request(method, path, **kw)
        code = r.status_code
    except Exception as exc:  # noqa: BLE001 — a raised exception IS the failure
        results.append(("ERROR", method, path, repr(exc)[:110], label))
        return None
    ok = code in expect
    results.append(("PASS" if ok else "FAIL", method, path, code, label))
    return r


# ── Read-only GETs ──────────────────────────────────────────────────────────
OK = {200}
call("GET", "/", expect=OK, label="UI shell")
call("GET", "/api/health", expect=OK)
call("GET", "/api/settings/model", expect=OK, label="model picker")
call("GET", "/api/settings/guardrails", expect=OK)
call("GET", "/api/businesses", expect=OK)
call("GET", "/api/connections", expect=OK)
call("GET", "/api/conversations", expect=OK)
# entity_id is a required query param on the entity-scoped reads below.
_biz = client.get("/api/businesses").json()
_ents = _biz.get("businesses") if isinstance(_biz, dict) else _biz
ENTITY = (_ents[0].get("entity_id") or _ents[0].get("slug") or _ents[0].get("id")) if _ents else "all"
print(f"entity under test: {ENTITY}")
call("GET", "/api/documents", params={"entity_id": ENTITY}, expect=OK)
call("GET", "/api/bills", expect=OK)
call("GET", "/api/bills/insights", expect=OK)
call("GET", "/api/files/tree", params={"entity_id": ENTITY}, expect=OK)
call("GET", "/api/memories", expect=OK)
call("GET", "/api/usage", expect=OK, label="cost table")
call("GET", "/api/search", params={"q": "invoice"}, expect=OK | {422})
call("GET", "/api/media/presign", params={"key": "x.png"}, expect=OK | {400, 404, 422})
call("GET", "/api/meetings/sessions", expect=OK)
call("GET", "/api/meetings/upcoming", params={"business": ENTITY}, expect=OK | {400, 502})
call("GET", "/api/scheduled-tasks", expect=OK)
call("GET", "/api/voice/voices", expect=OK)
call("GET", "/openapi.json", expect=OK)
call("GET", "/docs", expect=OK)
call("GET", "/redoc", expect=OK)
call("GET", "/docs/oauth2-redirect", expect=OK)

# ── GETs on ids that don't exist: must 404, not 500 ─────────────────────────
NF = {400, 404, 422}
call("GET", f"/api/conversations/{BOGUS}", expect=OK | NF, label="empty session")
call("GET", f"/api/documents/{BOGUS}", expect=NF)
call("GET", f"/api/attachments/{BOGUS}", expect=NF)
call("GET", f"/api/bills/{BOGUS}/view", expect=NF)
call("GET", f"/api/bills/{BOGUS}/email-body", expect=NF)
call("GET", f"/api/meetings/sessions/{BOGUS}", expect=NF)
call("GET", f"/api/scheduled-tasks/{BOGUS}", expect=NF)
call("GET", f"/api/scheduled-tasks/{BOGUS}/runs", expect=OK | NF)

# ── DELETEs on bogus ids: route + guard, nothing real removed ───────────────
call("DELETE", f"/api/documents/{BOGUS}", expect=NF)
call("DELETE", f"/api/attachments/{BOGUS}", expect=NF)
call("DELETE", f"/api/bills/{BOGUS}", expect=NF)
call("DELETE", f"/api/memories/{BOGUS}", expect=NF)
call("DELETE", f"/api/meetings/sessions/{BOGUS}", expect=NF)
call("DELETE", f"/api/meetings/sessions/{BOGUS}/turns", expect=OK | NF)
call("DELETE", f"/api/scheduled-tasks/{BOGUS}", expect=NF)
call("DELETE", f"/api/conversations/{BOGUS}", expect=OK | NF, label="no-op delete")

# ── Mutating POSTs: invalid body -> validation, no data written ─────────────
V = {400, 404, 422}
call("POST", "/api/businesses", json={}, expect=V)
call("POST", "/api/documents", json={}, expect=V)
call("POST", "/api/connections", json={}, expect=V)
call("POST", "/api/attachments", json={}, expect=V)
call("POST", "/api/bills/upload", json={}, expect=V)
call("PATCH", f"/api/bills/{BOGUS}", json={}, expect=V)
call("PATCH", f"/api/documents/{BOGUS}", json={}, expect=V)
call("POST", "/api/scheduled-tasks", json={}, expect=V)
call("PATCH", f"/api/scheduled-tasks/{BOGUS}", json={}, expect=V)
call("POST", f"/api/scheduled-tasks/{BOGUS}/run", json={}, expect=V)
call("POST", "/api/meetings/prepare", json={}, expect=V)
call("POST", "/api/meetings/copilot", json={}, expect=V)
call("POST", f"/api/meetings/sessions/{BOGUS}/ai-notes", json={}, expect=V)
call("POST", f"/api/meetings/sessions/{BOGUS}/end", json={}, expect=V)
call("POST", f"/api/meetings/sessions/{BOGUS}/fathom", json={}, expect=V)
call("POST", "/api/voice/speak", json={}, expect=V)
call("POST", "/api/voice/transcribe", json={}, expect=V)
call("POST", "/api/settings/guardrails", json={}, expect=V)
call("GET", "/api/scheduled-tasks/run-due", expect=OK | {401, 403}, label="cron, needs secret")

# ── The feature under test: switch across all three providers ──────────────
print("\n--- model switching ---")
for mid in [
    "claude-haiku-4-5-20251001",
    "gpt-4o-mini",
    "openai/gpt-oss-20b",
    "google/gemma-4-31b-it",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
]:
    r = call("POST", "/api/settings/model", json={"model": mid}, expect=OK, label=mid)
    active = (r.json() or {}).get("active") if r is not None else None
    print(f"  set {mid:42} -> active={active}  {'OK' if active == mid else 'MISMATCH'}")
r = call("POST", "/api/settings/model", json={"model": "does-not-exist"}, expect={400},
         label="rejects unknown")
print(f"  unknown id -> {r.status_code} {r.json() if r is not None else ''}")

opts = client.get("/api/settings/model").json()
print(f"  picker exposes {len(opts['options'])} options; active={opts['active']}")

# ── Real agent turns on each NIM model (the whole point) ───────────────────
for mid in ["nvidia/nemotron-3.5-lightning-30b-a3b", "openai/gpt-oss-20b", "google/gemma-4-31b-it"]:
    print(f"\n--- live agent turn on {mid} ---")
    client.post("/api/settings/model", json={"model": mid})
    sess = f"{SESSION}-{uuid.uuid4().hex[:6]}"
    r = call("POST", "/api/chat",
             json={"session_id": sess, "message": "Reply with exactly: PONG"},
             expect=OK, label=f"chat on {mid}")
    if r is not None and r.status_code == 200:
        d = r.json()
        reply = str(d.get("reply") or "")
        empty = (not reply.strip()) or reply == "(no response)"
        print(f"  reply     : {reply[:60]!r}  {'<<< EMPTY' if empty else ''}")
        print(f"  model_used: {d.get('model_used')}")
        print(f"  stop/cost : {d.get('stop_reason')}  ${d.get('cost_usd')}")
        print(f"  usage     : in={d['usage']['input_tokens']} out={d['usage']['output_tokens']}")
        print(f"  thinking  : {str(d.get('thinking') or '(none)')[:60]!r}")
        if empty:
            results.append(("FAIL", "POST", "/api/chat", "empty reply", mid))

    r = call("POST", "/api/chat/stream",
             json={"session_id": sess + "-s", "message": "Name one colour. One word."},
             expect=OK, label=f"stream on {mid}")
    if r is not None and r.status_code == 200:
        kinds = {}
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    t = json.loads(line[5:]).get("type")
                except ValueError:
                    continue
                kinds[t] = kinds.get(t, 0) + 1
        print("  SSE events:", kinds)

call("POST", "/api/reset", json={"session_id": SESSION}, expect=OK, label="fresh session only")

# ── Report ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
bad = [r for r in results if r[0] != "PASS"]
for status, method, path, code, label in results:
    if status != "PASS":
        print(f"{status:5} {method:6} {path:45} {code} {label}")
print(f"{len(results) - len(bad)}/{len(results)} routes OK, {len(bad)} failed")
print("skipped (side effects): /api/admin/migrate, /api/bills/scan, /api/bills/check-payments")
sys.exit(1 if bad else 0)
