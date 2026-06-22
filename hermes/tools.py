"""Tool definitions exposed to Claude, plus the dispatcher that executes them."""
from __future__ import annotations

import json
from typing import Any

from .config import Config
from .maton import MatonClient, MatonError


def build_tools() -> list[dict[str, Any]]:
    """Return the tool list passed to the Messages API."""
    apps = Config.apps()
    app_hint = ", ".join(apps)

    tools: list[dict[str, Any]] = [
        {
            "name": "maton_request",
            "description": (
                "Call a connected third-party app through the Maton API gateway. "
                "Maton injects the OAuth token and proxies to the app's native REST API. "
                f"Allowed app slugs: {app_hint}. "
                "`path` is the native API path for that app (everything after the app slug). "
                "Examples: app='google-mail', path='/gmail/v1/users/me/messages'. "
                "app='google-sheets', path='/v4/spreadsheets/{id}/values/Sheet1!A1:C10'. "
                "app='quickbooks', path='/quickbooks/v2/company/:realmId/query' "
                "(`:realmId` is auto-substituted). "
                "Always do read-only (GET) calls to discover IDs before any write. "
                "Do NOT call this for POST/PUT/PATCH/DELETE until the user has explicitly "
                "confirmed the change."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": f"Maton app slug. One of: {app_hint}",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "description": "HTTP method.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Native API path beginning with '/' (after the app slug).",
                    },
                    "query": {
                        "type": "object",
                        "description": "Optional query-string parameters.",
                        "additionalProperties": True,
                    },
                    "body": {
                        "type": "object",
                        "description": "Optional JSON request body for write methods.",
                        "additionalProperties": True,
                    },
                    "connection_id": {
                        "type": "string",
                        "description": "Optional Maton connection id when multiple exist for an app.",
                    },
                },
                "required": ["app", "method", "path"],
            },
        },
        {
            "name": "maton_list_connections",
            "description": (
                "List the user's Maton app connections so you know which apps are "
                "connected before calling them. Optionally filter by app slug or status "
                "(ACTIVE | PENDING | FAILED)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Optional app slug filter."},
                    "status": {
                        "type": "string",
                        "enum": ["ACTIVE", "PENDING", "FAILED"],
                        "description": "Optional status filter.",
                    },
                },
            },
        },
        {
            "name": "maton_create_connection",
            "description": (
                "Begin connecting a new app. Returns an OAuth authorization URL the user "
                "must open in their browser to grant access. Use this when an app the user "
                "wants is not yet connected."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "App slug to connect, e.g. 'quickbooks' or 'google-mail'.",
                    },
                },
                "required": ["app"],
            },
        },
    ]

    if Config.WEB_SEARCH:
        tools.append({"type": "web_search_20260209", "name": "web_search"})
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch"})

    return tools


# Names that Hermes executes locally (everything else is server-side).
LOCAL_TOOLS = {"maton_request", "maton_list_connections", "maton_create_connection"}


def dispatch(name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Execute a local tool. Returns (content_string, is_error)."""
    client = MatonClient()
    try:
        if name == "maton_request":
            result = client.request(
                app=tool_input["app"],
                method=tool_input.get("method", "GET"),
                path=tool_input["path"],
                query=tool_input.get("query"),
                body=tool_input.get("body"),
                connection_id=tool_input.get("connection_id"),
            )
        elif name == "maton_list_connections":
            result = client.list_connections(
                app=tool_input.get("app"), status=tool_input.get("status")
            )
        elif name == "maton_create_connection":
            result = client.create_connection(app=tool_input["app"])
        else:
            return (f"Unknown tool: {name}", True)
    except MatonError as exc:
        return (json.dumps({"error": str(exc)}), True)
    except KeyError as exc:
        return (json.dumps({"error": f"Missing required argument: {exc}"}), True)

    # A non-2xx upstream status is surfaced to the model but not treated as a
    # hard tool error, so Claude can read the error body and adjust.
    is_error = not result.get("ok", False)
    return (json.dumps(result, default=str), is_error)
