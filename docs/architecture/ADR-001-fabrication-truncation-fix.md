# ADR-001: Fixing fabrication + truncation-blind reporting

**Status:** Proposed
**Date:** 2026-07-27
**Branch:** `truncation-issue-fix`
**Source:** Hermes Failure Diagnostics Report, 25 Jul 2026 (R1–R8)

---

## TL;DR

- **What broke:** the agent asserted a payroll figure it never retrieved (~KES 900k/month), and described email content that lay past a silent 20,000-character cut.
- **One root cause:** nothing in the pipeline knows *where a fact came from*. A tool result is a JSON blob concatenated into context; once the model has written a sentence, no code can tell whether the number in it was retrieved, derived, remembered, or invented.
- **The decisive discovery:** the anti-fabrication rules the report asks for **already exist as prompt text** — and failed anyway. See below.
- **The fix:** make provenance a *data structure*, not an instruction. Record what actually entered context at the one dispatch choke point; check the finished reply against that record at the one reply exit point.
- **Start with:** a ~60-line deterministic money-claim check that appends an honest footer. Catches the KES 900k fabrication, ships in an afternoon, cannot damage a correct reply.

---

## The finding that changes the diagnosis

`HERMES_CONTEXT_PACKS.md`, "Hard rules (non-negotiable)":

> **Rule 2.** RENT AND MONEY FIGURES COME FROM LIVE QUICKBOOKS ONLY. Never state a rent amount, arrears figure, or balance from a document, this pack, memory, or an old file. **If QuickBooks is unreachable, say so — do not estimate.**
>
> **Rule 4.** NEVER INVENT tenants, dates, amounts, vendors, or people. If a fact is not in this pack, the business pack, retrieved documents, or a live tool result, say "no indexed source found" and mark the point UNVERIFIED.

That is R3 and R5, already written, in stronger language than the remediation request uses. QuickBooks was PENDING. The agent estimated anyway.

### ⚠️ RESOLVED 2026-07-27 — the rules were never loaded

This section originally posed pack-loading as an open question and concluded "prompt instruction is not an available enforcement channel." **That question has now been answered against the production database, and the answer inverts the conclusion.**

```
select table_name from information_schema.tables
 where table_name in ('context_packs','playbooks','task_playbooks');   →  []
```

Migration `0017_context_packs_playbooks.sql` was **never applied** — the tables do not exist. `packs_for()` swallows the `relation does not exist` error into a `log.warning` and returns `""` (`hermes/context_packs.py:78-80`), so **every production turn runs with an empty BUSINESS CONTEXT PACK block.** The seven authored packs — including the hard rules above — have never been in the system prompt.

| Original claim | Corrected |
|---|---|
| The rules were in the prompt and were ignored. | The rules were **never in the prompt**. The agent had no standing anti-fabrication rule at all. |
| Prompt instruction is not an available enforcement channel. | **Unproven.** Prompt-level control has never actually run in production. |
| Stage 0 is the money-claim footer. | **Stage 0 is now: load the packs, and make a missing pack loud.** ~1 hour, and it may prevent a recurrence outright. |

**What this does not change:** the code controls below are still worth building. A silent failure that empties the entire rule block, with nothing anywhere reporting it, is itself the argument for enforcement that doesn't depend on a prompt arriving intact. But the honest sequencing is now *load the packs, measure, then build the gate* — not "prose already failed, skip to code."

The two verified code bugs (invalid-JSON truncation, dropped locators) are unaffected and worth fixing regardless.

### Stage 0.0 — do this before anything else (~1 hour)

1. Apply migration 0017; run `load_packs.py` and `load_playbooks.py` against production.
2. Make the silent path loud — `packs_for()` must not treat a *missing table* the same as a legitimately empty pack. Log at ERROR and assert at startup that the `global` pack exists.
3. Re-run the 25 Jul scenario. If the agent now refuses to supply the payroll benchmark, you have **measured** that prompt-level control works, and Stages 0.3+ become defence in depth rather than the primary control.

