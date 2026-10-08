# Hermes — Day-1 Task Prompts (2-3 hour batch)

Ready-to-paste prompts for Claude Code. Each block is **self-contained**: open a fresh session in `D:\Gandi AI Assistant`, paste one block, let it run. Do them in the order below — they're independent, so any single one can ship alone.

Line references verified against the code snapshot on 2026-07-24. Each prompt still tells Claude to re-verify first.

| # | Task | Type | Files | Est. |
|---|------|------|-------|------|
| 1 | A1 — Unthrottle config | env + 0 code | `.env`, `.env.example`, Vercel | 15 min |
| 2 | B4 — Raise memory-injection ceiling | code | `memory_retrieve.py`, `config.py`, `.env.example` | 30 min |
| 3 | A3 — Graceful wrap-up when tool budget runs out | code | `agent.py` | 45 min |
| 4 | E4 — Tool-result size guard | code | `tools.py`, `config.py` | 30 min |
| 5 | G2 — Secrets hygiene sweep | ops/audit | none (checklist) | 20 min |

Total: ~2h20m plus test time. Skip any you'd rather do by hand.

---

## Prompt 1 — A1: Unthrottle effort / tokens / tool iterations

```
Task A1 from HERMES_UPGRADE_PLAN.md. Do NOT change HERMES_MODEL.

Verify first: hermes/config.py lines 71-74 currently read:
  MODEL = "claude-sonnet-5"
  EFFORT = os.getenv("HERMES_EFFORT", "low")
  MAX_TOKENS = int(os.getenv("HERMES_MAX_TOKENS", "8000"))
  MAX_TOOL_ITERATIONS = int(os.getenv("HERMES_MAX_TOOL_ITERATIONS", "12"))
Confirm those defaults still match before editing.

Make these changes:
1. In .env (local): set / add
     HERMES_EFFORT=medium
     HERMES_MAX_TOKENS=16000
     HERMES_MAX_TOOL_ITERATIONS=32
2. In .env.example: add the same three keys (with a short comment) so the change is documented, keeping the file's existing style.
3. Do NOT edit hermes/config.py defaults in this task — the env values are the source of truth; the code defaults stay conservative.

Then tell me exactly which three env vars I must also set in the Vercel project dashboard (Vercel env changes need a redeploy — you cannot do that part).

Definition of done: .env and .env.example carry the three keys; you print the Vercel checklist for me.

Do NOT commit or push. Show me the diff.
```

**Manual step after (yours):** set the same three vars in Vercel → redeploy. Then hit `GET /api/health` and run the multi-app test from the plan (A1 "Test").

---

## Prompt 2 — B4: Raise the memory-injection ceiling

```
Task B4 from HERMES_UPGRADE_PLAN.md.

Verify first:
- hermes/memory_retrieve.py line 27: `_MAX_BLOCK_CHARS = 2000`
- hermes/memory_retrieve.py lines 118-119: truncates with `block[:_MAX_BLOCK_CHARS].rstrip() + "\n  ...(truncated)"`
- hermes/config.py line 237: `MEMORY_SEMANTIC_TOP_K = int(os.getenv("MEMORY_SEMANTIC_TOP_K", "5"))`
Confirm these before editing.

Make these changes:
1. hermes/config.py: add a config value
     MEMORY_BLOCK_MAX_CHARS = int(os.getenv("MEMORY_BLOCK_MAX_CHARS", "8000"))
   near the other MEMORY_* settings.
2. hermes/config.py: raise the MEMORY_SEMANTIC_TOP_K default from "5" to "8".
3. hermes/memory_retrieve.py: delete the module constant `_MAX_BLOCK_CHARS = 2000` and read `Config.MEMORY_BLOCK_MAX_CHARS` at the truncation site instead (import Config is already present).
4. hermes/memory_retrieve.py: change the truncation so it cuts on a LINE boundary, not mid-fact. Instead of `block[:cap]`, take `block[:cap]`, then trim back to the last "\n" within it (if one exists) before appending the "...(truncated)" marker. Keep the marker text.
5. .env.example: document MEMORY_BLOCK_MAX_CHARS (default 8000) and MEMORY_SEMANTIC_TOP_K (default 8), matching the file's existing comment style.

Definition of done: a memory block over 2000 chars renders whole; if truncation ever fires it ends on a complete line. No other behavior changes.

Do NOT commit or push. Show me the diff and confirm nothing else imports `_MAX_BLOCK_CHARS`.
```

---

## Prompt 3 — A3: Graceful wrap-up when the tool budget runs out

