# Hermes — Full Task Prompt Library

Ready-to-paste prompts for Claude Code, **one per actionable task** in `HERMES_UPGRADE_PLAN.md` (Workstreams A–G). Workstream H is parked ("do not build") — skipped by design.

**How to use:** open a fresh Claude Code session in `D:\Gandi AI Assistant`, paste one prompt block, review the diff, run its test, commit yourself. Each prompt is self-contained and starts by telling Claude to re-verify the anchors before editing.

Anchors were verified against the live code on 2026-07-24 (function names, line numbers, and cross-file wiring). Re-verify at implementation time — the code is truth.

**The five smallest (A1, B4, A3, E4, G2)** have full prompts in [`HERMES_DAY1_TASK_PROMPTS.md`](HERMES_DAY1_TASK_PROMPTS.md) — pointer stubs here to avoid duplication.

### Index & suggested order

| Order | Task | Effort | Depends on | Type |
|-------|------|--------|-----------|------|
| Day 1 | A1, B4, A3, E4, G2 | S | — | see Day-1 file |
| 1 | **G1** — API auth + real CORS | M | — | code + ops |
| 2 | **G3** — Restricted-data policy gate | S | — | code |
| 3 | **B1** — context_packs table + endpoints + loader | M | — | code |
| 4 | **B2** — Inject packs into system prompt | S | B1 | code |
| 5 | **B3** — Author the 7 packs | M | B1,B2 | content |
| 6 | **B5** — Extractor skips pack-known facts | S | B3 | code |
| 7 | **C2** — Wire PDF/image extraction into ingest | M | — | code |
| 8 | **C3** — Bulk-ingest leases | M | C2 | code+content |
| 9 | **A2** — Per-run-type model/effort overrides | M | A1 | code |
| 10 | **D1** — Playbook store + scheduler wiring | M | — | code |
| 11 | **D3** — Author the 5 playbooks | M | D1 | content |
| 12 | **C1** — Kill vector split-brain (pgvector) | L | — | code |
| 13 | **C4** — Retrieval quality (titled chunks, k) | S–M | C2 | code |
| 14 | **C5** — Structure-aware chunking | M | C2 | code |
| 15 | **E1** — Typed Gmail tools | M | — | code |
| 16 | **E2** — Typed QuickBooks tools | M | — | code |
| 17 | **E3** — Typed Calendar tools | S–M | — | code |
| 18 | **F1** — Persist agent runs + tool calls | M | — | code |
| 19 | **F3** — Token + cost telemetry | S | F1 | code |
| 20 | **F2** — Golden-question eval harness | M–L | F1, B/C | code |
| 21 | **D2** — Chat playbook auto-router | M | D1 | code |

---

# Workstream A — Unthrottle the agent

## A1 — Unthrottle config *(→ full prompt in Day-1 file)*
Env-only: `HERMES_EFFORT=medium`, `HERMES_MAX_TOKENS=16000`, `HERMES_MAX_TOOL_ITERATIONS=32`. Anchor: `hermes/config.py:71-74`.

## A2 — Per-run-type model / effort / iteration overrides

```
Task A2 from HERMES_UPGRADE_PLAN.md — give scheduled (unattended) runs a bigger budget than interactive chat. Depends on A1.

Verify first:
- hermes/config.py:71-74 — MODEL / EFFORT / MAX_TOKENS / MAX_TOOL_ITERATIONS globals.
- hermes/agent.py: _anthropic_turn is defined at line 293; it applies effort at line 317 via extra["output_config"] = {"effort": Config.EFFORT}. get_final_message is at ~332.
- hermes/agent.py: the tool loop is `for _ in range(Config.MAX_TOOL_ITERATIONS):` at line 458. Public entry points: stream_turn at line 404, run_turn at line 562.
- hermes/scheduler.py: run_task() at line 348 imports run_turn from .agent (line 358) and calls it at lines 367-373 with (session_key, [{"type":"text","text":run_prompt}], voice=False, active_business=business, allowed_apps=allowed_apps). No model/effort args are passed today.

Make these changes:
1. hermes/config.py — add near the chat globals:
     SCHEDULED_MODEL = os.getenv("HERMES_SCHEDULED_MODEL", "")            # empty = same as chat
     SCHEDULED_EFFORT = os.getenv("HERMES_SCHEDULED_EFFORT", "high")
     SCHEDULED_MAX_TOOL_ITERATIONS = int(os.getenv("HERMES_SCHEDULED_MAX_TOOL_ITERATIONS", "48"))
2. hermes/agent.py — add optional kwargs to BOTH stream_turn(...) and run_turn(...):
     model_id: str | None = None, effort: str | None = None, max_iterations: int | None = None
   Default None → current behavior (existing callers unchanged). Inside, resolve:
     eff = effort or Config.EFFORT
     iters = max_iterations if max_iterations is not None else Config.MAX_TOOL_ITERATIONS
     model = model_id or Config.MODEL   (respect the existing provider selection logic)
   Thread `eff` into _anthropic_turn (replace the direct Config.EFFORT read at line 317 with a param that defaults to Config.EFFORT), and use `iters` in the range() at line 458. run_turn should forward the same kwargs into whatever it delegates to.
3. hermes/scheduler.py — in run_task() (line 348), pass the scheduled overrides into run_turn:
     model_id=(Config.SCHEDULED_MODEL or None),
     effort=Config.SCHEDULED_EFFORT,
     max_iterations=Config.SCHEDULED_MAX_TOOL_ITERATIONS
4. .env.example — document HERMES_SCHEDULED_MODEL (empty=chat), HERMES_SCHEDULED_EFFORT (default high), HERMES_SCHEDULED_MAX_TOOL_ITERATIONS (default 48). Ship SCHEDULED_MODEL empty.

Definition of done: chat behavior byte-for-byte unchanged; a scheduled run applies the scheduled effort + iteration budget. 

Test: temporarily set HERMES_SCHEDULED_MAX_TOOL_ITERATIONS=2, "Run now" a read-only scheduled task, confirm it degrades (proves the override is live), restore.

Do NOT commit or push. Show me the diff.
```