---

## Verified facts (read from the working tree)

| Fact | Evidence |
|---|---|
| Every Maton response is capped at 20k chars by serialise-then-slice; the surviving `preview` is **invalid mid-object JSON**, and the payload's *type* is destroyed (a truncated list becomes a dict) | `hermes/maton.py:21`, `:217-226` |
| `_truncate` runs on **every** response via `_parse_body`, no opt-out | `hermes/maton.py:209-214` |
| **Nothing reads `_truncated`.** Three writers, zero readers | `hermes/maton.py:223`, `hermes/tools.py:1806`, `ingestion-service/ingestion/maton.py:104` |
| `dispatch()` is the single choke point for local tools — one definition, one caller | `hermes/tools.py:2011`, `hermes/agent.py:659` |
| `dispatch` has **four** returns, not one | `hermes/tools.py:2159, 2161, 2163, 2168` |
| The `tool_use` id is in scope but dropped from the trace | `hermes/agent.py:660-665` vs `:687` |
| The UI **replaces** streamed text with `final.reply` on the `done` event — so a server-side gate can rewrite the answer post-stream | `static/app.js:513-514` |
| The complete reply string first exists at one line | `hermes/agent.py:718` |
| Connector status is inert metadata — a filter arg on one tool schema, read by no code | `hermes/tools.py:104-108` |
| `search.py` strips the `chunk_id` that `vector_store.py` already returns; `_search_business_knowledge` strips `document_id` | `hermes/search.py:57-66`, `hermes/vector_store.py:199-208`, `hermes/tools.py:1253-1264` |
| One tool of ~21 returns a result count | `hermes/tools.py:1906` |
| `pytest` is **not** in `requirements.txt` | `requirements.txt` |

---

## Decision

Build a **deterministic, in-process evidence check**, scoped narrowly to the claim classes that actually failed, shipped in observe-mode first, with **no LLM in the verification path** for v1.

The gate **annotates; it does not delete.** It appends a footer listing figures it could not locate in the turn's retrieved data. Non-destructive, markdown-safe, TTS-safe — and crucially, it does not get switched off in week one, which every critique flagged as the most likely failure mode.

### Options considered

| | A: Deterministic ledger + gate | B: LLM adversarial second pass | C: Provenance table + resolver |
|---|---|---|---|
| Complexity | Medium-high (pure Python) | High (nonce protocol, 3-stage) | High (new table, migration) |
| Cost | Zero marginal API | +1–2 model calls per brief | Zero API, new PII surface |
| Scalability | Good — no network in hot path | Poor — serial call per brief | Medium |
| Team familiarity | High — no new deps | Medium | Medium |
| Honest effort | ~25–30d | ~20–25d | ~25–40d |

**Chosen: A as the spine**, with the best pieces of B and C grafted on.

**Taken from B:** the *inverted burden of proof*, reduced to code — a value is SOURCED only if it literally occurs in a recorded tool-result body. The substring test **is** the check; no verifier needed. Plus observe-first discipline as a hard gate before ever enabling strike.

**Taken from C:** structure-preserving truncation; locator passthrough (`chunk_id`, `document_id`, `result_count`, `match_scope`); and the framing that content past a cut mints *zero* evidence records — so claims about it are unresolvable by construction rather than by the model noticing a marker.

**Dropped:**