```
Task A3 from HERMES_UPGRADE_PLAN.md. This is in hermes/agent.py, inside stream_turn().

Verify first:
- Line 458: `for _ in range(Config.MAX_TOOL_ITERATIONS):`
- Lines 479-529: when stop_reason == "tool_use" and tool_results exist, it appends them and `continue`s.
- Lines 546-547: normal end path sets `reply = _text_of(assistant_content); break`.
- Lines 551-559: emits the "done" event with `"reply": reply or "(no response)"`.
The failure mode: if the loop exhausts all iterations while the model still wants tools, execution falls out of the `for` with reply == "" and stop_reason == "tool_use", so the user gets "(no response)" — even for a scheduled run that did real work.

Make this change:
After the `for` loop ends (before `store.set(...)` at line 549), detect the exhausted case: `stop_reason == "tool_use"` AND not reply. When that's true:
1. Append one more user message to `messages`:
     "[system] Tool budget for this turn is exhausted. Do not call more tools. Write your best final answer from what you already gathered, and list explicitly what you could not verify."
2. Make ONE more model call using the SAME provider path used in the loop (_anthropic_turn / _openai_turn), but force no tools:
   - For anthropic: pass tool_choice={"type": "none"}. KEEP the `tools` list in the call — the message history contains tool_use blocks, so tool definitions must still be present or the API rejects it.
   - Match however the existing loop invokes the turn; reuse system_prompt from the same _system_prompt(...) call.
   You may need to thread tool_choice into _anthropic_turn as an optional param defaulting to None (no change for existing callers). Do the minimal, consistent thing — read how _anthropic_turn builds its request (around lines 293-333) first.
3. Collect that call's text into `reply`, append its assistant_content to `messages`, and capture any thinking summary the same way the loop does.
4. The existing `store.set` + "done" event then persist and return it normally.

Guardrails:
- Do not break the normal end_turn / refusal / pause_turn paths.
- If the extra call itself errors, fall back to a plain string reply summarizing that the turn hit its tool limit — never re-raise into the user's turn.

Definition of done: with iterations forced low, a multi-app question yields an honest partial-results answer that names what's missing — never "(no response)".

Test: temporarily set HERMES_MAX_TOOL_ITERATIONS=2, ask a 3-app question, confirm a real partial answer, then restore.

Do NOT commit or push. Show me the diff.
```

---

## Prompt 4 — E4: Tool-result size guard

```
Task E4 from HERMES_UPGRADE_PLAN.md.

Verify first: hermes/tools.py dispatch() is defined at line 2011 and its main success return is line 2168:
    return (json.dumps(result, default=str), is_error, media, blocks if not is_error else None)
Early error returns (lines 2159, 2161, 2163) return small JSON error strings. There is currently NO truncation of large tool results anywhere — confirm this by scanning dispatch() and its caller in hermes/agent.py (line 489, `content_str, is_error, media, blocks = dispatch(...)`).

Make these changes:
1. hermes/config.py: add
     TOOL_RESULT_MAX_CHARS = int(os.getenv("TOOL_RESULT_MAX_CHARS", "20000"))
2. hermes/tools.py: add a small helper that, given content_str and is_error, returns a bounded string:
   - If is_error: only truncate when it's very large; never trim an error payload below ~2000 chars (the error text is what lets the model self-correct).
   - Otherwise, if len(content_str) > TOOL_RESULT_MAX_CHARS: keep head (first 60% of the cap) + tail (last 20% of the cap), joined by:
       "\n...[truncated N chars — refine the request or ask for a narrower range]...\n"
     where N is the number of chars removed.
   - When truncation fires, log (logging.warning) the tool name + original size. That log is the shopping list for future typed wrappers.
3. Apply the helper on the SUCCESS return path (line 2168) so `content_str` is bounded before it leaves dispatch(). Make sure `name` is in scope for the log message (it is — it's the first arg).
4. Leave media/blocks untouched.

Definition of done: a deliberately huge maton_request result comes back bounded; the turn stays coherent; error payloads under ~2000 chars are never trimmed.

Do NOT commit or push. Show me the diff.
```

---

## Prompt 5 — G2: Secrets hygiene sweep (audit only)

```
Task G2 from HERMES_UPGRADE_PLAN.md. This is an AUDIT — do not rotate or change any secret yourself; produce a checklist with findings so I can act.

Run and report on each of these:
1. Confirm .env is git-ignored: check .gitignore contains .env, and run:
     git log --all --oneline -- .env
     git log --all -p -S "sk-ant" -- . | head
   Report whether .env or any Anthropic-style key ever appears in history.
2. Note: there is a nested git repo at ingestion-service/.git — run the same two checks inside it and report separately.
3. Grep the working tree (NOT just staged) for accidentally-committed secret patterns: "sk-ant", "sk-", "AKIA" (AWS), "AIza" (Google), and any *_API_KEY assigned a literal value in tracked files. Report file + line for any hit; do NOT print the secret value — mask it.
4. Check whether CRON_SECRET / SCHEDULER_SECRET are referenced by the run-due endpoints in app.py and hermes/config.py (lines ~160-164), and report whether they are REQUIRED (endpoint 401s without them) or optional. Flag if the endpoints are open when the secret is unset.
5. Produce a final table: item | status (OK / NEEDS ROTATION / NEEDS CONFIG / N-A) | note. Cover: Anthropic, OpenAI, the 6 Maton business keys, AWS, NuExtract, SerpAPI, Groq, CRON/SCHEDULER secret.

Do NOT modify any file. Output only the report.

Note for me (not you): the RDS security group (no 0.0.0.0/0 on 5432), S3 public-access block, and sslmode=require live in AWS/Vercel dashboards — list them as manual items I must verify there.
```

---

## Order & what's yours vs. Claude's

- **Prompts 2, 3, 4** are pure code — Claude Code does them end to end; you review the diff and run the test.
- **Prompt 1** — Claude edits `.env`/`.env.example`; **you** set the Vercel vars + redeploy.
- **Prompt 5** — Claude audits and reports; **you** do any rotation and the AWS/Vercel dashboard checks.
- Nothing here commits or pushes — review each diff, then commit the batch yourself.

Not in this batch (need more than 2-3h or ops-heavy): **G1** (auth layer — do the Vercel Deployment-Protection stopgap now, build the token middleware this week), **B1→B3** (context packs, the biggest win but ~half a day), **C2** (PDF extraction). See the main plan.
