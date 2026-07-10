# Hermes — Data Architecture Report
### Pinecone + AWS RDS (Postgres) + S3 — what's stored, and how the code talks to it

> Generated from a code-level analysis of the `history-convo` branch.
> Sources: `hermes/db/*`, `hermes/pinecone_store.py`, `hermes/ingest.py`,
> `hermes/search.py`, `hermes/storage.py`, `hermes/memory.py`, `app.py`,
> the three migration files, and the two `scripts/init_*.py` setup scripts.

---

## 1. The big picture — three stores, one rule

Hermes deliberately splits storage across **three** systems, each with a single job. The
governing rule is **tenant isolation by `entity_id`** (one of 8 businesses) plus a
**4-level sensitivity ladder** that travels with every row/vector.

| Store | Role | What it holds | Source of truth? |
|-------|------|---------------|------------------|
| **AWS RDS (Postgres 17.7)** | The record keeper | Users, businesses, access rules, chat history, document + chunk metadata, and ~16 scaffolding tables | ✅ **Yes** — the truth |
| **Pinecone** (serverless) | The search brain | A *search copy* of chunk text as 1536-dim vectors, with isolation metadata | ❌ No (derived) |
| **AWS S3** | The locker | The original file bytes (PDFs, images, exports) | ❌ No (blob store) |

**Pointers tie them together** (Postgres stays authoritative):
- `documents.s3_uri` → the file bytes in S3
- `document_chunks.pinecone_vector_id` → the vector in Pinecone
- Every Pinecone vector's metadata carries `entity_id`, `document_id`, `chunk_id`,
  `sensitivity_level`, `region` so search can filter **without trusting the caller**.

---

## 2. AWS RDS (Postgres) — structure

**Location:** `us-east-1`, PostgreSQL 17.7. Schema lives in
`hermes/db/migrations/`, applied idempotently by `scripts/init_db.py` and tracked in a
`schema_migrations` table.

- `0001_init.sql` — 24 tables + a `sensitivity` domain + a `set_updated_at()` trigger
  function (re-used by ~8 tables).
- `0002_seed.sql` — reference data: **3 roles**, **8 businesses**, **12 app types**.
- `0003_message_metadata.sql` — adds `hermes_messages.metadata jsonb` for replay fidelity.

### 2.1 Connection & auth (`hermes/db/connection.py`)

- **Driver:** `psycopg` (v3), one connection per unit of work via a `cursor()` context
  manager (commit on clean exit, rollback on error, always close). *Note: no pooling yet —
  a fresh connection per request; the code comments flag swapping in a pool under load.*
- **Two auth modes, auto-selected:**
  1. **Password auth** if `DB_PASSWORD` / `PGPASSWORD` is set.
  2. **IAM auth** otherwise — a short-lived RDS token (~15 min) is generated with `boto3`
     from the AWS keys in the env, **cached for 600 s** to avoid regenerating per connect.
- **SSL always required** (`sslmode=require`, upgradeable to `verify-full` with a root cert).
- **Retries** transient DNS/connection failures (default 3, `DB_CONNECT_RETRIES`).
- Credentials are never stored — only used to open the socket.

### 2.2 The "sensitivity" domain

A reusable column type, not an enum (so it extends without `ALTER TYPE`):

```
public  <  internal  <  confidential  <  restricted
```

A viewer with clearance *X* may see level *X and everything below it*. This is enforced
identically in Postgres (`CHECK`) and in the Pinecone query filter.

### 2.3 The 24 tables

**Core (10):**