| Dropped | Why |
|---|---|
| A's "derivation checker" (does 900k fall out of arithmetic on retrieved numbers?) | Simulated at pool=40: **200/200 trials found a spurious "derivation" of 900,000.** It would have printed the fabricated figure *and told the operator it was verified.* Manufactures false confidence — strictly worse than silence. |
| A's noise-budget circuit breaker | Disables the gate above ~8 annotations, i.e. exactly on dense financial briefs — the incident's own document class. |
| B's LLM verifier pass entirely | Serial latency and cost on every brief; `hermes/llm_json.py:39-44` degrades silently to `{fallback_key: raw_text}` so "fail closed on unparseable JSON" has nothing to branch on; blind to Drive content (base64 blocks) and web search. Revisit only with observe data. |
| C's strict `date` / `proper_noun` kinds | Today's date comes from the *prompt* (`hermes/agent.py:132`) and app names from `Config.app_catalog()` — a guaranteed false-positive machine. |
| C's new `evidence_records` table | Not needed for v1. `hermes_messages.metadata` jsonb already exists (`0003_message_metadata.sql`). |
| Inline striking of text | Breaks markdown tables/fences, reads as nonsense through TTS, and flickers on the streaming path. Footer gets 90% of the value with none of the damage. |

### Consequences

**Positive:** two real bugs fixed regardless of the fabrication story (invalid-JSON truncation, dropped locators); the 25 Jul money fabrication is caught in Stage 0; every stage has a deterministic test; no new API cost; no new table.

**Accepted negatives:**
- Mis-attribution (a *real* number under a wrong label) stays undetectable. No design in the set catches it.
- Paraphrased prose about content past a cut stays undetectable. Stage 1 makes the boundary honest, not the paraphrase checkable.
- Write-class tools fire *inside* the loop (`hermes/tools.py:2092-2093`), before any reply-level gate. A full-autonomy scheduled brief can send a bad email before the gate runs. **Open risk — not covered by this plan.**
- Ungated text is briefly visible on the typed-chat path (deltas leave at `:443`, complete string only exists at `:718`).

---

## THE PLAN — small steps

Each stage is independently shippable. Ordered by (prevents-a-real-failure ÷ effort).

### Stage 0 — Ship Monday (~6 hours)

**0.1 · Make the test suite runnable · 30 min**
Add `pytest>=8` to `requirements.txt`. Move the two untracked visual test files out of `tests/` — they fail against the working tree and will bury every new signal.
✅ `python -m pytest tests/ -q` is green.

**0.2 · Stop dropping the tool-call id · 15 min · R1**
`hermes/agent.py:660` — add `"id": block["id"]` to `trace_entry`. It's already in scope, used 20 lines later at `:687`, and every later source pointer resolves against it.
✅ `tool_calls[0].id` appears in the `done` SSE frame.

**0.3 · Money-claim footer — the one that catches 900k · 3 h · R3, R5**
New file `hermes/factcheck.py`. Collect each `content_str` into a per-turn list beside `tool_trace`; at `hermes/agent.py:718`, after `reply = _text_of(...)`:

```python
if Config.fact_footer():
    missing = unsourced_money(reply, tool_bodies)
    if missing:
        reply += ("\n\n---\n**Not found in this turn's retrieved data:** "
                  + ", ".join(missing[:6])
                  + " — I did not read these from a tool result. Treat as unverified.")
```

`unsourced_money` regex-extracts currency figures from the reply, normalises them (`900k` → `Decimal(900000)`) so reformatting can't dodge it, and set-matches against every number appearing in any tool body this turn.

*Why it catches 25 Jul:* QuickBooks was PENDING, no payroll figure was ever retrieved, so `900000` occurs in no body.
*Why it can't backfire:* appended footer, never inline mutation. Worst case is a noisy footer, not a damaged reply.
*Known limit:* a fabricated figure that collides with a real number in a payload is missed. Collisions cause **misses, not false positives** — the failure direction is safe.

**0.4 · Three prompt rules · 30 min · R5, R6, R7**
`hermes/agent.py:131`, after "Lead with the answer": every figure must come from a tool result *this turn* (benchmarks held to the same standard); counts come from a count field or array length, never by eye; reproduce proper nouns byte-for-byte, never normalise.
*Not the enforcement* — see the decisive finding above. It's 30 minutes, reduces footer noise, and makes the code gate's verdict legible to the model.

