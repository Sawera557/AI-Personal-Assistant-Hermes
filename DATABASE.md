# Hermes — Database Setup

Three storage places, per the project scope doc:

| Store    | Role             | What it holds                                            |
|----------|------------------|---------------------------------------------------------|
| Postgres | the record keeper | lists, access rules, chat history — the source of truth |
| S3       | the locker        | real files (PDFs, images, exports); served via short links |
| Pinecone | the search brain  | a *search copy* of text for meaning search (no real files) |

## Postgres (AWS RDS — `us-east-1`)

**Status: built & verified live (PostgreSQL 17.7).** 24 schema tables + seed data.

- Schema: `hermes/db/migrations/0001_init.sql` (10 core + 13 supporting tables).
- Seed: `hermes/db/migrations/0002_seed.sql` (3 roles, 8 businesses, 12 apps).
- Connection: `hermes/db/connection.py` — auto-selects auth:
  - **Password auth** if `DB_PASSWORD` is set, else
  - **IAM auth** — a short-lived RDS token is generated with boto3 from the AWS
    keys in the environment (no AWS CLI needed). SSL always required.

Run / re-run (idempotent, tracked in `schema_migrations`):

```bash
python scripts/init_db.py            # apply pending migrations
python scripts/init_db.py --status   # show applied vs pending
```

### Isolation & safety (enforced by the schema)
- Every business is separated by `entity_id`, denormalized onto child rows
  (messages, chunks, documents…) so the backend can filter cheaply.
- `sensitivity_level` (`public < internal < confidential < restricted`) travels
  with rows.
- `integration_connections.secret_ref` is a **pointer to a vault** — never the
  credential itself.
- `documents.s3_uri` points at S3; `document_chunks.pinecone_vector_id` points
  at the vector. Postgres stays the truth.

## Pinecone (the search brain)

**Status: code ready; needs `PINECONE_API_KEY` to create the live index.**

- One serverless index (`aws` / `us-east-1`), cosine, **1536 dims**.
- Embeddings: OpenAI **`text-embedding-3-small`** (validated working).
- Namespaces:
  - `entity__<slug>` — one per business (all 8, incl. `personal`, `kenya_ventures`).
  - `user__<user_id>` — **private** chat memory, never shared (separate from
    `entity__personal` on purpose).
- Sensitivity + region are enforced as **metadata filters**, not separate indexes.
- Only the backend talks to Pinecone, always through `pinecone_store.query()`,
  which forces the namespace boundary + `is_active` + sensitivity (<= clearance)
  + region filters. No cross-business search by design.

Create the index once `PINECONE_API_KEY` is in `.env`:

```bash
python scripts/init_pinecone.py
```

## Decisions locked in (spec section 7)
- Embeddings: OpenAI `text-embedding-3-small`, **one** index.
- Private data: **two** folders — `entity__personal` + `user__<id>`.
- Region: **all `us-east-1`**; Kenya businesses tagged `country='KE'` for a future
  residency move without a schema change.
- All **8** business namespaces included.

## Conversation history — wired to Postgres ✅

Chat history is persisted in `hermes_sessions` + `hermes_messages`:

- `hermes/db/conversations.py` — `PostgresConversationStore`, a drop-in for the
  old in-memory store (same methods), selected automatically in
  `hermes/memory.py` whenever `DBRDS_HOST` is set (else it falls back to memory).
- **Sessions** belong to a single **owner** user (owner-first; set `OWNER_EMAIL`
  / `OWNER_NAME` to override). **Messages** carry the optional `entity_id`
  (a business), accepted as a slug or uuid.
- The agent's cross-turn memory (`store.get`) is rebuilt from stored
  user/assistant text. In-turn tool blocks stay ephemeral — Postgres is the
  display/record truth.
- Assistant `metadata` jsonb (migration 0003) keeps the reasoning summary + tool
  traces, so a reloaded conversation replays exactly as it did live.

Endpoints now reading/writing Postgres: `GET/DELETE /api/conversations[/{id}]`,
and `POST /api/chat` (records each turn). No frontend or API changes were needed.

## Still in-memory (next step)
`hermes/attachments.py` still uses process memory. Migrating uploads to
`documents` (+ S3 for the bytes, + `document_chunks` → Pinecone for search) is
the follow-on — the schema and the Pinecone layer are both ready for it.
