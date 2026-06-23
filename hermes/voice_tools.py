"""Read-only tool layer for voice mode.

Voice (Groq) may READ and SUMMARIZE data from the user's connected Maton apps,
but must NEVER perform a write/action. This module is the hard, code-level
enforcement of that rule.

It does NOT re-implement any Maton call — allowed reads route through the SAME
`hermes.tools.dispatch()` the typed app uses. Disallowed calls are blocked here,
before they can ever reach `dispatch()` / Maton.
"""
from __future__ import annotations

import json
from typing import Any

from .tools import dispatch

# The only tools voice mode may invoke at all.
_READ_TOOLS = {"maton_request", "maton_list_connections"}

REFUSAL = (
    "Voice mode is read-only — I can look things up and summarize, but I can't "
    "make changes. Please use the typed assistant for anything that writes."
)


def is_read_only(name: str, tool_input: dict[str, Any]) -> bool:
    """True only if this exact call cannot cause a side effect."""
    if name == "maton_list_connections":
        return True
    if name == "maton_request":
        method = str(tool_input.get("method", "GET")).strip().upper()
        return method == "GET"
    # maton_create_connection (starts an OAuth connection) and anything else
    # count as actions and are never allowed in voice mode.
    return False


def voice_dispatch(name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Execute a voice tool call ONLY if it is read-only.

    Returns (content_string, is_error) — same shape as `tools.dispatch()`.
    A blocked call never reaches `dispatch()` / Maton.
    """
    tool_input = dict(tool_input or {})
    if name not in _READ_TOOLS or not is_read_only(name, tool_input):
        return (json.dumps({"blocked": True, "reason": REFUSAL}), True)
    if name == "maton_request":
        # Belt-and-suspenders: force GET even though is_read_only already checked.
        tool_input["method"] = "GET"
    return dispatch(name, tool_input)


# OpenAI/Groq-format function schemas for the voice LLM. Note: `maton_request`
# deliberately exposes NO `method` field, so the model cannot even express a
# write — and `voice_dispatch` forces GET regardless. `maton_create_connection`
# is intentionally absent.
VOICE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "maton_request",
            "description": (
                "Read data from one of the user's connected apps via the Maton "
                "gateway (read-only GET). Use this to look up and summarize "
                "emails, files, invoices, calendar events, contacts, etc. "
                "`path` is the app's native API path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Maton app slug, e.g. google-mail, quickbooks, google-drive.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Native API path beginning with '/' (after the app slug).",
                    },
                    "query": {
                        "type": "object",
                        "description": "Optional query-string parameters.",
                    },
                },
                "required": ["app", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "maton_list_connections",
            "description": "List which of the user's apps are connected to Maton.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Optional app slug filter."},
                    "status": {"type": "string", "description": "Optional: ACTIVE | PENDING | FAILED."},
                },
            },
        },
    },
]
