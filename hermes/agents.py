"""Multi-agents — validation, mode resolution and per-turn constraints.

An agent is a named, reusable configuration of the ordinary Hermes turn:
its own role prompt, a whitelist of tools, and modes (named variants that add
instructions and can narrow autonomy, tools and apps). Nothing here runs a
model; hermes.agent.stream_turn asks `turn_plan()` how to constrain a turn and
`prompt_block()` what to tell the model. Storage is hermes.db.agents.

Enforcement, not suggestion: the tool whitelist is applied to the schemas a
turn is OFFERED, and agent._dispatchable_tools refuses anything not offered;
readonly/draft modes go through the same autonomy gate scheduled tasks use
(hermes.tools._restrict_writes / write_blocked_reason).
"""
from __future__ import annotations

import re
from typing import Any

from . import agent_tools
from .agent_presets import SHARED_RULES
from .businesses import BUSINESS_TO_ENTITY_SLUG

AUTONOMIES = ("readonly", "draft", "full")
_RANK = {"readonly": 0, "draft": 1, "full": 2}

MAX_PROMPT = 12000
MAX_INSTRUCTIONS = 4000
MAX_MODES = 12

# Tool groups for the editor's checklist. Anything unlisted lands in "Other".
TOOL_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Research & web", ("web_search", "web_fetch", "worldbank_data")),
    ("Knowledge & files", ("search_business_knowledge", "read_document", "drive_read_file",
                           "create_file", "create_artefact")),
    ("Agent data tools", tuple(sorted(agent_tools.NAMES))),
    ("Connected apps (Maton)", ("maton_request", "maton_list_connections")),
    ("Email & calendar", ("send_email", "email_with_attachment", "calendar_list_upcoming",
                          "zoom_list_meetings", "zoom_send_invite")),
    ("WhatsApp (WATI)", ("wati_list_contacts", "wati_get_messages", "wati_get_media",
                         "wati_send_message", "wati_send_file", "wati_add_contact",
                         "wati_update_contact_attributes", "wati_assign_operator")),
    ("Jira", ("jira_search_issues", "jira_get_issue", "jira_create_issue", "jira_assign_issue")),
]

# Never offered to an agent: connection provisioning, scheduling and the
# guardrail switch stay with the main assistant and the owner.
EXCLUDED_TOOLS = frozenset({
    "maton_create_connection", "set_guardrail",
    "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task",
    "list_scheduled_tasks",
})


def _all_tool_schemas() -> list[dict[str, Any]]:
    from .tools import build_tools

    seen: dict[str, dict[str, Any]] = {}
    for t in build_tools("all") + agent_tools.SCHEMAS:
        name = t.get("name")
        if name and name not in EXCLUDED_TOOLS and name not in seen:
            seen[name] = t
    return list(seen.values())


def known_tool_names() -> set[str]:
    return {t["name"] for t in _all_tool_schemas()}


def _first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    m = re.match(r"(.{20,220}?[.!?])(\s|$)", text)
    return m.group(1) if m else text[:220]


# Anthropic's server-side tools carry no description of their own.
_SERVER_TOOL_DESCRIPTIONS = {
    "web_search": "Search the live web; results come back with source URLs to cite.",
    "web_fetch": "Read a web page (or PDF) found by search or given by the user.",
}


def tool_catalog() -> list[dict[str, Any]]:
    """[{group, tools:[{name, description}]}] for the editor checklist."""
    schemas = {t["name"]: t for t in _all_tool_schemas()}
    for name, desc in _SERVER_TOOL_DESCRIPTIONS.items():
        if name in schemas and not schemas[name].get("description"):
            schemas[name] = {**schemas[name], "description": desc}
    used: set[str] = set()
    groups = []
    for label, names in TOOL_GROUPS:
        items = [n for n in names if n in schemas]
        used.update(items)
        if items:
            groups.append({"group": label, "tools": [
                {"name": n, "description": _first_sentence(schemas[n].get("description", ""))}
                for n in items
            ]})
    rest = sorted(set(schemas) - used)
    if rest:
        groups.append({"group": "Other", "tools": [
            {"name": n, "description": _first_sentence(schemas[n].get("description", ""))}
            for n in rest
        ]})
    return groups


