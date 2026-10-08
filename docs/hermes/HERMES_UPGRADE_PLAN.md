# HERMES UPGRADE PLAN — Implementation Work Orders

**Date:** 2026-07-24
**Repo:** `D:\Gandi AI Assistant` (Hermes — FastAPI agent backend + dashboard)
**Derived from:** code-level review of `hermes/agent.py`, `hermes/config.py`, `hermes/memory_retrieve.py`, `hermes/memory_extract.py`, `hermes/vector_store.py`, `hermes/tools.py` (tool surface), `.env.example`, `DATA_ARCHITECTURE_REPORT.md`, `PROJECT_SCOPE.md` §7, `PROJECT_CONTEXT.md`, `SCHEDULED_TASKS.md`
**Status:** DERIVED ANALYSIS — the code is truth. Line references are from the 2026-07-24 snapshot; re-verify at implementation time.

---

## How to use this document

Each task below is a **self-contained work order**: current state (with file references), exact steps, definition of done, and a test. Tasks are deliberately small and independent — ship any one of them alone. Dependencies are listed only where real.

**Handing a task to Claude Code (or any AI coding agent):** open the repo, paste one task block, and say: *"Implement this task exactly as specified. Verify the 'Current state' claims against the code first, then make the change, then run the test in the task."*

**Effort key:** S = under 2 hours · M = half a day · L = 1–2 days
**Priority key:** P0 = do first (safety/highest leverage) · P1 = this week · P2 = next · P3 = later

**Workstreams:**

| WS | Theme | Why it matters |
|----|-------|----------------|
| A | Unthrottle the agent (config + loop) | Hermes runs Claude at low effort, 8k tokens, 12 tool calls — the brain is leashed |
| B | Context packs | Hermes knows ~0 standing facts about Gandi's businesses; this closes the single biggest quality gap |
| C | RAG repair | Vector split-brain + PDFs never extracted = the knowledge base is partly empty and partly invisible |
| D | Playbooks | Written procedures for recurring jobs — clones the "skills" advantage |
| E | Typed tools | Stop burning tool iterations on hand-written raw REST calls |
| F | Observability + evals | You cannot improve what you don't measure; the DB tables already exist, unwired |
| G | Security hardening | The main API currently has no auth layer and CORS defaults to `*` |
| H | Parked (explicitly not now) | Semantic cache, MCP, subagents, Obsidian — with reasons |

---

## 0. Current state — read this before touching anything

**What Hermes is.** A FastAPI backend (`app.py`) + dashboard (`templates/index.html`, `static/*`) deployed on Vercel. The agent brain is the Claude API (`claude-sonnet-5` default, switchable to OpenAI) running a tool-use loop (`hermes/agent.py::stream_turn`). External apps are reached through the **Maton** gateway with one OAuth key per business (`afk`, `gak`, `gif`, `gakc`, `nh`, plus `per` on the shared default key — `hermes/config.py` L50–57). Storage: **Postgres 17.7 on AWS RDS** is the source of truth; **pgvector** on the same instance is the search index; **S3** holds original file bytes. Scheduled Tasks fire via cron hitting `/api/scheduled-tasks/run-due`, with per-task autonomy ceilings (`readonly` / `draft` / `full`).

**The six verified bottlenecks:**

1. **Throttled config** — `HERMES_EFFORT=low`, `HERMES_MAX_TOKENS=8000`, `HERMES_MAX_TOOL_ITERATIONS=12` (`hermes/config.py` L71–74). A cross-account sweep (5 Gmail lanes + QuickBooks + Calendar) exhausts 12 iterations mid-task.
2. **No standing context** — the system prompt (`hermes/agent.py::_system_prompt`, L77–147) is generic: app catalog + operating rules + date. Zero facts about the businesses. Extracted memories are injected but hard-capped at **2,000 characters total** (`hermes/memory_retrieve.py` L27, `_MAX_BLOCK_CHARS`).
3. **Vector split-brain** — the main app reads/writes **pgvector** (`hermes/vector_store.py`), but `ingestion-service/` still writes **Pinecone**. Anything the ingestion service indexes is invisible to `search_business_knowledge` (documented in `PROJECT_SCOPE.md` §7).
4. **PDFs never extracted in the main ingest path** — binary files get a `documents` row but no chunks/vectors (`PROJECT_SCOPE.md` §7; `DATA_ARCHITECTURE_REPORT.md` §6). Leases are PDFs → the knowledge base does not actually contain the leases.
5. **No playbooks** — every scheduled run re-derives its procedure from a one-line prompt.
6. **Raw tool ergonomics** — 31 tools, but most real work goes through `maton_request`, a raw REST passthrough; malformed calls and retries burn the 12-iteration budget.

**Also true and good — do not break:** per-business tenant isolation via Maton keys and pgvector namespaces; the 4-level sensitivity ladder enforced in SQL (`hermes/vector_store.py::query`); autonomy ceilings on scheduled runs; confirm-before-write rules in the system prompt; memory extraction with dedup/update thresholds (`hermes/memory_extract.py`, `config.py` L233–238); provider-agnostic conversation storage.

**Two identity schemes gotcha (affects several tasks):** `businesses` slugs (`afk`, `gak`, …) and `entities` slugs (`afro_farm`, `gak_properties`, …) are bridged only by `hermes/businesses.py::to_entity_slug`. Any new code touching both layers must go through that bridge (see how `hermes/memory_retrieve.py::_resolve_entity_id` does it).

---

# Workstream A — Unthrottle the agent

## A1. Raise effort, token, and tool-iteration budgets (env only)

**Priority:** P0 · **Effort:** S · **Depends on:** nothing · **Files:** `.env` (local), Vercel project env vars. No code.

**Current state**
- `hermes/config.py` L71–74: `MODEL=claude-sonnet-5`, `EFFORT=low`, `MAX_TOKENS=8000`, `MAX_TOOL_ITERATIONS=12`.
- Effort is passed per call in `hermes/agent.py::_anthropic_turn` (L314–318) via `output_config={"effort": Config.EFFORT}`; the loop cap is applied at L458 (`for _ in range(Config.MAX_TOOL_ITERATIONS)`).

**What to do**
1. Set in `.env` and in the Vercel project environment:
   ```
   HERMES_EFFORT=medium
   HERMES_MAX_TOKENS=16000
   HERMES_MAX_TOOL_ITERATIONS=32
   ```
2. Redeploy (Vercel env changes need a redeploy) and restart local dev.
3. Do NOT change `HERMES_MODEL` globally in this task — model tiering per run type is Task A2. Keep chat on sonnet for latency.