## A3 — Graceful wrap-up when tool budget runs out *(→ full prompt in Day-1 file)*
Anchor: `hermes/agent.py` loop `:458`, `reply or "(no response)"` at `:554`.

---

# Workstream B — Context packs (highest-leverage)

## B1 — context_packs table + admin endpoints + cached loader

```
Task B1 from HERMES_UPGRADE_PLAN.md — a per-business "standing facts" pack injected into every turn.

Verify first:
- Migrations live in hermes/db/migrations/ as numbered NNNN_name.sql; the highest existing is 0015_jobs_allow_multiple.sql — so create 0016_context_packs.sql.
- hermes/db/connection.py:192 defines `@contextmanager def cursor(commit: bool = True)`. Usage pattern (from scheduled_tasks.py):
      with cursor() as cur:
          cur.execute("...", (args,))
          return cur.fetchone()
  Read-only calls pass cursor(commit=False).
- app.py uses plain FastAPI decorators: `app = FastAPI(...)` at line 142; routes like `@app.get("/api/health")` (326), `@app.post("/api/documents")` (798). Add new routes in that style.
- hermes/db/ existing modules: conversations.py, documents.py, memories.py, scheduled_tasks.py, jobs.py, settings.py — mirror their structure.

Make these changes:
1. hermes/db/migrations/0016_context_packs.sql:
     CREATE TABLE IF NOT EXISTS context_packs (
       pack_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
       slug       text UNIQUE NOT NULL,     -- 'global'|'afk'|'gak'|'gif'|'gakc'|'nh'|'per'
       content_md text NOT NULL DEFAULT '',
       version    int  NOT NULL DEFAULT 1,
       updated_at timestamptz NOT NULL DEFAULT now()
     );
   Confirm how migrations are applied in this repo (look for a runner / where 0015 gets executed) and follow it.
2. hermes/db/context_packs.py — get_pack(slug) -> dict|None, upsert_pack(slug, content_md) -> dict (bump version on update via INSERT ... ON CONFLICT (slug) DO UPDATE SET content_md=..., version=version+1, updated_at=now()). Use the cursor() pattern.
3. hermes/context_packs.py — packs_for(active_business: str | None) -> str: returns the 'global' pack + (if active_business set and not "all") that business's pack, concatenated with clear headers. In-process TTL cache (5 min) keyed by slug so it's ~0 cost per turn. FAIL OPEN: any DB error returns "" — a pack failure must never break a turn.
4. app.py — GET /api/context-packs/{slug} and PUT /api/context-packs/{slug} (body {content_md}). Business slugs only (afk/gak/gif/gakc/nh/per + global) — validate against that set. (Leave auth to G1; add a TODO comment.)

Definition of done: PUT then GET round-trips a pack and bumps version; packs_for('gak') returns global+gak concatenated; a cold/broken DB returns "" with no exception.

Test: curl -X PUT .../api/context-packs/gak -d '{"content_md":"# TEST"}' then GET it back.

Do NOT commit or push. Show me the diff and the migration file.
```

## B2 — Inject packs into the system prompt

```
Task B2 from HERMES_UPGRADE_PLAN.md. Depends on B1 (needs hermes/context_packs.packs_for).

Verify first:
- hermes/agent.py: _system_prompt(...) at line 77, signature (voice=False, active_business=None, memory_block="", attachments=None). Assembly order (77-147): identity+catalog+guidelines → business-scope block (132-143) → MEMORY block if memory_block (144-145) → attachments (146) → voice style (147).
- build_memory_block is called at lines 441-443 inside stream_turn, wrapped in try/except → "" on failure. _system_prompt(...) is then invoked per tool-iteration at line 459.

Make these changes:
1. hermes/agent.py — add param `context_pack: str = ""` to _system_prompt(). Insert it AFTER the business-scope block and BEFORE the MEMORY block, formatted:
     "BUSINESS CONTEXT PACK (authoritative standing facts and rules — trust these over memory or retrieval; if a tool result contradicts them, say so explicitly):\n<pack content>"
   Only render the header if context_pack is non-empty.
2. In stream_turn, next to the build_memory_block call (441-443), fetch once per turn:
     try: context_pack = packs_for(active_business)
     except Exception: context_pack = ""
   and pass context_pack into the _system_prompt(...) call at line 459.
3. Size guard: if len(global+business packs) > 40000 chars, log a warning and truncate the BUSINESS pack tail only (never the global pack) with a "[pack truncated]" marker. (Simplest: do this inside packs_for, or right after fetch.)

Definition of done: every locked-business turn carries global+business pack; "all businesses" mode carries global only; empty pack renders nothing.

Test: PUT a gak pack containing "The test sentinel word is QUARTZ", then ask (locked to gak) "what is the test sentinel word?" → answers QUARTZ with zero tool calls.

Do NOT commit or push. Show me the diff.
```

## B3 — Author the seven packs *(content task, no code)*

