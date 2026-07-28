-- Hermes — 0017: Context packs + Playbooks (Plan tasks B1 + D1).
--
-- Two independent features, one migration:
--
--  * context_packs — always-present, per-business standing facts injected whole
--    into the system prompt each turn (owner rules, tenant rosters, watchlists).
--    One row per slug: 'global' plus one per business ('gak','gif','afk',...).
--    This is the biggest single quality lever: it gives the agent the durable
--    context it otherwise lacks. Written by load_packs.py / the (future) admin
--    endpoint; read by hermes/context_packs.py.
--
--  * playbooks — written procedures a scheduled run follows (arrears-chase,
--    friday-brief, ...). A task is linked to a playbook via task_playbooks so the
--    scheduler can inject the procedure without touching the scheduled_tasks
--    read path. Written by load_playbooks.py; read by hermes/scheduler.py.
--
-- Idempotent (IF NOT EXISTS); safe to re-run via scripts/init_db.py.

-- ── Context packs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS context_packs (
    pack_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       text UNIQUE NOT NULL,          -- 'global' | business slug (gak, gif, afk, gakc, nh, per)
    content_md text NOT NULL DEFAULT '',
    version    integer NOT NULL DEFAULT 1,     -- bumped on every upsert
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ── Playbooks ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playbooks (
    playbook_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,          -- arrears-chase, friday-brief, ...
    name        text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',       -- one line: when to use it
    content_md  text NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Link a scheduled task to the playbook it should follow. Kept OUT of the
-- scheduled_tasks table on purpose: the scheduler looks it up by task_id in
-- build_run_prompt, so the hot task-read path (_TASK_COLS / _task_row_to_dict)
-- is untouched. One playbook per task (task_id is the PK).
CREATE TABLE IF NOT EXISTS task_playbooks (
    task_id       uuid PRIMARY KEY REFERENCES scheduled_tasks(task_id) ON DELETE CASCADE,
    playbook_slug text NOT NULL,
    assigned_at   timestamptz NOT NULL DEFAULT now()
);
