"""HTTP routes for Multi-agents (the Multi-agents tab) — a self-contained APIRouter.

Chatting with an agent goes through the normal /api/chat/stream endpoint with
`agent_id` + `agent_mode` (app.py); these routes only manage the roster.

  GET    /api/agents?entity_id=      roster (built-ins seeded on first read)
  GET    /api/agents/tool-catalog    grouped tool checklist for the editor
  POST   /api/agents                 create
  GET    /api/agents/{id}            one agent (uuid or slug)
  PATCH  /api/agents/{id}            edit
  DELETE /api/agents/{id}            delete (custom agents only)
  POST   /api/agents/{id}/reset      restore a built-in to its shipped preset
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import agents as agents_domain
from .db import agents as agents_db

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@router.get("")
async def list_agents(entity_id: str | None = None):
    try:
        items = await run_in_threadpool(agents_db.list_agents, entity_id)
    except Exception as exc:  # noqa: BLE001
        return _err(f"Couldn't load agents: {exc}", 500)
    return {"agents": items}


# Static path before /{agent_id} so it isn't captured as an id.
@router.get("/tool-catalog")
async def tool_catalog():
    groups = await run_in_threadpool(agents_domain.tool_catalog)
    return {"groups": groups, "autonomies": list(agents_domain.AUTONOMIES),
            "apps": sorted(agents_domain.known_apps())}


@router.post("")
async def create_agent(body: dict[str, Any] = Body(...)):
    clean, err = await run_in_threadpool(agents_domain.validate, body)
    if err:
        return _err(err)
    agent = await run_in_threadpool(agents_db.create_agent, clean)
    return {"agent": agent}


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    agent = await run_in_threadpool(agents_db.get_agent, agent_id)
    if not agent:
        return _err("Agent not found.", 404)
    return {"agent": agent}


@router.patch("/{agent_id}")
async def patch_agent(agent_id: str, body: dict[str, Any] = Body(...)):
    current = await run_in_threadpool(agents_db.get_agent, agent_id)
    if not current:
        return _err("Agent not found.", 404)
    clean, err = await run_in_threadpool(
        lambda: agents_domain.validate(body, partial=True, current=current)
    )
    if err:
        return _err(err)
    agent = await run_in_threadpool(agents_db.update_agent, current["agent_id"], clean)
    return {"agent": agent}


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    current = await run_in_threadpool(agents_db.get_agent, agent_id)
    if not current:
        return _err("Agent not found.", 404)
    try:
        await run_in_threadpool(agents_db.delete_agent, current["agent_id"])
    except agents_db.AgentError as exc:
        return _err(str(exc))
    return {"ok": True}


@router.post("/{agent_id}/reset")
async def reset_agent(agent_id: str):
    current = await run_in_threadpool(agents_db.get_agent, agent_id)
    if not current:
        return _err("Agent not found.", 404)
    try:
        agent = await run_in_threadpool(agents_db.reset_agent, current["agent_id"])
    except agents_db.AgentError as exc:
        return _err(str(exc))
    return {"agent": agent}