```
Task B3 from HERMES_UPGRADE_PLAN.md — write the 7 context packs and store them via PUT /api/context-packs/{slug}. Requires B1 (store) + B2 (inject) live.

This is a CONTENT task. Draft each pack from Gandi's canonical sources (his CLAUDE.md governance file and gandi_hub.db exports) — ask me for those if you don't have them; do NOT invent facts. Stamp EVERY pack with this first line:
    <!-- DERIVED from canonical sources, generated 2026-07-24 — do not hand-edit facts here; regenerate -->

Write these 7 (structure + word budget per HERMES_UPGRADE_PLAN.md §B3):
- global (~600-900w): who Gandi is (voice-dictation user — interpret meaning, never correct grammar); the six businesses table; HARD RULES: strict entity separation, related-party map (1st Attentive↔Samsam at 3 GAK sites; NH↔Gandi at 720 E Lake — tag in any AR/rent output), confirm-before-write, email-only tenant outreach, reply format (short answer first; label: value lines), halal/prayer-time scheduling, Kenya Window 6-9 AM CST for AFK.
- gak (~400-700w): 7 properties; per-property tenant roster + lease end dates; watchlist (A Caring Living 6733+7601 both end 09/30/2026 — renew NOW; 2027 cluster; Caremark+Khadijah lease bodies wrongly reference 1229 — fix at renewal); COI-per-tenant; MN property tax May 15 / Oct 15.
- gif (~400-700w): 3 properties; 1229 E Lake roster incl. Clear Channel billboard (auto-renewing year-to-year since 03/2021; income booked as separate signage line); 2609 15th Ave S VACANT (leasing priority); 1230 Logan (Love & Care).
- afk (~300-500w): 128-acre Loitokitok farm; ~54 staff; Abdirahman Abdiaziz (Manager & Finance), Mohamed (Logistics/Security); reporting 9/12/4 EAT; GLOBALG.A.P. prep; KES; Kenya Window.
- gakc (~150w): Kenya RE entity; distinct from Kediye Holdings; minimal facts.
- nh (~300w): pre-formation; women-only hammam at 720 E Lake west wing (related-party lease with GAK, arm's-length, documented both sides, target ~June 2026); buildout compliance; service details confidential.
- per (~200-400w): father Yusuf (97, daily care); prayer-time/halal scheduling defaults; personal finance separate from every entity.

NEVER put in packs: SSNs/EINs, bank account numbers, API keys, or any Comfort Services patient/PHI data.

Deliver: write all 7 to local files under a scratch folder for my review FIRST, then (after I approve) PUT each to /api/context-packs/{slug}.

Test after storing: three zero-tool-call questions answer correctly — "Which GAK tenants are related-party?", "Which leases expire in 2026?", "What's the standing rule for contacting tenants?"
```

## B4 — Raise memory-injection ceiling *(→ full prompt in Day-1 file)*
Anchors: `hermes/memory_retrieve.py:27` and `:118-119`; `hermes/config.py:237`.

## B5 — Stop the extractor re-learning pack facts

```
Task B5 from HERMES_UPGRADE_PLAN.md. Depends on B3 (packs must have content). Small.

Verify first:
- hermes/memory_extract.py: _build_system(entity_slug, entities, projects) at line 95. The extractor runs via extract_async (line 110) → call_claude_json at 144-146 on Config.GUIDANCE_MODEL (Haiku tier).
- hermes/context_packs.py exists after B1 with packs_for(active_business).

Make this change:
1. In _build_system(), append a short section built from the relevant packs (fetch via packs_for(entity_slug or business); fail-open to ""):
     "ALREADY KNOWN (do not extract duplicates of these):\n" + first ~1500 chars of the business pack + the global pack's section headings.
   Keep everything else unchanged — MEMORY_DUP_THRESHOLD (0.92) still dedupes similar memories; this just stops exact-known facts at the source.
   Note the entity/business slug bridge: packs key off BUSINESS slugs (afk/gak...), while extract works in entities slugs — reuse whatever mapping the file already uses (hermes.businesses.to_entity_slug in reverse, or pass the business slug through).

Definition of done: a turn that restates a pack fact yields {"memories": []}; a genuinely new fact still produces one write.

Test: tell Hermes "remember: 2609 15th Ave S is vacant" (already in gif pack) → no write logged; tell it a new fact → one write.

Do NOT commit or push. Show me the diff.
```

---

# Workstream C — RAG repair

## C2 — Wire PDF/image extraction into the main ingest path

```
Task C2 from HERMES_UPGRADE_PLAN.md. GOOD NEWS: the extraction ladder ALREADY EXISTS — this task is mostly wiring, not new OCR code.

Verify first:
- hermes/ingest.py: ingest_document(entity_id, *, filename, mime="", raw=None, text=None, ...) at line 120. It calls _extract_text(raw, text, mime) at line 155. _extract_text (line 45) returns text for text-like MIME but returns None for PDFs/images (see comment "wire pdf-co/pypdf later"). So binaries get a documents row but ZERO chunks. Chunking happens in _reindex (line 64) → chunk_text (line 24) → docs_repo.add_chunks → vector_store.upsert_chunks (line 113).
- hermes/extract.py ALREADY has extract_text(filename, raw, mime) at line 46, which routes PDFs/images through _extract_pdf_or_image (line 194): tries NuExtract (_extract_via_nuextract, 216) then falls back to Claude vision (_extract_via_claude, 281, using Config.OCR_FALLBACK_MODEL). It raises ExtractionError on failure. It rejects HEIC/HEIF (line 64).

Make these changes:
1. In hermes/ingest.py, when _extract_text returns None AND raw bytes are present AND mime is a PDF or image (or filename extension indicates one), call hermes.extract.extract_text(filename, raw, mime) to get the text, then feed it through the SAME chunk/embed path (_reindex). Wrap in try/except ExtractionError: on failure, keep the current behavior (documents row, no chunks) and log a warning — never fail the whole upload.
2. Preserve page structure if extract.py exposes it; if it only returns flat text, that's fine for now (C5 handles structure-aware chunking later).
3. Sensitivity: extracted chunks must inherit the parent document's sensitivity_level unchanged (already threaded through ingest_document).
4. Backfill script scripts/backfill_extract.py: find documents rows with zero document_chunks, pull bytes from S3 (documents.s3_uri), run the new extraction, chunk, embed, insert. Add a --limit flag; log per-doc success/failure. Reuse ingest.py helpers rather than duplicating.

Definition of done: uploading a scanned lease PDF via POST /api/documents produces document_chunks rows with embeddings; backfill fills historical gaps.

Test: upload one real lease PDF to business gak → SELECT count(*) FROM document_chunks WHERE document_id = '<id>' > 0 → ask "what is the lease end date in <tenant> lease?" → correct, cites the doc.

Do NOT commit or push. Show me the diff and the backfill script.
```