| Table | Holds | Key isolation columns |
|-------|-------|------------------------|
| `users` | One row per person (name, email, status). Unique on `lower(email)`. | — |
| `roles` | Named permission levels: owner / admin / read-only (seeded). | — |
| `entities` | **The 8 businesses.** `slug` is the stable handle → Pinecone namespace + URLs. | `region` (default `us-east-1`), `country` |
| `user_entity_access` | **THE GATE** — who can see which business + what they can do. | `user_id`+`entity_id` (unique), `role_id`, `permission_scope` jsonb |
| `connected_apps` | Catalog of app **types** (Gmail, Drive, QuickBooks… 12 seeded). | — |
| `integration_connections` | A real connected account for a business. **`secret_ref` is a *pointer* to a vault — never the credential.** | `entity_id`, `sensitivity_level` |
| `hermes_sessions` | One row per chat (builds the history sidebar). Owned by a user. | `user_id` |
| `hermes_messages` | One row per message. `content` text + `metadata` jsonb (thinking + tool traces). | `entity_id` (nullable), `sensitivity_level`, `seq` |
| `documents` | One row per file. `s3_uri` → the bytes. | `entity_id`, `sensitivity_level` |
| `document_chunks` | A document split into pieces; each → its `pinecone_vector_id`. | `entity_id`, `sensitivity_level`, `region`, `is_active`, `version` |

**Supporting (14) — schema scaffolding from the spec:**
`sync_jobs`, `ai_runs`, `hermes_agent_runs`, `hermes_tool_calls`, `approvals`,
`audit_logs`, `activity_events`, `pipeline_runs`, `notifications`,
`quickbooks_snapshots`, `calendar_events`, `email_threads`, `email_messages`,
`farm_form_submissions`.

> ⚠️ **Finding — defined vs. used.** A grep for SQL across the codebase shows that **only
> `hermes/db/conversations.py` and `hermes/db/documents.py` issue queries.** They touch
> just **6 tables at runtime**: `users`, `entities`, `hermes_sessions`, `hermes_messages`,
> `documents`, `document_chunks` (plus `roles`/`connected_apps` are written once at seed
> time). The other **~16 tables exist in the schema but are not yet wired** to any code path
> — they're forward-looking structure for audit logging, approvals, app sync, analytics
> dashboards, and the per-app data caches (email/calendar/QuickBooks/farm forms). This is
> worth knowing: the DB *describes* a much larger system than the API currently exercises.

### 2.4 Relationships (foreign keys)

```
users ──< user_entity_access >── entities
users ──< hermes_sessions ──< hermes_messages >── entities (nullable)
entities ──< documents ──< document_chunks
documents.message_id ──> hermes_messages   (links an ingested file to a chat message)
entities ──< integration_connections >── connected_apps
entities ──< (sync_jobs, approvals, audit_logs, calendar_events,
              email_threads ──< email_messages, quickbooks_snapshots, …)
```

Cascade rules: deleting a `hermes_session` cascades to its messages; deleting an `entity`
cascades to its documents/chunks; message deletion nulls `documents.message_id` (file
survives). `entity_id` is **denormalized onto child rows** (messages, chunks, documents) so
the backend can filter by tenant cheaply without joins.

---

## 3. Pinecone — structure

**Status:** code is ready; the live index is created by `scripts/init_pinecone.py` once
`PINECONE_API_KEY` is set. Config in `hermes/pinecone_store.py`.

### 3.1 Index configuration

| Setting | Value | Env override |
|---------|-------|--------------|
| Index name | `hermes` | `PINECONE_INDEX` |
| Type | Serverless | — |
| Cloud / region | `aws` / `us-east-1` | `PINECONE_CLOUD`, `PINECONE_REGION` |
| Dimensions | **1536** | `EMBED_DIM` |
| Metric | **cosine** | — |
| Embedding model | OpenAI **`text-embedding-3-small`** | `EMBED_MODEL` |

Embeddings are produced by a **direct HTTPS call to OpenAI's `/embeddings`** endpoint
(`pinecone_store.embed()` using `requests`), *not* the OpenAI SDK. One index for everything;
isolation is done by namespace + metadata, not by separate indexes.

### 3.2 Namespaces — the isolation boundary