**0.5 · Config flags done right · 45 min**
Add as `@classmethod` readers (following `Config.apps()` at `hermes/config.py:215-223`), **not** class attributes. Class attributes freeze at import, and `load_dotenv(override=True)` at `config.py:10` makes a developer's gitignored `.env` beat anything a test sets — a test would silently pass for the wrong reason.

---

### Stage 1 — Make truncation structural (~2 days)

**1.1 · Structure-preserving `_truncate` · 4 h · R2**
`hermes/maton.py:217-226` — replace serialise-then-slice with a recursive walk that keeps container types, truncates *leaf strings* (splicing `<<<TRUNCATED n of m chars>>>`), caps list length, and attaches a sibling `_truncation` report naming every cut path. Keep the sub-cap fast path byte-identical.

*This is a plain bug fix independent of the fabrication story:* today a truncated list becomes a dict, so `hermes/tools.py:1645` `resources[0]["id"]` would raise and `hermes/bills.py:391` silently returns `{}`.
⚠️ Capping list length drops MIME parts from `_flatten_parts` (`hermes/bills.py:426-434`) — cap lists generously (≥50) or exempt `payload.parts`.

**1.2 · Hoist the marker + inject `result_count` at dispatch · 2 h · R2, R6**
`hermes/tools.py:2168` — stamp the result so `_truncation` lands at the *top* of the serialised dict. Only `:2168` needs it; the other three returns are already error strings. ⚠️ Not every tool returns `{status, ok, data}` — `_search_business_knowledge` and `list_scheduled_tasks` return flat dicts.

**1.3 · Document the contract in the tool description · 30 min · R2**
`hermes/tools.py:355-443` — the `maton_request` description is ~90 lines and mentions size limits **zero** times. Add one paragraph: responses cap at 20k chars; a `_truncation` key means you have not seen the whole payload and must not describe anything past it; re-request with the API's paging parameter or narrow the query.

**1.4 · Escalate on truncated calls · 2 h · R2, R3**
If any tool body this turn carries `_truncation`, change the footer wording to name the boundary and unread byte count.
**Honest limit:** this makes a *figure* claimed past a cut loud. It does **not** catch the questions-26/27 paraphrase — that text contains no currency, no quote, no proper noun. Do not claim R2 is closed.

---

### Stage 2 — Counts and locators (~1.5 days)