def known_apps() -> set[str]:
    from .config import Config

    return set(Config.apps()) | {"web"}


# ---- validation -------------------------------------------------------------
def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:48] or "agent"


def validate(body: dict[str, Any], *, partial: bool = False,
             current: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Clean an agent create/patch body. Returns (clean, None) or (None, error).

    With `partial`, only the keys present are validated — but cross-field rules
    (mode tools ⊆ agent tools, default_mode exists) are checked against the
    merged result with `current`."""
    out: dict[str, Any] = {}
    known = known_tool_names()

    if "name" in body or not partial:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 80:
            return None, "Name is required (max 80 characters)."
        out["name"] = name
    if "business" in body or not partial:
        biz = body.get("business")
        biz = None if biz in (None, "", "all") else str(biz).strip()
        if biz is not None and biz not in BUSINESS_TO_ENTITY_SLUG:
            return None, f"Unknown business '{biz}'."
        out["business"] = biz
    if "description" in body:
        out["description"] = str(body.get("description") or "").strip()[:400]
    if "icon" in body:
        out["icon"] = str(body.get("icon") or "").strip()[:8]
    if "system_prompt" in body or not partial:
        prompt = str(body.get("system_prompt") or "").strip()
        if not prompt:
            return None, "The agent needs a prompt describing its role."
        if len(prompt) > MAX_PROMPT:
            return None, f"Prompt is too long (max {MAX_PROMPT} characters)."
        out["system_prompt"] = prompt
    if "tools" in body or not partial:
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            return None, "tools must be a list of tool names."
        tools = list(dict.fromkeys(str(t).strip() for t in tools if str(t).strip()))
        unknown = [t for t in tools if t not in known]
        if unknown:
            return None, f"Unknown or unavailable tool(s): {', '.join(unknown)}."
        out["tools"] = tools
    if "enabled" in body:
        out["enabled"] = bool(body.get("enabled"))

    merged_tools = out.get("tools", (current or {}).get("tools") or [])
    if "modes" in body or not partial:
        modes, err = _clean_modes(body.get("modes"), merged_tools)
        if err:
            return None, err
        out["modes"] = modes
    elif "tools" in out and current:
        # Tools changed: existing modes must still fit inside them.
        _, err = _clean_modes(current.get("modes"), merged_tools)
        if err:
            return None, err

    merged_modes = out.get("modes", (current or {}).get("modes") or [])
    if "default_mode" in body or "modes" in out or not partial:
        keys = [m["key"] for m in merged_modes]
        dm = str(body.get("default_mode") or (current or {}).get("default_mode") or "").strip()
        out["default_mode"] = dm if dm in keys else (keys[0] if keys else None)
    return out, None


def _clean_modes(modes: Any, agent_tools_list: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    if modes in (None, []):
        return [{"key": "default", "label": "Default", "instructions": "",
                 "autonomy": "readonly"}], None
    if not isinstance(modes, list):
        return [], "modes must be a list."
    if len(modes) > MAX_MODES:
        return [], f"At most {MAX_MODES} modes."
    allowed_tool_set = set(agent_tools_list)
    apps = known_apps()
    out, keys = [], set()
    for i, m in enumerate(modes):
        if not isinstance(m, dict):
            return [], f"Mode {i + 1} is not an object."
        label = str(m.get("label") or "").strip()
        if not label:
            return [], f"Mode {i + 1} needs a label."
        key = _slugify(m.get("key") or label)
        if key in keys:
            return [], f"Two modes share the key '{key}'."
        keys.add(key)
        autonomy = str(m.get("autonomy") or "readonly").strip().lower()
        if autonomy not in AUTONOMIES:
            return [], f"Mode '{label}': autonomy must be readonly, draft or full."
        instructions = str(m.get("instructions") or "").strip()
        if len(instructions) > MAX_INSTRUCTIONS:
            return [], f"Mode '{label}': instructions too long (max {MAX_INSTRUCTIONS})."
        clean = {"key": key, "label": label[:60], "instructions": instructions, "autonomy": autonomy}
        mt = m.get("tools")
        if mt:
            mt = [str(t).strip() for t in mt if str(t).strip()]
            bad = [t for t in mt if t not in allowed_tool_set]
            if bad:
                return [], f"Mode '{label}' uses tool(s) the agent doesn't have: {', '.join(bad)}."
            clean["tools"] = mt
        ma = m.get("allowed_apps")
        if ma:
            ma = [str(a).strip() for a in ma if str(a).strip()]
            bad = [a for a in ma if a not in apps]
            if bad:
                return [], f"Mode '{label}': unknown app(s) {', '.join(bad)}."
            clean["allowed_apps"] = ma
        out.append(clean)
    return out, None


def new_slug(name: str, taken: set[str]) -> str:
    base = _slugify(name)
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---- per-turn ---------------------------------------------------------------
def resolve_mode(agent: dict[str, Any], mode_key: str | None) -> dict[str, Any]:
    modes = agent.get("modes") or []
    for key in (mode_key, agent.get("default_mode")):
        if key:
            hit = next((m for m in modes if m.get("key") == key), None)
            if hit:
                return hit
    return modes[0] if modes else {"key": "default", "label": "Default",
                                   "instructions": "", "autonomy": "readonly"}


def turn_plan(
    agent: dict[str, Any], mode: dict[str, Any],
    autonomy: str | None, allowed_apps: list[str] | None,
) -> tuple[str | None, list[str] | None, frozenset[str]]:
    """(autonomy, allowed_apps, tool_names) for one agent turn.

    Autonomy: the stricter of the caller's and the mode's. In a live chat the
    caller passes None (= every write is held for the user's approval); a
    `full` mode keeps that, readonly/draft remove the write capability itself.
    Apps: intersect the mode's list with the caller's. Tools: the agent's
    whitelist, narrowed by the mode's, with maton_list_connections riding along
    with maton_request so the agent can see what is connected."""
    mode_aut = mode.get("autonomy") or "readonly"
    if autonomy is None:
        eff = None if mode_aut == "full" else mode_aut
    else:
        eff = min((autonomy, mode_aut), key=lambda a: _RANK.get(a, 0))

    mode_apps = mode.get("allowed_apps") or None
    if mode_apps and allowed_apps:
        apps = [a for a in allowed_apps if a in set(mode_apps)]
    else:
        apps = mode_apps or allowed_apps

    names = set(agent.get("tools") or [])
    if mode.get("tools"):
        names &= set(mode["tools"])
    if "maton_request" in names:
        names.add("maton_list_connections")
    return eff, (list(apps) if apps else None), frozenset(names - EXCLUDED_TOOLS)


_AUTONOMY_LINE = {
    "readonly": "This mode is READ-ONLY: you can read, analyse and save files, but cannot send, create or change anything in connected apps.",
    "draft": "This mode is DRAFT-ONLY: you may prepare drafts (e.g. a Gmail draft) but never send or change anything else.",
    "full": "This mode may act, but every send or write is shown to the user for approval first.",
}


def prompt_block(agent: dict[str, Any], mode: dict[str, Any]) -> str:
    """The ACTIVE AGENT section of the system prompt."""
    parts = [
        f"ACTIVE AGENT — you are operating as Hermes's \"{agent.get('name')}\" agent. "
        "The role below takes priority over the general assistant persona above, but never "
        "over GROUNDING & ATTRIBUTION, the business context pack's rules, or the safety "
        "guardrails.",
        "ROLE:\n" + str(agent.get("system_prompt") or "").strip(),
        f"CURRENT MODE: {mode.get('label') or mode.get('key')}"
        + (f"\n{mode['instructions'].strip()}" if (mode.get("instructions") or "").strip() else ""),
        _AUTONOMY_LINE.get(mode.get("autonomy") or "readonly", ""),
        SHARED_RULES,
    ]
    return "\n\n".join(p for p in parts if p)