| Pattern | Purpose | Sharing |
|---------|---------|---------|
| `entity__<slug>` | One per business — its shared knowledge base (e.g. `entity__gak_properties`). All 8 businesses, incl. `entity__personal`. | Within the business only |
| `user__<user_id>` | A user's **private chat memory** — kept separate from `entity__personal` on purpose. | **Never shared** |

> Note: `user__<id>` namespaces are *designed* but not yet populated — chat turns aren't
> embedded to Pinecone yet (see follow-ons in `DATABASE.md`). Today only **document
> ingestion** writes vectors, into `entity__<slug>` namespaces.

### 3.3 What's stored in each vector

Per chunk (`upsert_chunks`):
- **`id`** = `"<document_id>:<chunk_index>"` — this is exactly what's saved back to
  `document_chunks.pinecone_vector_id`.
- **`values`** = the 1536-dim embedding.
- **`metadata`** = `entity_id`, `document_id`, `chunk_id`, `sensitivity_level`, `region`,
  `source_system`, `version`, `is_active`, and a **`text` preview (first 2000 chars)** for
  displaying the source snippet in search results.

### 3.4 The guarded query — the heart of isolation

**Every** read goes through `pinecone_store.query()`, which *always* applies:
- `is_active == true`
- `sensitivity_level ∈ {levels ≤ caller's clearance}`
- `region == <entity's region>` (when provided)
- a **single namespace** (one business) — there is **no cross-business search** by design.
  To search "All Businesses," the caller queries each permitted namespace and merges.

The website and the agent **never** call Pinecone directly — they go through
`hermes/search.py::semantic_search()`, which resolves the entity, calls the guarded query,
then joins hits back to `documents` (title, type, and a presigned S3 link).

---

## 4. S3 — the locker (`hermes/storage.py`)

- **Optional.** If `S3_BUCKET` is unset, ingestion still records + indexes documents — only
  the original bytes aren't persisted.
- **Write:** `put(key, bytes)` where key = `"<slug>/<document_id>/<filename>"`; returns an
  `s3://bucket/key` URI stored in `documents.s3_uri`.
- **Read:** never public — `presign(s3_uri)` issues a short-lived (900 s) GET URL, generated
  on demand during search.
- Uses `boto3` with `AWS_REGION`.

---

## 5. How the code communicates with the databases

### 5.1 Module map

| Concern | Module | Talks to |
|---------|--------|----------|
| API surface (FastAPI) | `app.py` | — (delegates) |
| Conversation history | `hermes/db/conversations.py` (`PostgresConversationStore`) | **Postgres** |
| Document/chunk rows | `hermes/db/documents.py` | **Postgres** |
| DB connection + auth | `hermes/db/connection.py` | **Postgres / boto3 (IAM)** |
| Vector upsert + query | `hermes/pinecone_store.py` | **Pinecone + OpenAI embeddings** |
| Ingestion pipeline | `hermes/ingest.py` | Postgres + Pinecone + S3 |
| Guarded search | `hermes/search.py` | Postgres + Pinecone (+ S3 presign) |
| File bytes | `hermes/storage.py` | **S3 (boto3)** |
| Store selection | `hermes/memory.py` | picks Postgres vs in-memory |

**Store selection is automatic** (`memory.py::_make_store`): if `DBRDS_HOST` is set it uses
`PostgresConversationStore`; otherwise (or if `HERMES_DISABLE_DB` is truthy, e.g. tests) it
falls back to an in-memory store with the *identical* interface. So `app.py` and the agent
never know which backend is live.

### 5.2 Write path — document ingestion (`POST /api/documents`)