**2.1 · `result_count` on the list tools · 1 h · R6** — `tools.py:1219` (calendar), `:1632` (carry Zoom's own `page_count`/`total_records`/`next_page_token` instead of discarding them), `:1253-1264`.

**2.2 · Stop stripping locators · 1 h · R1, R8** — `search.py:57-66` add `chunk_id` (already returned by `vector_store.py:204`, silently dropped); `tools.py:1256-1263` re-add `document_id` + `chunk_id`, add `match_scope` and `snippet_truncated`.
*Why:* the model currently receives title / source / snippet / score — **no identifier it could ever cite**. And the snippet is a 500-char cut of a stored 2,000-char preview: a fragment of a fragment, undetectable today.

**2.3 · Count-contradiction check · 2 h · R6**
**Honest limit:** the 7-vs-6 bounce case was a *filtered subset*; no array length equals 7. This catches count-vs-whole-list mismatches only.

---

### Stage 3 — Promote the footer to a real ledger (~3 days)

**3.1 · `TurnLedger` with origin tags · 1 day · R1** — new `hermes/provenance.py`, created beside `tool_trace` at `agent.py:590`. Ingests tool results (`origin=tool-response`, `call_id`, business slug), the user turn (`:567`), memory (`:572`), pack (`:580`). Indexed by `(value, business, origin)` — keying by business prevents a GAK figure silently satisfying an AFK claim.

**3.2 · Move the money check onto the ledger · 4 h · R1, R3, R5** — footer entries gain resolvable pointers (`tool=maton_request call_id=toolu_01ABC`).

**3.3 · Persist the verdict; block fabrication laundering · 4 h · R1, R3**
Add `provenance` to `meta` in `hermes/db/conversations.py:249-253` (**no migration needed**). Skip `memory_type="fact"` extraction when a turn had unsourced figures, so a fabricated number cannot become a durable `memories` row and get re-injected next turn as fact.
⚠️ `record_turn` wraps writes in a bare `except Exception` (`:296`) — put the evidence write in its own try or a failure silently discards the whole turn's history.

**3.4 · Surface it in the UI · 3 h** — `static/app.js:513` already re-renders from `final.reply`, so the footer lands for free. Add a badge + `<details>` popover reusing the tool-trace idiom.
⚠️ `app.py:538-543` spreads `**result` **last**, so a result key named `type`/`session_id`/`entity_id` overrides the envelope and breaks the client protocol. Namespace new keys.

---

### Stage 4 — Connector gating (~1 day, **gated on a spike**)

**4.0 · SPIKE FIRST · 30 min** — capture one real Maton `GET /connections` body and one real `messages.get?format=full` body. Write the shapes down. **Do not write a line of Stage 4 before this.** The `ACTIVE|PENDING|FAILED` enum at `tools.py:106` is Hermes' own declaration of a *filter argument*, not a description of the response.

**4.1 · Status oracle + precondition · 6 h · R4** — new `hermes/connectors.py` with a 60s TTL cache; hook into `_resolve_client` (`tools.py:1178`) and raise `MatonError`, which `dispatch` already converts to a tool error at `:2160`.
⚠️ Covers the Maton-backed half only (not calendar, knowledge search, documents, worldbank). Uncached lookups cost up to 12 extra HTTP calls per turn; on serverless the in-process cache may not survive.

---

### Stage 5 — Only after two weeks of observe data

Read the footer logs, measure the false-positive rate, *then* decide about inline striking or an LLM verifier. There is no logging on tool results today (`hermes/agent.py` has no logging import), so **Step 3.3's persistence is the telemetry.**

---

## Acceptance test

House convention (from `tests/test_scheduled_window.py`): `sys.path.insert` at top, `HERMES_DISABLE_DB=1` set **before** imports, **no pytest fixtures** (they break the `python tests/x.py` dual-mode runner), a `check(label, cond)` counter, `__main__` block collecting `test_*` from `globals()`.

New file `tests/test_fact_gate.py`:

| # | Scenario | Kind |
|---|---|---|
| a1 | Cash figure present, **no** payroll result → `"KES 900k"` flagged | Deterministic |
| a2 | `900k` / `900,000` / `~900000` all normalise to the same `Decimal` | Deterministic |
| a3 | An invented "industry norm of KES 1.2M" is flagged identically to a primary figure | Deterministic |
| a4 | A figure that IS in a tool body is absent from the footer (no false positive) | Deterministic |
| a5 | A GAK-scoped 900,000 does not satisfy an AFK claim (Stage 3+) | Deterministic |
| b1 | `_truncate` on an over-cap dict returns an **indexable container** — `out["data"]["messages"]` still works | Deterministic |
| b2 | `json.loads(json.dumps(out))` round-trips (today's `preview` raises `JSONDecodeError`) | Deterministic |
| b3 | `_truncation.cuts` names every cut path; `bytes_total > bytes_shown` | Deterministic |
| b4 | Truncation marker appears within the first 200 chars of `dispatch`'s output | Deterministic |
| c1–c3 | `quickbooks: PENDING` blocks financial domain; ACTIVE passes; `zoom: PENDING` does not block (Stage 4) | Deterministic |
| d1–d2 | `result_count == len(events)`; knowledge results carry `document_id` | Deterministic |
| — | *Does the refusal read acceptably to a human* | **LLM-judged — offline eval only, never assert** |

**Baseline confirmed today: b1, b2, b3 all fail against `main`.**

**Rule for the whole file: never assert on model prose.** The existing LLM-in-the-loop precedent (`tests/test_worldbank_tool_triggering.py`) makes real paid Anthropic calls at 6-way concurrency and is deliberately not pytest-collected. Keep it that way.

---

## What could NOT be verified

| Unknown | Why it matters |
|---|---|
| ~~Whether the `global` context-pack row exists in production~~ | **✅ RESOLVED 2026-07-27 — it does not; the table itself is absent. See the corrected section above.** |
| Maton's real `GET /connections` response body | **Blocks Stage 4 entirely.** Field name, literal values, and per-connection vs per-app shape are all unknown. |
| **Gmail encoding — partially resolved, and it contradicts the report.** `hermes/bills.py:366-385` shows Maton returns `format=full` bodies **base64url-encoded** (bills decodes them itself). No decoder exists in the agent tool path. A 20k cut of `messages.get` would land mid-base64 and show the model an opaque blob — **not** "partway through question 9." | Either the 25 Jul incident used a different path (`messages.list` snippets?), or the report's narrative doesn't match this code. Worth resolving before building Stage 1's worked example. |
| The exact Gmail path used on 25 Jul | There is no Gmail tool; the path was authored by the model at runtime inside `maton_request` and nothing logs it. Unrecoverable from code. |
| Which tool produced the 7 bounce notices | Can't say whether Step 2.3 would have caught the undercount. |
| Whether `DBRDS_HOST` is set in production | Decides whether `PostgresConversationStore` (drops all tool_result blocks at `db/conversations.py:153-156`) or the in-memory store is live — changes what Stage 3.3 buys. |
| Whether 25 Jul was chat or a scheduled task | Decides which prompt channel was live and whether write-tools had already fired. |
| Whether `hermes/agent.py` or one of its three `.bak` siblings is deployed | Only the non-`.bak` file was read. |
| How often the 20k cap actually fires | No logging at `maton.py:222-226`. There is no telemetry to answer this — which is itself a finding. |

---

## Deliberately deferred

| R | Status | Why |
|---|---|---|
| **R4** connector gating | Stage 4, behind a spike | Shape unverified; built blind it ships fail-open and never fires. **Step 0.3 catches the payroll case without it** — the figure was never retrieved, so it fails the set-match alone. R4 is defence in depth, not the primary control. |
| **R2 forced pagination** | Deferred indefinitely | No `pageToken`/`startAt`/`cursor` anywhere in `hermes/`. Auto-pagination multiplies calls into an account Gmail already rate-limits (`email_attachments.py:185-205` handles a 429) and consumes the same 12-iteration budget as real work. Stage 1 delivers *honest* truncation, not *complete* retrieval. **Do not claim R2 is closed.** |
| **R7** verbatim proper nouns, in code | Prompt rule only | Direct conflict: the packs assert "Loitokitok" (`HERMES_CONTEXT_PACKS.md:47,196`) and are injected as *authoritative over tool results* (`agent.py:148-150`), while a real in-repo source spells it "Loitoktok" (`scripts/nh_all_messages_jun17_jul17.md:818`). An edit-distance check would fire on pack-sanctioned spellings. Needs a precedence decision first. |
| **R8** match-scope honesty | Partial (Step 2.2, retrieval only) | There is **no thread search tool** — zero `threads`/`threadId` hits in `hermes/*.py`. Gmail search reaches the model only through raw `maton_request` with Gmail's own un-annotated response. |
| **R1 cross-turn** | Deferred | `PostgresConversationStore.get` rebuilds history text-only and `set()` is a no-op. A week of work, and its failure mode is **striking true statements a user already saw** — the fastest route to the flag being switched off. |
| **R3 inline striking** | Behind observe data | Breaks markdown, breaks TTS, flickers on stream. Footer gets 90% of the value. |
| **Write-tool gating** | Not attempted | `send_email` dispatches *inside* the loop, so a full-autonomy scheduled run can send before any reply-level gate. Needs verification *before* a draft exists — a genuinely different design. **Open risk; the current plan does not cover it.** |
