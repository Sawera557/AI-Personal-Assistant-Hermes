"""Postgres-backed store for Multi-agents (table `hermes_agents`, migration 0022).

Mirrors hermes/db/scheduled_tasks.py: owner-scoped rows, business stored as an
entity_id and surfaced as the business slug. Built-in agents are seeded lazily
from hermes/agent_presets.py (insert-if-missing by slug) the first time the
roster is read, so a fresh database needs no seed step. Validation lives in
hermes/agents.py — this module only stores what it is handed.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .connection import cursor
from .scheduled_tasks import _is_uuid, _iso, _owner, _resolve_entity_id

log = logging.getLogger("hermes.db.agents")

_COLS = (
    "a.agent_id, a.slug, a.name, a.description, a.icon, a.system_prompt, a.tools, "
    "a.modes, a.default_mode, a.is_builtin, a.enabled, e.slug, a.created_at, a.updated_at"
)
_FROM = "FROM hermes_agents a LEFT JOIN entities e ON e.entity_id = a.entity_id"


class AgentError(ValueError):
    """A refused change (e.g. deleting a built-in) — surfaced as HTTP 400."""


def _row(row: tuple) -> dict[str, Any]:
    from ..businesses import to_business_slug

    (agent_id, slug, name, desc, icon, prompt, tools, modes, default_mode,
     is_builtin, enabled, entity_slug, created, updated) = row
    return {
        "agent_id": str(agent_id),
        "slug": slug,
        "name": name,
        "description": desc or "",
        "icon": icon or "",
        "system_prompt": prompt,
        "tools": tools or [],
        "modes": modes or [],
        "default_mode": default_mode,
        "is_builtin": bool(is_builtin),
        "enabled": bool(enabled),
        "business": to_business_slug(entity_slug),  # None == All Businesses
        "created_at": _iso(created),
        "updated_at": _iso(updated),
    }


def _entity_for(cur, business: str | None) -> str | None:
    from ..businesses import to_entity_slug

    if not business:
        return None
    return _resolve_entity_id(cur, to_entity_slug(business))


def _seed_builtins(cur, user_id: str) -> None:
    from ..agent_presets import BUILTIN_AGENTS

    for p in BUILTIN_AGENTS:
        cur.execute(
            "INSERT INTO hermes_agents (user_id, entity_id, slug, name, description, icon, "
            "system_prompt, tools, modes, default_mode, is_builtin) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, true) "
            "ON CONFLICT (user_id, slug) DO NOTHING;",
            (user_id, _entity_for(cur, p.get("business")), p["slug"], p["name"],
             p.get("description", ""), p.get("icon", ""), p["system_prompt"],
             json.dumps(p["tools"]), json.dumps(p["modes"]), p.get("default_mode")),
        )


def list_agents(business: str | None = None) -> list[dict[str, Any]]:
    """Every agent, built-ins first. With `business`, only that business's
    agents plus the all-business ones."""
    with cursor() as cur:
        uid = _owner(cur)
        _seed_builtins(cur, uid)
        cur.execute(
            f"SELECT {_COLS} {_FROM} WHERE a.user_id = %s "
            "ORDER BY a.is_builtin DESC, a.created_at, a.name;",
            (uid,),
        )
        agents = [_row(r) for r in cur.fetchall()]
    if business and business != "all":
        agents = [a for a in agents if a["business"] in (None, business)]
    return agents


def get_agent(agent_ref: str) -> dict[str, Any] | None:
    """By agent_id (uuid) or slug."""
    ref = str(agent_ref or "").strip()
    if not ref:
        return None
    col = "a.agent_id" if _is_uuid(ref) else "a.slug"
    with cursor() as cur:
        uid = _owner(cur)
        if col == "a.slug":
            _seed_builtins(cur, uid)
        cur.execute(f"SELECT {_COLS} {_FROM} WHERE a.user_id = %s AND {col} = %s;", (uid, ref))
        row = cur.fetchone()
    return _row(row) if row else None


def create_agent(clean: dict[str, Any]) -> dict[str, Any]:
    from ..agents import new_slug

    with cursor() as cur:
        uid = _owner(cur)
        cur.execute("SELECT slug FROM hermes_agents WHERE user_id = %s;", (uid,))
        taken = {r[0] for r in cur.fetchall()}
        slug = new_slug(clean["name"], taken)
        cur.execute(
            "INSERT INTO hermes_agents (user_id, entity_id, slug, name, description, icon, "
            "system_prompt, tools, modes, default_mode, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
            "RETURNING agent_id;",
            (uid, _entity_for(cur, clean.get("business")), slug, clean["name"],
             clean.get("description", ""), clean.get("icon", ""), clean["system_prompt"],
             json.dumps(clean.get("tools") or []), json.dumps(clean.get("modes") or []),
             clean.get("default_mode"), clean.get("enabled", True)),
        )
        agent_id = str(cur.fetchone()[0])
    return get_agent(agent_id)  # type: ignore[return-value]


_UPDATABLE = ("name", "description", "icon", "system_prompt", "tools", "modes",
              "default_mode", "enabled")


def update_agent(agent_id: str, clean: dict[str, Any]) -> dict[str, Any] | None:
    sets, vals = [], []
    for k in _UPDATABLE:
        if k in clean:
            if k in ("tools", "modes"):
                sets.append(f"{k} = %s::jsonb")
                vals.append(json.dumps(clean[k]))
            else:
                sets.append(f"{k} = %s")
                vals.append(clean[k])
    with cursor() as cur:
        uid = _owner(cur)
        if "business" in clean:
            sets.append("entity_id = %s")
            vals.append(_entity_for(cur, clean["business"]))
        if sets:
            sets.append("updated_at = now()")
            cur.execute(
                f"UPDATE hermes_agents SET {', '.join(sets)} WHERE agent_id = %s AND user_id = %s;",
                (*vals, agent_id, uid),
            )
            if cur.rowcount == 0:
                return None
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> bool:
    agent = get_agent(agent_id)
    if not agent:
        return False
    if agent["is_builtin"]:
        raise AgentError("Built-in agents can't be deleted — switch it off, or reset it to default.")
    with cursor() as cur:
        uid = _owner(cur)
        cur.execute("DELETE FROM hermes_agents WHERE agent_id = %s AND user_id = %s;", (agent_id, uid))
        return cur.rowcount > 0


def reset_agent(agent_id: str) -> dict[str, Any] | None:
    from ..agent_presets import preset_by_slug

    agent = get_agent(agent_id)
    if not agent:
        return None
    preset = preset_by_slug(agent["slug"]) if agent["is_builtin"] else None
    if not preset:
        raise AgentError("Only built-in agents can be reset to default.")
    return update_agent(agent_id, {
        "name": preset["name"], "description": preset.get("description", ""),
        "icon": preset.get("icon", ""), "system_prompt": preset["system_prompt"],
        "tools": preset["tools"], "modes": preset["modes"],
        "default_mode": preset.get("default_mode"), "business": preset.get("business"),
        "enabled": True,
    })