**Definition of done:** `GET /api/health` returns ok; a chat turn shows deeper reasoning (thinking summaries present); a multi-app request no longer dies at 12 tool calls.

**Test:** ask Hermes (business = `gak`): *"Check this week's unread email, pull AR aging from QuickBooks, and list next week's calendar — one combined summary."* Count `tool_calls` in the response JSON: it should complete with `stop_reason=end_turn`, not run out of loop.

**Risk / cost note:** higher effort + more iterations = more tokens = higher Anthropic API spend, roughly proportional to the extra thinking/tool traffic. Watch the first week of usage on the Anthropic console before raising further. If chat latency degrades, keep `HERMES_EFFORT=medium` for chat and rely on A2 to give scheduled runs `high`.

---

## A2. Per-run-type model and effort overrides (scheduled runs get the big brain)

**Priority:** P1 · **Effort:** M · **Depends on:** A1 · **Files:** `hermes/config.py`, `hermes/agent.py`, `hermes/scheduler.py`

**Current state**
- `stream_turn()` / `run_turn()` (`hermes/agent.py` L404, L562) take no model/effort/iteration parameters; everything reads global `Config`.
- The scheduler runs the *same* agent as chat (`SCHEDULED_TASKS.md` §5) — so unattended overnight runs, where latency is irrelevant, are stuck with interactive-chat settings.

**What to do**
1. Add to `Config`:
   ```python
   SCHEDULED_MODEL: str = os.getenv("HERMES_SCHEDULED_MODEL", "")        # empty = same as chat
   SCHEDULED_EFFORT: str = os.getenv("HERMES_SCHEDULED_EFFORT", "high")
   SCHEDULED_MAX_TOOL_ITERATIONS: int = int(os.getenv("HERMES_SCHEDULED_MAX_TOOL_ITERATIONS", "48"))
   ```
2. Add optional kwargs to `stream_turn(...)` and `run_turn(...)`: `model_id: str | None = None`, `effort: str | None = None`, `max_iterations: int | None = None`. Default `None` → current behavior (zero change for existing callers).
3. Thread `effort` into `_anthropic_turn` (replace the direct `Config.EFFORT` read at L317 with the parameter) and `max_iterations` into the L458 loop.
4. In `hermes/scheduler.py`, find the call into the agent for a task run and pass the scheduled overrides.
5. Set `HERMES_SCHEDULED_MODEL` to an Opus-class model id if/when Gandi approves the spend; ship with it empty first.

**Definition of done:** chat behavior unchanged; a scheduled run's stored trace shows the scheduled effort/iteration budget was applied.

**Test:** create a throwaway scheduled task ("daily", any read-only prompt), hit **Run now**, and confirm in the run history that a long multi-tool run completes; temporarily set `HERMES_SCHEDULED_MAX_TOOL_ITERATIONS=2` and confirm it degrades (proves the override is live), then restore.

---

## A3. Graceful wrap-up when the tool budget runs out

**Priority:** P1 · **Effort:** S–M · **Depends on:** nothing (compatible with A2) · **Files:** `hermes/agent.py`

**Current state**
- If the loop at `hermes/agent.py` L458 exhausts all iterations while the model still wants tools, execution falls out of the `for` with `reply == ""` → the user gets `"(no response)"` (L551–557). Worst possible failure mode for an unattended scheduled run: work done, nothing reported.

**What to do**
1. After the loop, detect the exhausted case (`stop_reason == "tool_use"` and no final text).
2. Append a user message: `"[system] Tool budget for this turn is exhausted. Do not call more tools. Write your best final answer from what you already gathered, and list explicitly what you could not verify."`
3. Make ONE more model call with `tool_choice={"type": "none"}` (keep the `tools` list — history contains `tool_use` blocks, so tool definitions must still be present) and use its text as `reply`.
4. Persist that closing exchange to the conversation like any other turn.

**Definition of done:** iteration exhaustion always yields a real, honest summary — never `"(no response)"`.

**Test:** set `HERMES_MAX_TOOL_ITERATIONS=2`, ask a 3-app question, confirm you get a partial-results answer that names what's missing. Restore the setting.

---

# Workstream B — Context packs (the highest-leverage change in this document)

**Concept.** Claude-for-Gandi is good mostly because every turn starts with a large curated brief (entities, tenants, rules, watchlists) that is *always present* — not retrieved in fragments. Hermes currently injects at most 2,000 characters of auto-extracted memory. This workstream gives Hermes the same always-present brief: one **global pack** (who Gandi is, entity separation rules, confirmation rules) + one **pack per business** (rosters, watchlists, standing facts), stored in Postgres, injected whole into the system prompt.

**Single-copy rule (binding):** packs are DERIVED COPIES generated from Gandi's canonical sources (his CLAUDE.md governance file and `gandi_hub.db` on the command-center machine). Stamp every pack with `<!-- DERIVED from canonical sources, generated YYYY-MM-DD — do not hand-edit facts here; regenerate -->`. On any conflict, canonical wins.

## B1. `context_packs` table + admin endpoints + cached loader

**Priority:** P0 · **Effort:** M · **Depends on:** nothing · **Files:** new migration in `hermes/db/migrations/`, new `hermes/db/context_packs.py`, new `hermes/context_packs.py`, `app.py`

**What to do**
1. Migration:
   ```sql
   CREATE TABLE IF NOT EXISTS context_packs (
     pack_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     slug       text UNIQUE NOT NULL,   -- 'global' | 'afk' | 'gak' | 'gif' | 'gakc' | 'nh' | 'per'
     content_md text NOT NULL DEFAULT '',
     version    int  NOT NULL DEFAULT 1,
     updated_at timestamptz NOT NULL DEFAULT now()
   );
   ```
2. Repo module `hermes/db/context_packs.py`: `get_pack(slug)`, `upsert_pack(slug, content_md)` (bump `version` on update). Follow the `cursor()` pattern from `hermes/db/connection.py`.
3. Loader `hermes/context_packs.py`: `packs_for(active_business: str | None) -> str` returning `global` pack + the locked business's pack concatenated with clear headers. In-process TTL cache (5 minutes) so it costs ~0 per turn. **Fail open**: any DB error returns `""` — a pack failure must never break a turn (mirror the try/except style of `build_memory_block` usage in `agent.py` L440–445).
4. Endpoints in `app.py`: `GET /api/context-packs/{slug}` and `PUT /api/context-packs/{slug}` (body: `{content_md}`). Gate with auth once G1 lands.
5. Business slugs only (`businesses` scheme: `afk`/`gak`/…), NOT `entities` slugs. No bridging needed here since packs key off the chat's `active_business`.