## C3 — Bulk-ingest the documents that matter (leases first)

```
Task C3 from HERMES_UPGRADE_PLAN.md. Depends on C2 (extraction must work first).

Verify first:
- The upload endpoint is POST /api/documents (app.py line 798, upload_documents). It reads a multipart form; sensitivity_level defaults to "internal" (line 817); it passes doc_type/sensitivity to ingest_document.
- Confirm the exact form fields the endpoint expects (file field name, business/entity field, sensitivity_level, doc_type) before scripting against it.

Make this change:
1. scripts/bulk_ingest.py — walk a local folder tree and POST each file to /api/documents. CLI:
     python scripts/bulk_ingest.py --business gak --folder ./staging/gak-leases --sensitivity internal --doc-type lease [--api-base URL] [--token ...]
2. Idempotency: dedupe by SHA-256 of file bytes via a small JSON manifest (path + hash + document_id + timestamp); skip already-ingested files on re-run.
3. Defaults: --sensitivity internal; allow confidential for personal-data docs.
4. Respect G1 auth if it's live — accept a --token and send Authorization: Bearer.
5. Print a summary: N ingested, N skipped (dupes), N failed, with per-file status.

Do NOT bulk-ingest anything with bank account numbers/SSNs/EINs without review, and nothing from Comfort Services.

Ingest priority order (document in the script's --help): (1) 17 lease PDFs, (2) insurance COIs, (3) Clear Channel billboard agreement, (4) key vendor contracts, (5) entity formation docs.

Definition of done: running it against a folder indexes each file once; manifest records hashes; re-run ingests nothing new.

Test: "Compare the end dates of both A Caring Living leases" (business gak) → correct dates, two document citations, no hallucinated terms.

Do NOT commit or push. Show me the script and a dry-run output.
```

## C4 — Retrieval quality: titled chunks + bigger k

```
Task C4 from HERMES_UPGRADE_PLAN.md. Best after C2 (real docs exist).

Verify first:
- Chunks are bare text slices (hermes/ingest.py chunk_text at line 24, size 1000 / overlap 150), embedded via _reindex (line 64) → vector_store.upsert_chunks (line 96).
- vector_store.query default top_k=8 (line 133). search.py semantic_search default top_k=8 (line 21).
- BUT the tool handler _search_business_knowledge (tools.py line 1222) calls semantic_search(..., top_k=6) and trims snippet to 500 chars (line ~1254). Result fields returned to the model: title, source_system, snippet, score.

Make these changes:
1. At embed time (in _reindex, before chunking or per chunk), prefix every chunk with a header built from its document row:
     "Document: <title> (<doc_type>, <business>) — part <i>/<n>\n\n<chunk text>"
   Put the header into BOTH the embedded text AND the text_preview column so relevance and displayed snippets both improve.
2. Raise the search_business_knowledge top_k from 6 to 12 in tools.py:1248 (keep semantic_search's own default at 8 for other callers).
3. Ensure the tool result includes document title (and page when available from C2) so the agent can cite.
4. Re-embedding existing chunks is required to benefit — fold this into the C2 backfill run (one pass, both fixes). Note that in the prompt output.

Definition of done: search results show titled, cite-able chunks; exact-name queries ("Caremark") reliably surface the right document.

Test: ask for a specific clause by tenant name; the returned snippet header names the correct lease.

(Stretch, SEPARATE PR — do not do now: hybrid tsvector+GIN full-text search merged with vector via reciprocal-rank fusion. Only after measuring pure-vector misses on clause-number/dollar queries.)

Do NOT commit or push. Show me the diff.
```

## C5 — Structure-aware chunking

```
Task C5 from HERMES_UPGRADE_PLAN.md. Depends on C2.

Verify first:
- hermes/ingest.py chunk_text(text, size=1000, overlap=150) at line 24 — fixed character slicing with a whitespace-boundary backup (rfind space). This cuts lease clauses mid-sentence.
- ingestion-service/ingestion/chunker.py (line 11) mirrors the same 1000/150 logic — update it too IF the ingestion service survives C1; otherwise leave a note.

Make this change:
1. Replace chunk_text with a two-level splitter, keeping the SAME signature so callers don't change:
   a. Split on structural boundaries first: page breaks (if C2 provides them), then blank-line paragraphs / numbered-clause starts matching ^\s*\d+(\.\d+)*\s.
   b. Greedily pack paragraphs into chunks of ~400-800 tokens (~1600-3200 chars) with one-paragraph overlap.
2. When detectable, carry page_start/page_end (or clause label) into chunk metadata for citations — thread it through add_chunks if the schema has a column; if not, note the schema gap rather than adding a migration in this task.

Definition of done: chunks start at clause/paragraph boundaries; the average chunk carries a whole provision; a rent-schedule table lands in one chunk.

Test: inspect 10 random chunks from a re-ingested lease — no mid-sentence starts.

Do NOT commit or push. Show me the diff and the 10-chunk sample.
```

