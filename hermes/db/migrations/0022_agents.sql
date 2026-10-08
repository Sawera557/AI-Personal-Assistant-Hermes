-- Hermes — 0022: Multi-agents.
--
-- A named, reusable agent: its own system prompt, a whitelist of tools, and a
-- list of modes (named variants that add instructions and narrow autonomy /
-- tools / apps). A chat turn that names an agent runs the normal Hermes loop
-- constrained to that agent — see hermes/agents.py and agent.stream_turn.
--
-- Built-in agents (is_builtin) are seeded lazily from hermes/agent_presets.py
-- by slug; they can be edited and reset, never deleted. Additive only.
--
-- Named hermes_agents, not agents: the shared database already has an
-- unrelated `agents` table (key, display_name, capabilities, ...) owned by
-- another system. Never touch that one.

CREATE TABLE IF NOT EXISTS hermes_agents (
    agent_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- NULL entity_id == "All Businesses": the agent follows the top-bar business.
    entity_id     uuid REFERENCES entities (entity_id) ON DELETE CASCADE,

    slug          text NOT NULL,
    name          text NOT NULL,
    description   text NOT NULL DEFAULT '',
    icon          text NOT NULL DEFAULT '',
    system_prompt text NOT NULL,

    -- JSON array of tool names this agent may be offered.
    tools         jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- JSON array of {key, label, instructions, autonomy, tools?, allowed_apps?}.
    modes         jsonb NOT NULL DEFAULT '[]'::jsonb,
    default_mode  text,

    is_builtin    boolean NOT NULL DEFAULT false,
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (user_id, slug)
);

CREATE INDEX IF NOT EXISTS hermes_agents_user_idx ON hermes_agents (user_id);