**Definition of done:** PUT then GET round-trips a pack; loader returns concatenated global+business content; cold DB → empty string, no exception.

**Test:** `curl -X PUT .../api/context-packs/gak -d '{"content_md":"# TEST PACK"}'` then run a `gak` chat turn with B2 in place and verify the text reaches the prompt (log it at debug level).

---

## B2. Inject packs into the system prompt

**Priority:** P0 · **Effort:** S · **Depends on:** B1 · **Files:** `hermes/agent.py`

**Current state**
- `_system_prompt()` (`hermes/agent.py` L77–147) assembles: base identity → business-scope note → `MEMORY` block → attachments ledger → optional voice style.

**What to do**
1. Add parameter `context_pack: str = ""` to `_system_prompt()` and insert it **after** the business-scope note and **before** the MEMORY block, as:
   ```
   BUSINESS CONTEXT PACK (authoritative standing facts and rules — trust these over memory or retrieval; if a tool result contradicts them, say so explicitly):
   <pack content>
   ```
2. In `stream_turn()`, fetch once per turn next to the `build_memory_block` call (L440–445): `context_pack = packs_for(active_business)` wrapped in the same never-fail try/except.
3. Size guard: if combined packs exceed ~40,000 characters, log a warning and truncate the *business* pack tail (never the global pack) with a `[pack truncated]` marker. Packs should be curated well under this.

**Definition of done:** every turn for a locked business carries global + business pack; "all businesses" mode carries the global pack only.

**Test:** with the B1 test pack containing a sentinel fact ("The test sentinel word is QUARTZ"), ask Hermes *"what is the test sentinel word?"* → answers QUARTZ with zero tool calls.

---

## B3. Author the seven packs (content task — no code)