## C1 — Kill the vector split-brain: consolidate on pgvector

```
Task C1 from HERMES_UPGRADE_PLAN.md. This is the biggest RAG task (L). VERIFY the map below before writing code — it was gathered from a prior scan but re-confirm.

Established facts (re-verify):
- ingestion-service writes to the SAME AWS RDS Postgres and the SAME documents/document_chunks tables as the main app (ingestion/db.py uses identical env keys; ingestion/repo.py create_document + add_chunks write those tables). It ALSO owns three ledger tables (source_items, business_sync_state, ingestion_runs) — leave those alone.
- The main app has ALREADY cut over to pgvector: migration 0008_pgvector.sql added embedding vector(1536), embed_model, text_preview to document_chunks + an HNSW cosine index; reads go through hermes/vector_store.py query() (guarded by sensitivity + namespace in SQL).
- The ingestion service still writes Pinecone: ingestion/pinecone_store.py upsert_chunks (line 78) is called from ingestion/indexer.py:200, and delete_ids from indexer.py:269. Critically, ingestion/repo.py add_chunks (line 178) writes pinecone_vector_id but NEVER populates the pgvector `embedding` column. So its chunks are invisible to search_business_knowledge.
- ingestion-service is a standalone deploy with an active Vercel cron (GET /api/cron/sync-all every 3 min).

Decision: consolidate on pgvector.

Make these changes:
1. In ingestion/indexer.py, replace the Pinecone write path (ps.upsert_chunks at 200) with a pgvector embed+update that mirrors hermes/vector_store.py upsert_chunks (line 96): embed the chunk texts via OpenAI, then UPDATE document_chunks SET embedding=..., embed_model=..., text_preview=... WHERE pinecone_vector_id=<id>. Reuse the exact SQL from vector_store.py. (The ingestion db.py has no vector registration — either add register_vector like hermes/db/connection.py:108-116, or write the vector as a string cast, matching how vector_store.py does it.)
2. Replace the delete path (ps.delete_ids at 269) with a DELETE / is_active=false on document_chunks by document_id — check how the main app deletes on re-index (ingest.py:213) and match it.
3. Quarantine ingestion/pinecone_store.py usage; update the ingestion-service README (it documents a dead main-app Pinecone read path).
4. Decommission the Pinecone index ONLY after the acceptance test passes.

Definition of done: a document ingested through the ingestion service is retrievable via the agent's search_business_knowledge tool.

Test: ingest a marker file ("The C1 sentinel is BASALT") through the ingestion service into business gak; in chat (locked to gak) ask "search business knowledge for the C1 sentinel" → BASALT with the doc reference.

Do NOT commit, push, or delete the Pinecone index. Show me the diff and a manual test result first.
```

---

# Workstream D — Playbooks

## D1 — Playbook store + scheduled-task wiring (explicit selection only)

```
Task D1 from HERMES_UPGRADE_PLAN.md.

Verify first:
- Migrations: numbered NNNN_name.sql in hermes/db/migrations/; highest is 0015. ALTER TABLE ... ADD COLUMN IF NOT EXISTS pattern is used in 0013/0015.
- scheduled_tasks columns are defined in hermes/db/scheduled_tasks.py (_TASK_COLS ~line 29). The scheduler builds each run's prompt in build_run_prompt (hermes/scheduler.py line 329), which already prepends the autonomy guardrail; run_task (line 348) calls run_turn.
- app.py route style (@app.get/@app.post) and cursor() pattern per B1.

Make these changes:
1. Migration 0016_playbooks.sql (or next free number — check what B1 used and increment):
     CREATE TABLE IF NOT EXISTS playbooks (
       playbook_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
       slug text UNIQUE NOT NULL, name text NOT NULL,
       description text NOT NULL DEFAULT '', content_md text NOT NULL,
       enabled boolean NOT NULL DEFAULT true, updated_at timestamptz NOT NULL DEFAULT now()
     );
     ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS playbook_slug text;
2. hermes/db/playbooks.py — list_playbooks(), get_playbook(slug), upsert_playbook(...). cursor() pattern.
3. app.py — GET /api/playbooks, GET/PUT /api/playbooks/{slug}. (Auth TODO for G1.)
4. hermes/db/scheduled_tasks.py — add playbook_slug to _TASK_COLS + the row->dict mapper so it round-trips.
5. hermes/scheduler.py build_run_prompt (line 329) — when the task has playbook_slug, fetch the playbook and PREPEND to the run prompt:
     "PLAYBOOK — follow this procedure exactly; deviate only if a step is impossible, and say so in the report:\n<content_md>\n\n"
   Fail-open: missing/disabled playbook → run without it, log a warning.
6. v1 is EXPLICIT selection only — no classifier. (Auto-routing is D2.)

Definition of done: a scheduled task with playbook_slug set runs with the procedure in-prompt; the report follows the playbook's output format.

Test: bind a test task to a playbook whose output ends with a fixed marker line; Run now; the marker appears in the run report.

Do NOT commit or push. Show me the migration + diff.
```

## D3 — Author the first five playbooks *(content task, no code)*