```
client → app.upload_documents() → ingest.ingest_document(entity_id, file)
  1. resolve_entity(slug|uuid) ............... Postgres: entities lookup
  2. storage.put(bytes) ...................... S3 (if S3_BUCKET set) → s3_uri
  3. docs_repo.create_document(...) .......... Postgres: INSERT documents
  4. chunk_text(body) ....................... in-process (1000 chars, 150 overlap)
  5. ps.embed(chunks) ....................... OpenAI /embeddings → 1536-dim vectors
  6. ps.upsert_chunks(namespace, vectors) ... Pinecone: upsert into entity__<slug>
  7. docs_repo.add_chunks(...) .............. Postgres: INSERT document_chunks
                                              (each row stores its pinecone_vector_id)
```

Result: one `documents` row + N `document_chunks` rows + N Pinecone vectors, all carrying
the same `entity_id` + `sensitivity_level` + `region`.

### 5.3 Read path — meaning search (`GET /api/search`)

```
client → app.search() → search.semantic_search(entity_id, q, max_sensitivity, top_k)
  1. resolve_entity ......................... Postgres: entities → (uuid, slug, region)
  2. ps.query(namespace=entity__<slug>, ...) Pinecone (guarded: is_active + sensitivity
                                              ≤ clearance + region) — embeds q via OpenAI
  3. docs_repo.documents_by_id([...]) ....... Postgres: join hits back to documents
  4. storage.presign(s3_uri) ................ S3: short-lived download links
```

### 5.4 Chat path — conversation history (`POST /api/chat`, `GET /api/conversations`)

- On each turn `store.record_turn()` **upserts** the `hermes_sessions` row (first turn
  creates it; the first user message becomes the 60-char title) and inserts **two**
  `hermes_messages` rows (user + assistant). The assistant row's `metadata` jsonb stores
  `thinking`, `tool_calls`, and `stop_reason` so a reloaded chat replays identically.
- `seq` is computed as `MAX(seq)+1` per session; `UNIQUE(session_id, seq)` keeps ordering.
- Cross-turn agent memory (`store.get`) is **rebuilt from stored text** — only the most
  recent `HERMES_HISTORY_MESSAGES` (default 40) messages, trimmed to start on a `user` turn
  (Claude API requirement). In-turn tool blocks stay ephemeral.
- Owner-first model: a single owner user (`OWNER_EMAIL`/`OWNER_NAME`, default
  `owner@hermes.local`) backs every session, created lazily on first use.
- The lifespan hook in `app.py` **warms the DB connection at boot** (resolves owner + IAM
  token in a background thread) so the first sidebar load isn't a slow cold start.

### 5.5 Safety properties baked into the code

1. **Tenant isolation** — `entity_id` denormalized on child rows; Pinecone one-namespace-
   per-query; no cross-business search.
2. **Sensitivity gate** — enforced server-side in the Pinecone filter; the caller can't
   raise its own clearance past what the backend passes.
3. **Secrets never in the DB** — `integration_connections.secret_ref` is a vault pointer;
   DB credentials live in env/IAM, not code or logs.
4. **Postgres is truth** — every Pinecone vector has a matching `document_chunks` row, so a
   vector can always be joined back to a real, access-controlled document.
5. **Graceful degradation** — every DB call in `conversations.py` is wrapped so a write/read
   miss logs a warning and returns empty rather than breaking the user's reply.

---

## 6. Gaps & follow-ons (from the code + `DATABASE.md`)

- **PDF/image text extraction** — binary files get a `documents` row but **no chunks/vectors**
  yet (`_extract_text` only decodes text-like bytes). Wire `pdf-co` or `pypdf`.
- **Chat memory → Pinecone** — the `user__<id>` namespace is designed but unused; chat turns
  aren't embedded for semantic recall yet.
- **The ~16 unused tables** — audit logs, approvals, app sync, analytics, and per-app caches
  are schema-only. No code writes them today.
- **Connection pooling** — a fresh psycopg connection per request; fine now, swap for a pool
  under load.
- **S3 bucket** — set `S3_BUCKET` to start persisting original bytes (currently optional).

---

*End of report. A visual architecture/ER diagram accompanies this in the chat.*