**Priority:** P0 · **Effort:** M (with Claude's help) · **Depends on:** B1 (to store), B2 (to use) · **Files:** none (content via PUT endpoint)

**What to write — structure per pack:**

- **`global`** (~600–900 words): who Gandi is (voice-dictation user — interpret meaning, never correct grammar); the six businesses in one table; HARD RULES: strict entity separation (never blend books/accounts), related-party map (1st Attentive ↔ Samsam at 3 GAK sites; NH ↔ Gandi at 720 E Lake; tag these in any AR/rent output), confirm-before-write reminder, email-only tenant outreach (no SMS/WhatsApp to tenants unless Gandi explicitly directs), reply format (short answer first; scannable; label: value lines), halal/prayer-time awareness for scheduling, Kenya Window = 6–9 AM CST for AFK calls.
- **`gak`** (~400–700 words): 7 properties; per-property tenant roster with lease end dates; watchlist (A Caring Living 6733 + 7601 both end 09/30/2026 — renewal decision NOW; 2027 cluster; Caremark + Khadijah lease bodies erroneously reference 1229 — fix at renewal); COI-per-tenant rule; MN property tax May 15 / Oct 15.
- **`gif`** (~400–700 words): 3 properties; 1229 E Lake roster incl. Clear Channel billboard (auto-renewing year-to-year since 03/2021 — renegotiation candidate; income booked as separate signage line, not base rent); 2609 15th Ave S VACANT — leasing priority; 1230 Logan (Love & Care).
- **`afk`** (~300–500 words): 128-acre Loitokitok farm; ~54 staff; key people (Abdirahman Abdiaziz — Manager & Finance; Mohamed — Logistics/Security); reporting cadence 9/12/4 EAT; GLOBALG.A.P. prep; KES currency; Kenya Window.
- **`gakc`** (~150 words): Kenya RE entity; distinct from Kediye Holdings; minimal standing facts.
- **`nh`** (~300 words): pre-formation; women-only hammam at 720 E Lake west wing (related-party lease with GAK — arm's-length, documented both sides, target ~June 2026); buildout compliance items; treat service details as confidential.
- **`per`** (~200–400 words): father Yusuf (97, daily care); prayer-time/halal scheduling defaults; keep personal finance separate from every entity.

**Sourcing:** generate from Gandi's canonical CLAUDE.md and `gandi_hub.db` exports; stamp the DERIVED header + date on each. **Never put in packs:** SSNs/EINs, bank account numbers, API keys, CS patient information (PHI stays out of Hermes entirely — see G3).

**Definition of done:** all 7 packs stored via `PUT /api/context-packs/{slug}`, each with the DERIVED stamp.

**Test:** three zero-tool-call questions answered correctly from packs: *"Which GAK tenants are related-party?"*, *"Which leases expire in 2026?"*, *"What's the standing rule for contacting tenants?"*

---

## B4. Raise the memory-injection ceiling

**Priority:** P0 · **Effort:** S · **Depends on:** nothing · **Files:** `hermes/memory_retrieve.py`, `hermes/config.py`, `.env.example`

**Current state**
- `hermes/memory_retrieve.py` L27: `_MAX_BLOCK_CHARS = 2000` — the ENTIRE injected memory (always-inject + semantic) is capped at ~300 words, then truncated mid-sentence (L118–119).
- `Config.MEMORY_SEMANTIC_TOP_K = 5` (`config.py` L237).

**What to do**
1. Make the cap env-driven: `MEMORY_BLOCK_MAX_CHARS` default **8000**; replace the module constant.
2. Raise `MEMORY_SEMANTIC_TOP_K` default to **8**.
3. Truncate on a line boundary (split at the last `\n` before the cap) instead of mid-fact.
4. Document both in `.env.example`.

**Definition of done:** memory blocks above 2k chars render whole; truncation (if ever hit) ends on a complete line.

**Test:** seed 30 always-inject memories in a dev DB; confirm the block includes all of them and the prompt shows no `...(truncated)` marker.

---

## B5. Stop the extractor from re-learning what the packs already say

**Priority:** P2 · **Effort:** S · **Depends on:** B3 · **Files:** `hermes/memory_extract.py`

**Current state**
- The extractor (`memory_extract.py`, Haiku via `call_claude_json`, L144–147) has no knowledge of context packs, so it will keep saving facts the packs already carry — noise that burns the (now larger) memory budget.

**What to do**
1. In `_build_system()`, append a short section: `"ALREADY KNOWN (do not extract duplicates of these):"` followed by the first ~1,500 chars of the relevant business pack + global pack headings (fetch via the B1 loader; fail-open to empty).
2. Keep everything else unchanged — thresholds (`MEMORY_DUP_THRESHOLD=0.92`) already dedupe *similar* memories; this stops *exact-known* facts at the source.

**Definition of done:** a turn that restates a pack fact produces `{"memories": []}`.

**Test:** tell Hermes "remember: 2609 15th Ave S is vacant" (already in the `gif` pack) → extractor logs no write; tell it a genuinely new fact → one write.

---

# Workstream C — RAG repair (make the knowledge base real)

## C1. Kill the vector split-brain: one store (pgvector)

**Priority:** P1 · **Effort:** L · **Depends on:** nothing · **Files:** `ingestion-service/ingestion/indexer.py`, `ingestion-service/ingestion/pinecone_store.py`, `ingestion-service/ingestion/db.py`, `ingestion-service/ingestion/repo.py`; reference: `hermes/vector_store.py`

**Current state**
- Main app: pgvector on the shared Postgres (`hermes/vector_store.py`; migration `0008_pgvector.sql`). Reads go through the guarded `query()` (sensitivity + namespace filters in SQL).
- `ingestion-service/` still writes **Pinecone** (`ingestion/pinecone_store.py`) and its README describes a main-app read path (`hermes/pinecone_store.query`) that **no longer exists**. Net effect: documents indexed by the ingestion service are invisible to `search_business_knowledge`. This is called out verbatim in `PROJECT_SCOPE.md` §7 ("Vector-store split-brain").

**Decision (recommended): consolidate on pgvector.** Postgres is already the source of truth; one store means one guard path, one backup story, no Pinecone bill.

**What to do**
1. **VERIFY FIRST** (30 min, before writing code): map exactly which database and tables `ingestion-service` writes (`ingestion/db.py`, `ingestion/repo.py`, `migrations/001_ingestion.sql`). Determine: same RDS instance as the main app, or separate? Same `documents`/`document_chunks` tables, or its own?
2. If it writes the **same** `document_chunks` table: replace `pinecone_store.upsert` calls in `indexer.py` with the main app's pattern — embed via OpenAI, then `UPDATE document_chunks SET embedding=..., embed_model=..., text_preview=...` keyed on the chunk's vector id (copy the SQL from `hermes/vector_store.py::upsert_chunks`, L119–129).
3. If it writes **its own** tables: pick one — (a) migrate its writes into the main `documents`/`document_chunks` tables (preferred), or (b) retire the ingestion service entirely and fold its workers (Drive sync, OCR, sensitivity tagging in `ingestion/sensitivity.py`) into the main app's ingest path. Option (b) is the right call if nothing in production depends on the service running standalone — fewer moving parts.
4. Delete/quarantine `pinecone_store.py` usage; update the service README (it currently documents the dead Pinecone read path).
5. Decommission the Pinecone index only after C-workstream acceptance tests pass.

**Definition of done:** a document ingested through the ingestion service is retrievable via the agent's `search_business_knowledge` tool.

**Test:** ingest a marker file (`"The C1 sentinel is BASALT"`) through the ingestion service into business `gak`; in chat (locked to `gak`) ask *"search business knowledge for the C1 sentinel"* → BASALT comes back with the doc reference.

---

## C2. Wire PDF/image text extraction into the main ingest path

**Priority:** P0 (your leases are PDFs) · **Effort:** M–L · **Depends on:** nothing · **Files:** `hermes/ingest.py`, possibly `hermes/extract.py`, `hermes/bills.py` (reuse), `hermes/config.py`

**Current state**
- `ingest_document` only indexes text-decodable files; binaries get a `documents` row but **no chunks and no vectors** (`DATA_ARCHITECTURE_REPORT.md` §6; `PROJECT_SCOPE.md` §7). So a lease PDF uploaded to Business Context is stored but unsearchable.
- Working extraction already exists elsewhere in the codebase: the bills flow uses the **NuExtract** SDK (`hermes/bills.py`, key `NUEXTRACT_API_KEY`), and `Config.OCR_FALLBACK_MODEL` (`config.py` L252, `claude-sonnet-5`) defines a Claude-vision fallback pattern. `hermes/extract.py` exists (13.6 KB) — **VERIFY** what it already does before writing anything new; extend rather than duplicate.

**What to do**
1. In the ingest path, branch on content type:
   - `application/pdf` → extract text per page. Order of preference: (1) whatever `hermes/extract.py` already provides, (2) the NuExtract client pattern from `bills.py`, (3) Claude vision fallback — send the PDF as a document block to `OCR_FALLBACK_MODEL` with prompt "Transcribe this document faithfully as plain text, preserving headings and clause numbering," page-batched.
   - images (`image/*`) → Claude vision transcription, same fallback model.
2. Keep the page number with each extracted page: `{"page": n, "text": ...}` — feed page-tagged text into chunking so C4/C5 can cite pages.
3. **Backfill script** `scripts/backfill_extract.py`: find `documents` rows with zero `document_chunks`, pull bytes from S3 (`documents.s3_uri`), run the new extraction, chunk, embed, insert. Batch with a `--limit` flag; log per-doc success/failure.
4. Respect sensitivity: extracted chunks inherit the parent document's `sensitivity_level` unchanged.

**Definition of done:** uploading a scanned lease PDF produces `document_chunks` rows with embeddings; backfill fills historical gaps.

**Test:** upload one real lease PDF to business `gak` → `SELECT count(*) FROM document_chunks WHERE document_id = ...` > 0 → ask *"what is the lease end date in <tenant> lease?"* → correct answer citing the document.

---

## C3. Bulk-ingest the documents that matter (leases first)

**Priority:** P1 · **Effort:** M · **Depends on:** C2 · **Files:** new `scripts/bulk_ingest.py`

**Current state**
- The knowledge base is only as good as what's in it; today it holds whatever was manually uploaded through the Business Context tab. The 17 leases, insurance certs, and key vendor contracts are the highest-value corpus and are not systematically ingested.

**What to do**
1. Script `scripts/bulk_ingest.py` that walks a local folder tree and POSTs each file to the existing ingest endpoint:
   ```
   python scripts/bulk_ingest.py --business gak --folder ./staging/gak-leases --sensitivity internal --doc-type lease
   ```
2. Dedupe by SHA-256 of file bytes (keep a small manifest table or JSON log) so re-runs are idempotent.
3. Sensible defaults: `--sensitivity internal`; allow `confidential` for anything with personal data.
4. Ingest priority order: (1) all 17 lease PDFs per property/entity, (2) insurance COIs, (3) Clear Channel billboard agreement (1229 E Lake), (4) key vendor contracts, (5) entity formation docs.
5. Do NOT ingest: anything with bank account numbers/SSNs/EINs in bulk without review; nothing from Comfort Services healthcare records (see G3).

**Definition of done:** lease corpus indexed per business; manifest shows what went in, with hashes.

**Test:** *"Compare the end dates of both A Caring Living leases"* (business `gak`) → correct dates, two document citations, no hallucinated terms.

---

## C4. Retrieval quality: title-prefixed chunks + bigger k + titled results

**Priority:** P2 · **Effort:** S–M · **Depends on:** C2 (worth doing after real docs exist) · **Files:** `hermes/ingest.py`, `hermes/vector_store.py`, `hermes/search.py`, `hermes/tools.py` (the `search_business_knowledge` handler)

**Current state**
- Chunks are embedded as bare 1,000-char text slices (chunking per `DATA_ARCHITECTURE_REPORT.md` §5.2: 1,000 chars / 150 overlap) — a chunk from page 6 of a lease says "Tenant shall..." with no clue *which* tenant or document. `query()` default `top_k=8` (`vector_store.py` L139).

**What to do**
1. At embed time, prefix every chunk with a header line built from its document row: `Document: <title> (<doc_type>, <business>) — part <i>/<n>\n\n<chunk text>`. Header goes into the embedded text AND `text_preview` so both search relevance and displayed snippets improve.
2. Raise `top_k` for `search_business_knowledge` to 12 (keep the function default at 8 for other callers).
3. Return document title + page (when available from C2) in the tool result so the agent can cite properly.
4. Requires re-embedding existing chunks to benefit → fold into the C2 backfill run (one pass, both fixes).
5. **Stretch (separate PR, optional):** hybrid search — add a `tsvector` column + GIN index on `document_chunks.text_preview`, run BM25-ish full-text alongside vector search, merge with reciprocal-rank fusion. Only do this after measuring that pure-vector misses keyword queries (lease clause numbers, dollar amounts).

**Definition of done:** search results show titled, cite-able chunks; exact-name queries ("Caremark") reliably surface the right document.

**Test:** ask for a specific clause by tenant name; the returned snippet header names the correct lease.

---

## C5. Structure-aware chunking

**Priority:** P2 · **Effort:** M · **Depends on:** C2 · **Files:** wherever `chunk_text` lives in the main ingest path (`hermes/ingest.py`), `ingestion-service/ingestion/chunker.py` if the service survives C1

**Current state**
- Fixed 1,000-char / 150-overlap character slicing cuts lease clauses mid-sentence and splits rent tables from their headings.

**What to do**
1. Replace with a two-level splitter: split on structural boundaries first (page breaks from C2, then blank-line paragraphs / numbered-clause starts like `^\s*\d+(\.\d+)*\s`), then pack paragraphs greedily into chunks of ~400–800 tokens (~1,600–3,200 chars) with one-paragraph overlap.
2. Store `page_start`/`page_end` (or clause label when detected) in chunk metadata for citations.
3. Keep the function signature compatible so callers don't change.

**Definition of done:** chunks start at clause/paragraph boundaries; average chunk carries a whole provision.

**Test:** inspect 10 random chunks from a re-ingested lease — no mid-sentence starts; the rent schedule table lands in one chunk.

---

# Workstream D — Playbooks (skills for Hermes)

**Concept.** A playbook is a written procedure for a recurring job: inputs, steps, which tools, output format, guardrails. Injected into the system prompt for the run, it converts "figure it out from a one-line prompt" into "follow the checked procedure" — the single biggest quality lever for scheduled runs. This is exactly what Claude's "skills" are: markdown procedures.

## D1. Playbook store + scheduled-task wiring (no classifier — explicit selection)

**Priority:** P1 · **Effort:** M · **Depends on:** nothing · **Files:** new migration, new `hermes/db/playbooks.py`, `hermes/scheduler.py`, `hermes/db/scheduled_tasks.py`, `app.py`, Tasks tab UI (`static/tasks.js`) optional

**What to do**
1. Migration:
   ```sql
   CREATE TABLE IF NOT EXISTS playbooks (
     playbook_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     slug        text UNIQUE NOT NULL,
     name        text NOT NULL,
     description text NOT NULL DEFAULT '',   -- one line: when to use it (feeds D2 router later)
     content_md  text NOT NULL,
     enabled     boolean NOT NULL DEFAULT true,
     updated_at  timestamptz NOT NULL DEFAULT now()
   );
   ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS playbook_slug text;
   ```
2. CRUD endpoints: `GET /api/playbooks`, `GET/PUT /api/playbooks/{slug}` (auth-gated after G1).
3. Scheduler wiring: when a task has `playbook_slug`, prepend to the run's prompt/system context:
   ```
   PLAYBOOK — follow this procedure exactly; deviate only if a step is impossible, and say so in the report:
   <content_md>
   ```
4. v1 is **explicit selection only** (a scheduled task names its playbook; a chat user can say "run the friday-brief playbook"). Auto-routing is D2, behind a flag.

**Definition of done:** a scheduled task bound to a playbook runs with the procedure in-prompt; run report reflects the playbook's output format.

**Test:** bind a test task to a playbook whose output format ends with a fixed marker line; **Run now**; the marker appears in the run report.

---

## D2. Chat auto-router for playbooks (flagged off by default)

**Priority:** P2 · **Effort:** M · **Depends on:** D1 · **Files:** new `hermes/playbook_router.py`, `hermes/agent.py`, `hermes/config.py`

**What to do**
1. Reuse the cheap-classifier pattern that already exists for memory extraction (`hermes/memory_extract.py` → `call_claude_json` on `Config.GUIDANCE_MODEL`, the Haiku tier): input = user message + the playbook catalog (slug + one-line description); output `{"playbook": "<slug>" | null}`. Instruct: *"Pick a playbook ONLY when the request clearly matches its description; when in doubt return null."*
2. Config flag `PLAYBOOK_ROUTER_ENABLED` (default `false`) + `PLAYBOOK_ROUTER_MODEL` (default = `GUIDANCE_MODEL`).
3. In `stream_turn()`, when enabled and a slug returns, inject the playbook the same way D1 does for scheduled runs. Fail-open: router error → no playbook.
4. Log every routing decision (chosen slug or null) for a week before enabling by default.

**Definition of done:** with the flag on, "who owes me money" pulls `arrears-chase`; "what's the weather" pulls nothing.

**Test:** 10 canned utterances (5 should route, 5 shouldn't) — ≥9/10 correct before enabling in production.

---

## D3. Author the first five playbooks (content task — no code)

**Priority:** P1 · **Effort:** M · **Depends on:** D1 · **Files:** none (content via PUT endpoint)

Each playbook: **Purpose · Inputs · Steps (numbered, tool-by-tool) · Output format · Guardrails.** Keep each 300–600 words. The five, with their core steps:

1. **`arrears-chase`** (business: gak or gif) — Pull AR aging from QuickBooks via Maton → match open invoices to tenants → cross-check Gmail (tenant-comms lane) for payment promises/disputes in the last 30 days → rank by amount × age → output: table (tenant | property | amount | days late | last contact | recommended action) → **tag related-party tenants (1st Attentive, NH) explicitly** → draft (never send) reminder emails for the top 3 — autonomy `draft` ceiling; sending requires Gandi.
2. **`friday-brief`** (all businesses) — Per entity: cash/AR headline (QuickBooks where connected), urgent unread email count + top 3 subjects, next-7-days calendar, lease watchlist items inside 90 days, open scheduled-task failures → one page, entity-labeled sections, no blended numbers → end with "Decisions needed" list.
3. **`lease-renewal-check`** (gak + gif) — From the context pack's watchlist: leases ending ≤ 180 days out → for each: search business knowledge for the lease doc (renewal notice period, escalation clause), check Gmail for existing renewal threads → output per lease: end date | notice deadline | current rent | status | next action. First targets: A Caring Living × 2 (end 09/30/2026).
4. **`month-close-prep`** (one business per run) — List QuickBooks uncategorized transactions, unmatched deposits, open invoices past terms → bank-feed vs ledger deltas → output: close checklist with per-item status; never write to QuickBooks; flag related-party items for memo documentation.
5. **`kenya-window-brief`** (afk) — Overnight email from the AFK lane + form submissions if reachable → staffing/security flags → weather + market context (web search, cite) → 5-bullet brief ready before 6 AM CST; timezone-explicit (EAT vs CST).

**Definition of done:** five playbooks stored, each bound to at least one scheduled task (friday-brief → Friday morning; kenya-window-brief → daily early AM; arrears-chase → weekly Monday; month-close-prep → monthly per entity; lease-renewal-check → weekly until 09/2026 renewals resolved).

**Test:** run each once via **Run now**; every report follows its playbook's output format and stays inside its autonomy ceiling.

---

# Workstream E — Typed tools (stop paying the raw-REST tax)

**Concept.** `maton_request` makes the model hand-write native REST calls (Gmail query syntax, QuickBooks report endpoints, Zoom payloads). Every malformed attempt burns a tool iteration and pollutes context with error JSON. The fix is the pattern the codebase **already uses** for `zoom_send_invite`, `calendar_list_upcoming`, `send_email`, `email_with_attachment` (see `hermes/tools.py`): a typed tool with a small schema that wraps the raw call internally. Add typed wrappers for the highest-traffic operations; keep `maton_request` as the escape hatch.

**Shared implementation notes for E1–E3**
- Follow the existing dedicated-tool pattern in `hermes/tools.py`: add the tool def in `build_tools()`, a `dispatch()` branch, and a human-readable `tool_status` label.
- Every tool takes optional `business` (used only in "all businesses" mode — mirror how existing tools resolve the per-business Maton key).
- Return **compact, model-friendly JSON** (only the fields the agent needs), not the raw API response.

## E1. Typed Gmail tools

**Priority:** P2 · **Effort:** M · **Depends on:** nothing · **Files:** `hermes/tools.py`

**Add:**
```
gmail_search   {query: str, newer_than_days?: int = 30, max_results?: int = 20, business?: str}
gmail_read_thread {thread_id: str, business?: str}
```
- `gmail_search` builds the Gmail query internally (append `newer_than:{n}d`), returns `[{thread_id, from, subject, date, snippet, unread}]`.
- `gmail_read_thread` returns the thread's messages with plain-text bodies (strip HTML), truncated per message to ~4,000 chars.
- Update the system prompt guideline block (`hermes/agent.py::_system_prompt`) to steer email reads to these tools, keeping `maton_request` for anything else (labels, drafts).

**Definition of done / test:** *"any unpaid-rent emails this month?"* (business `gif`) completes using `gmail_search` + `gmail_read_thread` in ≤ 4 tool calls, where the raw path typically needed more.

## E2. Typed QuickBooks tools

**Priority:** P2 · **Effort:** M · **Depends on:** nothing · **Files:** `hermes/tools.py`

**Add:**
```
qbo_ar_aging      {business: str, as_of?: str}
qbo_profit_loss   {business: str, period_start: str, period_end: str}
qbo_list_invoices {business: str, status?: "open"|"overdue"|"paid", customer_name?: str, max_results?: int = 50}
```
- Wrap the corresponding QuickBooks report/query endpoints via Maton; return flattened rows (customer | amount | bucket/date), not the raw QBO report tree — the report JSON is enormous and mostly noise for the model.
- These three cover the arrears-chase, friday-brief, and month-close-prep playbooks end to end.

**Definition of done / test:** `arrears-chase` playbook run makes zero `maton_request` calls for its QBO reads.

## E3. Typed Calendar write + availability

**Priority:** P3 · **Effort:** S–M · **Depends on:** nothing · **Files:** `hermes/tools.py`

**Add:**
```
calendar_create_event {title: str, start_iso: str, end_iso: str, attendees?: [str], description?: str, business?: str}
calendar_free_slots   {date_from: str, date_to: str, duration_minutes: int, business?: str}
```
- `calendar_create_event` is a WRITE → it must stay behind the existing confirm-before-write prompt rule and the scheduler autonomy ceiling. No silent invites.

**Definition of done / test:** "find me a 30-minute slot Tuesday and book it" produces a confirm step, then one clean create call.

## E4. Tool-result size guard

**Priority:** P1 (cheap, protects everything else) · **Effort:** S · **Depends on:** nothing · **Files:** `hermes/tools.py` (`dispatch()` return path), `hermes/config.py`

**Current state**
- **VERIFY FIRST:** check whether `dispatch()` already truncates large results anywhere. A raw `maton_request` against a QBO report or a big Gmail list can return tens of thousands of characters of JSON; at 12–32 iterations that floods the context window and degrades every later step of the turn.

**What to do**
1. Add `TOOL_RESULT_MAX_CHARS` (default `20000`) to `Config`.
2. In the shared `dispatch()` return path, when `content_str` exceeds the cap: keep head (60%) + tail (20%), insert `\n...[truncated N chars — refine the request or ask for a narrower range]...\n`. Never truncate `is_error` payloads below ~2,000 chars (the error text is what lets the model self-correct).
3. Log tool name + original size whenever truncation fires — that log is the shopping list for the next typed wrapper.

**Definition of done / test:** a deliberately huge `maton_request` (full QBO GL pull) returns a bounded result; the turn stays coherent afterward.

---

# Workstream F — Observability + evals (the tables already exist)

## F1. Persist agent runs and tool calls

**Priority:** P1 · **Effort:** M · **Depends on:** nothing · **Files:** `app.py` (post-turn hook, next to `store.record_turn`), new `hermes/db/runs.py`, existing schema

**Current state**
- `hermes_agent_runs`, `hermes_tool_calls`, `ai_runs`, `audit_logs` exist in the schema but **no code writes them** (`DATA_ARCHITECTURE_REPORT.md` §2.3 "defined vs. used"; `PROJECT_SCOPE.md` §7). Meanwhile the agent already produces a full `tool_trace` in memory every turn (`hermes/agent.py` L453, L488–513) and then drops it except for chat-replay metadata.

**What to do**
1. **VERIFY FIRST:** read the two tables' actual columns in `hermes/db/migrations/0001_init.sql`; adjust with a small migration only if required fields are missing (e.g., `effort`, `iterations_used`).
2. After each completed turn (same place `record_turn` is called in `app.py`): insert one `hermes_agent_runs` row — session_id, business slug, model_id, effort, stop_reason, iterations used, duration_ms, token usage (see F3) — and one `hermes_tool_calls` row per trace entry (tool, ok, label; store input JSON with any obvious secrets redacted).
3. Same hook for scheduled runs (they flow through the same agent; confirm the scheduler path also lands here — if it records separately, write the same rows there).
4. Fail-open: persistence errors log a warning, never break the reply (match the style used across `hermes/db/conversations.py`).

**Definition of done:** every chat and scheduled turn leaves a run row + tool rows.

**Test:** run 3 turns; `SELECT count(*) FROM hermes_agent_runs` increments by 3; tool rows match each reply's `tool_calls` array.

## F2. Golden-question eval harness (weekly, automated)

**Priority:** P2 · **Effort:** M–L · **Depends on:** F1 helpful, B/C landed (else scores are meaningless) · **Files:** new migration (`eval_questions`, `eval_runs`), new `scripts/run_evals.py`, `hermes/job_runner.py` (register a job)

**What to do**
1. Table `eval_questions`: `question_id, business_slug, question, must_contain text[] , must_not_contain text[], notes, enabled`.
2. Seed 15–20 per major business. Examples — `gak`: *"Which tenants are related-party?"* must_contain `["1st Attentive"]`; *"When do the A Caring Living leases end?"* must_contain `["2026"]`; `gif`: *"Which property is vacant?"* must_contain `["2609"]`; *"Who is the billboard tenant at 1229?"* must_contain `["Clear Channel"]`.
3. `scripts/run_evals.py`: for each enabled question, call `run_turn()` fresh (no session reuse), grade: (a) mechanical must_contain/must_not_contain check, (b) a Haiku judge (`call_claude_json` pattern) scoring 0–2 for correctness/citation. Write `eval_runs` rows (question_id, score, answer_excerpt, tokens, date).
4. Register as a job in the existing Jobs registry (`hermes/job_runner.py` — same pull-based cron as the reviews scraper), weekly.
5. Output: one summary line per business (avg score, regressions vs last week) — visible in the Jobs tab run history.

**Definition of done:** weekly scored report; a deliberately broken question (sabotage a pack fact in dev) shows up as a regression.

**Test:** run once manually; verify scores and that a known-answer question passes and a nonsense question fails.

## F3. Token + cost telemetry per run

**Priority:** P2 · **Effort:** S · **Depends on:** F1 · **Files:** `hermes/agent.py`, `hermes/db/runs.py`

**What to do**
1. In `_anthropic_turn`, the SDK's `stream.get_final_message()` result (L332) carries `resp.usage` (input/output token counts) — accumulate across iterations of the turn and include totals in the `_final`/`done` event.
2. Store on the F1 run row; add `input_tokens`, `output_tokens` columns if the schema lacks them.
3. Simple rollup endpoint `GET /api/usage/summary?days=7` → tokens by day and by run type (chat vs scheduled). Dashboard tile later, optional.

**Definition of done / test:** after A1/A2, Gandi can answer "what did the effort increase actually cost this week?" from the rollup instead of guessing.

---

# Workstream G — Security hardening (do G1–G2 before anything else ships)

## G1. Auth on the main API + real CORS

**Priority:** P0 · **Effort:** M · **Depends on:** nothing · **Files:** `app.py`, `hermes/config.py`, `static/boot.js` (or wherever the frontend bootstraps), Vercel env

**Current state**
- `PROJECT_SCOPE.md` §7, verbatim: **"No auth layer on the Main API"** — only the ingestion service's routes are protected. `.env.example` defaults `CORS_ORIGINS=*`. The deployment target is Vercel (public URL). An unauthenticated public `/api/chat` is a direct line to an agent holding tenant data and connected business accounts. **This is the most urgent item in the entire document.**

**What to do**
1. Add `HERMES_API_TOKEN` to config. FastAPI dependency on ALL `/api/*` routes requiring `Authorization: Bearer <token>`, with three exemptions: `GET /api/health`, and the cron endpoints `/api/scheduled-tasks/run-due` + `/api/jobs/run-due` (already guarded by `SCHEDULER_SECRET`/`CRON_SECRET` — keep that mechanism).
2. Frontend: minimal v1 — a token prompt on first load stored in memory/localStorage, sent on every fetch. (The dashboard is single-owner; a full login system is not needed yet.)
3. Set `CORS_ORIGINS` to the exact dashboard origin(s). Remove `*` in production.
4. Interim stopgap (same day, zero code): enable Vercel Deployment Protection / password on the deployment until the token middleware ships.

**Definition of done:** unauthenticated `POST /api/chat` → 401; dashboard works with the token; cron endpoints still fire.

**Test:** curl without token → 401; with token → 200; Vercel cron run still appears in run history.

## G2. Secrets hygiene sweep

**Priority:** P0 · **Effort:** S · **Depends on:** nothing · **Files:** none (ops checklist)

**What to do**
1. Confirm `.env` is git-ignored AND absent from history: `git log --all --oneline -- .env` and `git log --all -p -S "sk-ant" -- . | head`. The repo also embeds a second git repo under `ingestion-service/.git` — check both.
2. If any key ever landed in history: rotate it (Anthropic, OpenAI, Maton ×6 business keys, AWS, NuExtract, SerpAPI, Groq) and scrub or accept-and-rotate.
3. Verify in Vercel: `CRON_SECRET` (or `SCHEDULER_SECRET`) is actually set — if unset, the run-due endpoints are open (per `.env.example` L36–41 and `config.py` L160–164).
4. RDS security group: not 0.0.0.0/0 on 5432; S3 bucket: block public access; confirm `sslmode=require` in prod (default per `hermes/db/connection.py` notes).
5. Redact tool inputs when F1 starts persisting them (no raw tokens in `hermes_tool_calls.input`).

**Definition of done:** written checklist result per item — done / rotated / N-A — kept with the repo docs.

## G3. Keep MOST-SENSITIVE data out of Hermes (policy gate)

**Priority:** P1 · **Effort:** S · **Depends on:** nothing · **Files:** ingest endpoint in `app.py`, `CONTRIBUTING`-style note in README

**Current state**
- Gandi's standing privacy rule: Comfort Services patient PHI, immigration records, and attorney work product are **local-only — never cloud**. Hermes is entirely cloud (RDS/S3/Anthropic/OpenAI). Helpfully, CS is already NOT in Hermes' business list (`config.py` L50–57: afk/gak/gif/gakc/nh/per) — keep it that way.
- The memory pipeline's `MEMORY_MAX_SENSITIVITY=confidential` already stops `restricted` memories from being retrieved (`config.py` L238) — good, don't loosen it.

**What to do**
1. Ingest-time guard: reject uploads flagged `restricted` unless the request carries an explicit `allow_restricted=true` flag (which the UI does not expose). Error message: "Restricted-tier documents are not stored in Hermes by policy."
2. Add a short POLICY section to `README.md`: no CS healthcare records, no immigration documents, no attorney work product, no credentials — in any Hermes store (Postgres, S3, vectors, memories).
3. Do NOT add CS as a business/Maton key in future work without an explicit owner decision.

**Definition of done / test:** a `restricted` upload without the flag → 400 with the policy message; README carries the policy.

---

# Workstream H — Parked, with reasons (do NOT build these now)

## H1. Semantic cache — SKIP
Caches answers for *similar repeated queries across many users*. Hermes has one user asking novel, time-sensitive business questions ("this week's arrears") — cache hits would be rare and stale answers about money/tenants are worse than slow ones. The infographic concept doesn't fit this system. Revisit only if Hermes ever serves many external users asking overlapping questions.

## H2. MCP adoption — LATER, optional
Hermes reaches apps via Maton, which works. The Anthropic API supports attaching MCP servers server-side (MCP connector), which would let Hermes share the same connector ecosystem Claude uses and drop some Maton glue. Meaningful refactor, not a quick win. Park until the Maton dependency itself becomes a problem (pricing, coverage, reliability).

## H3. Subagents / planner–executor — LATER
Fan-out (one sub-run per entity, then merge) would help the friday-brief at scale. But A1/A2 + playbooks get 80% of the benefit with 5% of the complexity. Revisit when a single 48-iteration run demonstrably can't finish the weekly rollup.

## H4. Obsidian — NOT a performance fix
Obsidian is a markdown vault (a curation UI), not an engine — it improves the *human editing experience* for canonical files, not agent quality. Adding it now creates another memory home and drift risk against the single-copy rule. Acceptable future role: the editing surface for the canonical files that context packs (B3) are generated from — and only if it becomes the single owner of exactly that fact class. Skip for now.

---

# Suggested order & parallel lanes

**Day 1 (all S tasks, ~2–3 hours total):** G2 → A1 → B4. Plus the G1 interim stopgap (Vercel protection) immediately.

**Week 1 — lane 1 (backend):** G1 → B1 → B2 · **lane 2 (content, parallel):** B3 drafting · **lane 3 (independent):** C2, A3, E4
**Week 2:** C3 (needs C2) · D1 → D3 · A2 · C1 (verify-first step early in the week) · G3
**Week 3:** E1 → E2 · F1 → F3 · C4 · B5
**Week 4+:** C5 · D2 (flag on after a week of logged routing) · F2 · E3

**Dependency map (only the real ones):**
```
B1 → B2 → (B3 usable) → B5
C2 → C3, C4, C5
D1 → D2, D3
F1 → F3;  F2 after B/C (scores meaningless before content exists)
A1 → A2
G1 gates: exposing any new endpoint publicly
```
Everything else is order-independent by design.

**The two moments Gandi feels the difference:**
1. End of Week 1 — packs live (B1–B3) + unthrottled config (A1): Hermes suddenly *knows the businesses* and finishes multi-app tasks.
2. End of Week 2 — leases searchable (C2–C3) + playbooks on scheduled runs (D1/D3): unattended runs start producing briefs worth reading.

---

# System-level acceptance checklist

Run after Week 3. Every line must pass.

| # | Check | Proves |
|---|-------|--------|
| 1 | *"Which GAK tenants are related-party?"* → correct, **zero tool calls** | B1–B3 packs live |
| 2 | *"Which leases end in 2026 and what should I do about them?"* → A Caring Living ×2, renewal framing | packs + watchlist content |
| 3 | Upload a lease PDF → chunks in DB → clause question answered with doc citation | C2 |
| 4 | Doc ingested via ingestion service is findable in chat | C1 |
| 5 | 3-app sweep (email + QBO + calendar) completes, `stop_reason=end_turn` | A1 |
| 6 | With iterations forced to 2, reply is a partial-results summary, not "(no response)" | A3 |
| 7 | Friday-brief scheduled run: high effort applied, playbook format followed, entities labeled separately | A2 + D1 + D3 |
| 8 | Unauthenticated `POST /api/chat` → 401; dashboard + crons still work | G1 |
| 9 | `restricted` upload rejected with policy message | G3 |
| 10 | `hermes_agent_runs` + `hermes_tool_calls` rows for every turn; weekly token rollup answers "what did this cost?" | F1 + F3 |
| 11 | Eval job produces per-business scores; sabotaged fact in dev shows as regression | F2 |
| 12 | Memory block > 2,000 chars renders untruncated | B4 |

---

# Appendix — env var quick sheet (new / changed)

```
# A1
HERMES_EFFORT=medium
HERMES_MAX_TOKENS=16000
HERMES_MAX_TOOL_ITERATIONS=32

# A2
HERMES_SCHEDULED_MODEL=            # empty = same as chat; set to an Opus-class id when approved
HERMES_SCHEDULED_EFFORT=high
HERMES_SCHEDULED_MAX_TOOL_ITERATIONS=48

# B4
MEMORY_BLOCK_MAX_CHARS=8000
MEMORY_SEMANTIC_TOP_K=8

# D2
PLAYBOOK_ROUTER_ENABLED=false
PLAYBOOK_ROUTER_MODEL=             # empty = HERMES_GUIDANCE_MODEL

# E4
TOOL_RESULT_MAX_CHARS=20000

# G1
HERMES_API_TOKEN=<generate: openssl rand -hex 32>
CORS_ORIGINS=https://<your-dashboard-origin>
```

*End of plan. Companion analysis: see the 2026-07-24 session review (six bottlenecks, ranked). Owner: Gandi Kediye. Line references valid as of the 2026-07-24 code snapshot.*