```
Task D3 from HERMES_UPGRADE_PLAN.md — write 5 playbooks and store via PUT /api/playbooks/{slug}. Requires D1.

CONTENT task. Each playbook: Purpose · Inputs · Steps (numbered, tool-by-tool) · Output format · Guardrails. 300-600 words each. Draft to local files for my review FIRST, then PUT after I approve.

1. arrears-chase (gak or gif): AR aging from QuickBooks → match invoices to tenants → cross-check Gmail tenant-comms lane for payment promises/disputes (last 30 days) → rank by amount×age → table (tenant|property|amount|days late|last contact|action) → TAG related-party tenants (1st Attentive, NH) → draft (never send) reminders for top 3. Autonomy: draft ceiling.
2. friday-brief (all businesses): per entity — cash/AR headline, urgent unread email count + top 3 subjects, next-7-days calendar, lease watchlist inside 90 days, open scheduled-task failures → one page, entity-labeled sections, NO blended numbers → end with "Decisions needed".
3. lease-renewal-check (gak+gif): from the context-pack watchlist, leases ending ≤180 days → per lease search business knowledge for renewal notice period + escalation clause, check Gmail for renewal threads → output: end date|notice deadline|current rent|status|next action. First targets: A Caring Living ×2 (09/30/2026).
4. month-close-prep (one business/run): QuickBooks uncategorized txns, unmatched deposits, open invoices past terms, bank-feed vs ledger deltas → close checklist with per-item status; NEVER write to QuickBooks; flag related-party items for memo documentation.
5. kenya-window-brief (afk): overnight AFK-lane email + form submissions → staffing/security flags → weather + market context (web search, cite) → 5-bullet brief before 6 AM CST; timezone-explicit (EAT vs CST).

Definition of done: 5 playbooks stored; each bound to at least one scheduled task (friday-brief→Fri AM; kenya-window-brief→daily early; arrears-chase→Mon weekly; month-close-prep→monthly; lease-renewal-check→weekly until 09/2026).

Test: Run now each once — every report follows its output format and stays inside its autonomy ceiling.
```

## D2 — Chat auto-router for playbooks (flag off by default)

```
Task D2 from HERMES_UPGRADE_PLAN.md. Depends on D1. Ship the flag OFF.

Verify first:
- The cheap-classifier pattern already exists: hermes/memory_extract.py calls call_claude_json (from hermes/llm_json.py) on Config.GUIDANCE_MODEL (Haiku, config.py:82). Reuse it.
- hermes/agent.py stream_turn is at line 404; playbooks are injected the same way D1 does for scheduled runs (prepend to prompt / system context).
- hermes/db/playbooks.py list_playbooks() gives slug + description.

Make these changes:
1. hermes/playbook_router.py — route(user_message) -> str|None: call_claude_json on PLAYBOOK_ROUTER_MODEL with input = user message + playbook catalog (slug + one-line description); output {"playbook": "<slug>"|null}. Prompt: "Pick a playbook ONLY when the request clearly matches its description; when in doubt return null." Fail-open: any error → None.
2. hermes/config.py — PLAYBOOK_ROUTER_ENABLED (default false), PLAYBOOK_ROUTER_MODEL (default = GUIDANCE_MODEL). Document in .env.example.
3. hermes/agent.py stream_turn — when enabled and route() returns a slug, inject that playbook the same way D1 does. Log every routing decision (chosen slug or null).

Definition of done: with the flag ON, "who owes me money" pulls arrears-chase; "what's the weather" pulls nothing. Flag ships OFF.

Test: 10 canned utterances (5 route, 5 don't) — ≥9/10 correct before enabling. Log a week before default-on.

Do NOT commit or push. Show me the diff.
```

---

# Workstream E — Typed tools

**Shared pattern (read before E1-E3):** in hermes/tools.py — add a tool def in build_tools() (line 41), a dispatch() branch (line 2011), and a _TOOL_STATUS label (line 2177). Follow the existing dedicated tools (calendar_list_upcoming, send_email, zoom_send_invite). Every new tool takes optional `business` (used only in "all" mode; resolve the per-business Maton key the way existing tools do). Return COMPACT model-friendly JSON, not raw API responses. maton_request stays as the escape hatch.

## E1 — Typed Gmail tools

```
Task E1 from HERMES_UPGRADE_PLAN.md. Follow the shared typed-tool pattern in hermes/tools.py (build_tools:41, dispatch:2011, _TOOL_STATUS:2177; mimic send_email / calendar_list_upcoming).

Verify first: how existing Gmail work is done today via maton_request (grep the system prompt in hermes/agent.py and any Gmail helpers), and how per-business Maton keys resolve in dispatch (_resolve_client).

Add two tools:
  gmail_search    {query: str, newer_than_days?: int = 30, max_results?: int = 20, business?: str}
  gmail_read_thread {thread_id: str, business?: str}
- gmail_search: build the Gmail query internally (append newer_than:{n}d), return [{thread_id, from, subject, date, snippet, unread}].
- gmail_read_thread: return the thread's messages with plain-text bodies (strip HTML), each truncated to ~4000 chars.
- Update the email-reads guidance block in hermes/agent.py _system_prompt to steer reads to these tools, keeping maton_request for labels/drafts.

Definition of done / test: "any unpaid-rent emails this month?" (business gif) completes using gmail_search + gmail_read_thread in ≤4 tool calls.

Do NOT commit or push. Show me the diff.
```

## E2 — Typed QuickBooks tools

