"""Tool definitions exposed to Claude, plus the dispatcher that executes them."""
from __future__ import annotations

import json
from typing import Any

from .config import Config
from .maton import MatonClient, MatonError

# Sentinel meaning "no single business scoped — fan out across all connected
# businesses" (as opposed to None, which means "no business context at all",
# i.e. legacy behavior using the plain default key).
ALL_BUSINESSES = "all"


def build_tools(active_business: str | None = None) -> list[dict[str, Any]]:
    """Return the tool list passed to the Messages API.

    `active_business` is the business slug locked to this turn/conversation, or
    ALL_BUSINESSES ("all") when no single business is scoped. Only in the "all"
    case do the Maton tools get an extra `business` parameter, so Claude can
    pick which connected account to use per call — for a locked single-business
    conversation, the parameter is omitted entirely, making it structurally
    impossible for a tool call to reach a different business's data.
    """
    apps = Config.apps()
    app_hint = ", ".join(apps)
    fan_out = active_business == ALL_BUSINESSES

    request_props: dict[str, Any] = {
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
    }
    connections_props: dict[str, Any] = {
        "app": {"type": "string", "description": "Optional app slug filter."},
        "status": {
            "type": "string",
            "enum": ["ACTIVE", "PENDING", "FAILED"],
            "description": "Optional status filter.",
        },
    }

    if fan_out:
        connected = Config.connected_business_slugs()
        business_param = {
            "type": "string",
            "enum": connected,
            "description": (
                "Required: which connected business's account to use for this call. "
                "The user has 'All Businesses' selected, so you must pick one per call "
                f"— you cannot query more than one business at once. Connected: {connected}."
            ),
        }
        request_props["business"] = business_param
        connections_props["business"] = business_param

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
                "properties": request_props,
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
                "properties": connections_props,
            },
        },
        {
            "name": "maton_create_connection",
            "description": (
                "Begin connecting a new app. Returns an OAuth authorization URL the user "
                "must open in their browser to grant access. Use this when an app the user "
                "wants is not yet connected. Only works for a single scoped business — if "
                "the user has 'All Businesses' selected, ask them to pick a specific "
                "business first."
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


def _resolve_client(tool_input: dict[str, Any], active_business: str | None) -> MatonClient:
    """Pick the right Maton key for this tool call.

    Priority: an explicit `business` argument on the tool call (only meaningful
    in "all businesses" mode) > the turn's locked active_business > the plain
    default key (legacy behavior for callers with no business context at all).

    Raises MatonError with a message meant to be relayed to the user when the
    resolved business has no configured key — never silently substitutes
    another business's key.
    """
    slug = tool_input.get("business") or None
    if not slug:
        if active_business == ALL_BUSINESSES:
            raise MatonError(
                "No business specified. In 'All Businesses' mode you must pass a "
                "`business` argument on every Maton tool call."
            )
        if active_business:
            slug = active_business
        else:
            return MatonClient()  # no business context at all — legacy default key

    key = Config.maton_key_for_slug(slug)
    if not key:
        raise MatonError(
            f"'{slug}' has no Maton connection configured yet. Tell the user this "
            "business isn't connected — do not substitute another business's account "
            "or data."
        )
    return MatonClient(api_key=key)


def dispatch(
    name: str,
    tool_input: dict[str, Any],
    active_business: str | None = None,
) -> tuple[str, bool]:
    """Execute a local tool. Returns (content_string, is_error)."""
    try:
        if name == "maton_create_connection":
            # Connection provisioning is interactive/rare — keep it scoped to a
            # single locked business, never fan out or guess in "all" mode.
            if active_business == ALL_BUSINESSES:
                raise MatonError(
                    "Ask the user to pick a specific business before connecting a new app "
                    "— 'All Businesses' mode can't provision a connection."
                )
            client = _resolve_client({}, active_business)
            result = client.create_connection(app=tool_input["app"])
        else:
            client = _resolve_client(tool_input, active_business)
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