```
Task E2 from HERMES_UPGRADE_PLAN.md. Shared typed-tool pattern in hermes/tools.py.

Add three tools, wrapping the QuickBooks report/query endpoints via Maton; return FLATTENED rows, not the raw QBO report tree (it's huge and mostly noise):
  qbo_ar_aging      {business: str, as_of?: str}                      -> rows [{customer, amount, bucket}]
  qbo_profit_loss   {business: str, period_start: str, period_end: str} -> flattened P&L lines
  qbo_list_invoices {business: str, status?: "open"|"overdue"|"paid", customer_name?: str, max_results?: int = 50}

Verify first: how QBO is reached today via maton_request (app id, report paths) so the wrappers call the right endpoints.

These three cover arrears-chase, friday-brief, and month-close-prep end to end.

Definition of done / test: an arrears-chase playbook run makes ZERO maton_request calls for its QBO reads.

Do NOT commit or push. Show me the diff.
```

## E3 — Typed Calendar write + availability

```
Task E3 from HERMES_UPGRADE_PLAN.md. Shared typed-tool pattern in hermes/tools.py. There is already a calendar_list_upcoming tool — mirror its per-business key resolution (hermes/calendar.py).

Add:
  calendar_create_event {title: str, start_iso: str, end_iso: str, attendees?: [str], description?: str, business?: str}
  calendar_free_slots   {date_from: str, date_to: str, duration_minutes: int, business?: str}
- calendar_create_event is a WRITE → it MUST stay behind the confirm-before-write prompt rule and the scheduler autonomy ceiling. No silent invites. Verify the confirm-before-write rule text in _system_prompt and make sure this tool is covered by it.

Definition of done / test: "find me a 30-minute slot Tuesday and book it" → a confirm step, then one clean create call.

Do NOT commit or push. Show me the diff.
```

## E4 — Tool-result size guard *(→ full prompt in Day-1 file)*
Anchor: `hermes/tools.py` dispatch success return `:2168`.

---

# Workstream F — Observability + evals

## F1 — Persist agent runs and tool calls

```
Task F1 from HERMES_UPGRADE_PLAN.md.

Verify first:
- The tables hermes_agent_runs, hermes_tool_calls (and ai_runs, audit_logs) are defined in hermes/db/migrations/0001_init.sql but NO code writes them. READ their actual columns first; only add a small migration if required fields (effort, iterations_used, input_tokens, output_tokens, duration_ms) are missing.
- Post-turn hook: app.py calls store.record_turn at line 555 (POST /api/chat) and line 677 (streaming). This is where to insert run logging.
- The agent already builds a full tool_trace in memory each turn (hermes/agent.py, the trace_entry list) and returns it in the "done" event as tool_calls.
- Scheduled runs go through hermes/scheduler.py execute_and_record (line 380) → repo.finish_run (line 427) writing scheduled_task_runs. Add the SAME hermes_agent_runs/hermes_tool_calls rows there too.

Make these changes:
1. hermes/db/runs.py — insert_run(...) writing one hermes_agent_runs row (session_id, business slug, model_id, effort, stop_reason, iterations_used, duration_ms, token usage [F3]), and insert_tool_calls(run_id, trace) writing one hermes_tool_calls row per trace entry (tool, ok, label, input JSON with obvious secrets redacted). cursor() pattern.
2. Call these after record_turn at app.py:555 and :677, and in the scheduler path.
3. FAIL-OPEN: persistence errors log a warning, never break the reply (match hermes/db/conversations.py style).

Definition of done: every chat and scheduled turn leaves a run row + tool rows.

Test: run 3 turns; SELECT count(*) FROM hermes_agent_runs increments by 3; tool rows match each reply's tool_calls array.

Do NOT commit or push. Show me the diff + the two tables' actual columns.
```

## F3 — Token + cost telemetry per run

```
Task F3 from HERMES_UPGRADE_PLAN.md. Depends on F1.

Verify first: hermes/agent.py _anthropic_turn (line 293) gets the final message via get_final_message() around line 332; resp.usage carries input/output token counts. The loop runs multiple iterations per turn.

Make these changes:
1. In _anthropic_turn, read resp.usage and surface per-iteration input/output tokens; accumulate across the turn's iterations and include totals in the _final/done event.
2. Store input_tokens, output_tokens on the F1 hermes_agent_runs row (add columns via a small migration if absent).
3. Add GET /api/usage/summary?days=7 → tokens by day and by run type (chat vs scheduled), reading hermes_agent_runs.

Definition of done / test: after A1/A2, "what did the effort increase cost this week?" is answerable from /api/usage/summary instead of guessing.

Do NOT commit or push. Show me the diff.
```

## F2 — Golden-question eval harness (weekly, automated)

```
Task F2 from HERMES_UPGRADE_PLAN.md. Do this AFTER context packs (B) and RAG (C) land — scores are meaningless before the content exists. F1 helpful.

Verify first: the pull-based Jobs registry — hermes/job_runner.py REGISTRY dict at line 33 ({"google_reviews_scraper": reviews_scraper.run}); each job is run(config, cancel_check) -> dict; run-due endpoint in hermes/jobs_api.py:121 guarded by SCHEDULER_SECRET/CRON_SECRET. run_turn in hermes/agent.py:562 for fresh (no-session) calls. call_claude_json in hermes/llm_json.py for the Haiku judge.

Make these changes:
1. Migration: eval_questions (question_id, business_slug, question, must_contain text[], must_not_contain text[], notes, enabled) and eval_runs (run_id, question_id, score, answer_excerpt, tokens, created_at).
2. Seed 15-20 per major business, e.g. gak: "Which tenants are related-party?" must_contain ["1st Attentive"]; "When do the A Caring Living leases end?" must_contain ["2026"]; gif: "Which property is vacant?" must_contain ["2609"]; "Who is the billboard tenant at 1229?" must_contain ["Clear Channel"].
3. scripts/run_evals.py — for each enabled question: run_turn() fresh (no session reuse); grade (a) mechanical must_contain/must_not_contain, (b) a Haiku judge (call_claude_json) scoring 0-2 for correctness/citation. Write eval_runs rows.
4. Register as a Job (add run(config, cancel_check) to a new module + REGISTRY entry in job_runner.py:33 + a seed row). Weekly.
5. Output one summary line per business (avg score, regressions vs last week) in the Jobs tab run history.

Definition of done: weekly scored report; sabotaging a pack fact in dev shows up as a regression.

Test: run once manually; a known-answer question passes, a nonsense question fails.

Do NOT commit or push. Show me the migration, script, and job registration.
```

---

# Workstream G — Security hardening

## G1 — Auth on the main API + real CORS

```
Task G1 from HERMES_UPGRADE_PLAN.md — the single most urgent item. Currently there is NO auth on the main API and CORS defaults to "*".

Verify first:
- app.py: FastAPI app at line 142. CORS configured at lines 156-165 using Config.cors_origins() (config.py:200-202, default "*"). Routes are plain decorators. GET /api/health at 326. POST /api/chat at 479, /api/chat/stream at 566. Cron /api/scheduled-tasks/run-due at 1905 (guarded by SCHEDULER_SECRET/CRON_SECRET, lines 1916-1922); /api/jobs/run-due in hermes/jobs_api.py:121 (same guard).
- There is a session-gate middleware allowlist at app.py:195 (_AUTH_SELF_SECRET) — understand it before layering token auth so you don't double-gate the crons.

Make these changes:
1. hermes/config.py — add HERMES_API_TOKEN = os.getenv("HERMES_API_TOKEN", "").strip().
2. app.py — a FastAPI dependency (or middleware) requiring Authorization: Bearer <HERMES_API_TOKEN> on ALL /api/* routes, with THREE exemptions: GET /api/health, /api/scheduled-tasks/run-due, /api/jobs/run-due (those keep their existing SCHEDULER_SECRET/CRON_SECRET mechanism — do not break it). If HERMES_API_TOKEN is unset, log a loud warning and (choose one, ask me) either fail-closed or allow — default to allowing in dev so local isn't bricked, but make prod behavior explicit.
3. Frontend (static/): on first load, prompt for the token, store in localStorage, and send Authorization: Bearer on every fetch. Find where the frontend issues fetches (grep static/ for /api/) and add the header centrally.
4. CORS: set allow_origins to the exact dashboard origin(s) from CORS_ORIGINS; remove the "*" fallback for prod (keep "*" only when CORS_ORIGINS is unset in dev). Document CORS_ORIGINS in .env.example.

Definition of done: unauthenticated POST /api/chat → 401; dashboard works with the token; cron endpoints still fire.

Test: curl without token → 401; with token → 200; a Vercel cron run still appears in run history.

Tell me the interim same-day stopgap I should enable NOW (Vercel Deployment Protection) before this ships. Do NOT commit or push. Show me the diff.
```

## G2 — Secrets hygiene sweep *(→ full prompt in Day-1 file)*
Audit-only checklist across git history, working tree, and cron-secret config.

## G3 — Keep MOST-SENSITIVE data out of Hermes (policy gate)

```
Task G3 from HERMES_UPGRADE_PLAN.md.

Verify first:
- Ingest endpoint: app.py POST /api/documents at line 798 (upload_documents); sensitivity_level read at line 817 (default "internal"), passed to ingest_document at ~843.
- The memory pipeline already blocks restricted retrieval: Config.MEMORY_MAX_SENSITIVITY = "confidential" (config.py:238). Do NOT loosen it.
- Comfort Services is intentionally NOT in the business list (config.py:50-57: afk/gak/gif/gakc/nh/per). Keep it that way.

Make these changes:
1. Ingest-time guard in upload_documents: reject uploads flagged sensitivity_level == "restricted" unless the request carries an explicit allow_restricted=true form field (which the UI does NOT expose). Return 400 with: "Restricted-tier documents are not stored in Hermes by policy."
2. Add a POLICY section to README.md: no Comfort Services/CS healthcare records, no immigration documents, no attorney work product, no credentials — in any Hermes store (Postgres, S3, vectors, memories).
3. Do NOT add CS as a business/Maton key.

Definition of done / test: a restricted upload without the flag → 400 with the policy message; README carries the policy.

Do NOT commit or push. Show me the diff.
```

---

# Workstream H — Parked (do NOT build)

Per the plan, these are explicitly out of scope now — no prompts:
- **H1 Semantic cache** — one user, novel time-sensitive queries; cache hits rare, stale money/tenant answers are worse than slow ones.
- **H2 MCP adoption** — Maton works; MCP is a meaningful refactor, not a quick win. Park until Maton itself is a problem.
- **H3 Subagents / planner-executor** — A1/A2 + playbooks get 80% of the benefit at 5% of the complexity.
- **H4 Obsidian** — a curation UI, not an engine; adds a second memory home and drift risk. Possible future editing surface for the canonical files that packs (B3) derive from — not now.

---

## Ground rules baked into every prompt above
- **Verify-first**: each prompt re-checks the plan's claims against live code before editing.
- **No commit/push**: you review each diff and commit the batch yourself.
- **Fail-open**: packs, playbook fetch, run-logging, and routers must never break a live turn.
- **Ops steps are flagged as yours**: Vercel env/redeploy (A1, A2, G1), secret rotation (G2), AWS/Vercel dashboards (G2), Pinecone decommission (C1).
