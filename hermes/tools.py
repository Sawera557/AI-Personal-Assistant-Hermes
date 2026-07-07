"""Tool definitions exposed to Claude, plus the dispatcher that executes them."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from urllib.parse import urlsplit

import requests

from . import storage
from .attachments import store as attachment_store
from .config import Config
from .maton import MatonClient, MatonError
from .wati import WatiClient

# Sentinel meaning "no single business scoped — fan out across all connected
# businesses" (as opposed to None, which means "no business context at all",
# i.e. legacy behavior using the plain default key).
ALL_BUSINESSES = "all"

# Cap for files fetched from a `file_url` and forwarded to WATI as a session file.
_MAX_FETCH_BYTES = 25 * 1024 * 1024
# Inline-base64 fallback cap for wati_get_media when S3_BUCKET isn't configured
# (media can't be persisted, so it's inlined into the tool result instead).
_MAX_MEDIA_INLINE_B64_CHARS = 200_000


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

    # ---- WATI (WhatsApp Business) — dedicated tools ------------------------
    # Purpose-built wrappers over the handful of WATI endpoints Hermes supports,
    # so the model doesn't have to hand-construct raw `maton_request` calls for
    # everyday WhatsApp tasks. Still routed through the same per-business Maton
    # key resolution as every other app.
    custom_params_schema = {
        "type": "array",
        "description": "Custom attributes, e.g. [{\"name\": \"lead_stage\", \"value\": \"hot\"}].",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["name", "value"],
        },
    }
    wati_tool_defs: list[dict[str, Any]] = [
        {
            "name": "wati_list_contacts",
            "description": (
                "List WhatsApp contacts from the connected WATI account. Filter by name, "
                "creation date, or advanced attribute filters (e.g. lead stage, owner)."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Filter by contact name (partial match).",
                },
                "attribute_filters": {
                    "type": "array",
                    "description": (
                        "Advanced filters on custom attributes, e.g. "
                        "[{\"name\": \"lead_stage\", \"operator\": \"==\", \"value\": \"hot\"}]."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "operator": {
                                "type": "string",
                                "enum": [
                                    "contain", "notContain", "exist", "notExist",
                                    "==", "!=", "valid", "invalid",
                                ],
                            },
                            "value": {"type": "string"},
                        },
                        "required": ["name", "operator"],
                    },
                },
                "created_date": {
                    "type": "string",
                    "description": "Filter by contact creation date, e.g. 2026-07-01.",
                },
                "page_size": {"type": "integer", "description": "Results per page."},
                "page_number": {"type": "integer", "description": "Page number, 1-based."},
            },
            "required": [],
        },
        {
            "name": "wati_add_contact",
            "description": "Add a new WhatsApp contact to the connected WATI account, with optional custom attributes.",
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": (
                        "Phone number in international format, digits only, no '+' or "
                        "leading zeros — e.g. 919876543210."
                    ),
                },
                "name": {"type": "string", "description": "Contact's display name."},
                "custom_params": custom_params_schema,
            },
            "required": ["whatsapp_number"],
        },
        {
            "name": "wati_update_contact_attributes",
            "description": (
                "Update custom attributes on an existing WATI contact (e.g. lead stage, "
                "owner, deal value)."
            ),
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": "The contact's phone number, digits only.",
                },
                "custom_params": custom_params_schema,
            },
            "required": ["whatsapp_number", "custom_params"],
        },
        {
            "name": "wati_get_messages",
            "description": "Get the WhatsApp message history for a contact.",
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": "The contact's phone number, digits only.",
                },
                "page_size": {"type": "integer", "description": "Results per page."},
                "page_number": {"type": "integer", "description": "Page number, 1-based."},
            },
            "required": ["whatsapp_number"],
        },
        {
            "name": "wati_send_message",
            "description": (
                "Send a free-text WhatsApp session message to a contact. Only works within "
                "WATI's 24-hour customer session window (the contact must have messaged in "
                "the last 24h) — outside that window this call will fail and an approved "
                "template message is required instead. Always confirm the exact message text "
                "with the user before sending."
            ),
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": "The contact's phone number, digits only.",
                },
                "message_text": {"type": "string", "description": "Message to send (max 4096 chars)."},
            },
            "required": ["whatsapp_number", "message_text"],
        },
        {
            "name": "wati_send_file",
            "description": (
                "Send a file (PDF, image, etc.) as a WhatsApp session message. Same 24-hour "
                "session window rule as wati_send_message. Provide either `attachment_id` "
                "(a file the user already uploaded in this chat) or `file_url` (an http(s) "
                "URL to fetch and forward). Always confirm with the user before sending."
            ),
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": "The contact's phone number, digits only.",
                },
                "attachment_id": {
                    "type": "string",
                    "description": "Id of a file already uploaded in this chat via the attachments endpoint.",
                },
                "file_url": {
                    "type": "string",
                    "description": "An http(s) URL to fetch and forward instead of attachment_id.",
                },
                "caption": {"type": "string", "description": "Optional caption sent with the file."},
            },
            "required": ["whatsapp_number"],
        },
        {
            "name": "wati_assign_operator",
            "description": "Assign a human operator (agent) to handle a contact's WhatsApp conversation.",
            "properties": {
                "whatsapp_number": {
                    "type": "string",
                    "description": "The contact's phone number, digits only.",
                },
                "email": {
                    "type": "string",
                    "description": "Email address of the WATI operator/agent to assign.",
                },
            },
            "required": ["whatsapp_number", "email"],
        },
        {
            "name": "wati_get_media",
            "description": (
                "Download a media file (image, document, voice note, etc.) that a contact "
                "sent, or that was sent to them. Requires the media's `file_name`, typically "
                "found in a message record returned by wati_get_messages or an inbound "
                "webhook payload. When storage is configured, the file is saved for this "
                "business and shown inline in the chat — just report that you retrieved it "
                "and mention any caption/context; you don't need to describe or re-attach it."
            ),
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "The media file name, as referenced in a WATI message record.",
                },
            },
            "required": ["file_name"],
        },
    ]

    for tool_def in wati_tool_defs:
        props = dict(tool_def["properties"])
        if fan_out:
            props["business"] = business_param
        tools.append(
            {
                "name": tool_def["name"],
                "description": tool_def["description"],
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": tool_def["required"],
                },
            }
        )

    if Config.WEB_SEARCH:
        tools.append({"type": "web_search_20260209", "name": "web_search"})
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch"})

    return tools


WATI_TOOLS = {
    "wati_list_contacts",
    "wati_add_contact",
    "wati_update_contact_attributes",
    "wati_get_messages",
    "wati_send_message",
    "wati_send_file",
    "wati_assign_operator",
    "wati_get_media",
}

# Names that Hermes executes locally (everything else is server-side).
LOCAL_TOOLS = {"maton_request", "maton_list_connections", "maton_create_connection", *WATI_TOOLS}


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


def _attachment_scope(tool_input: dict[str, Any], active_business: str | None) -> str | None:
    """The entity_id an attachment was stored under, using the same business-slug
    resolution precedence as `_resolve_client` (without the Maton-key checks)."""
    slug = tool_input.get("business") or None
    if slug:
        return slug
    return active_business if active_business != ALL_BUSINESSES else None


def _fetch_url_bytes(url: str) -> tuple[bytes, str, str]:
    """Download a file from an http(s) URL to forward as a WATI session file."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise MatonError("file_url must be an http(s) URL.")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise MatonError(f"Failed to download file_url: {exc}") from exc
    if len(resp.content) > _MAX_FETCH_BYTES:
        raise MatonError(f"file_url content exceeds the {_MAX_FETCH_BYTES // (1024 * 1024)}MB limit.")
    media_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    filename = urlsplit(url).path.rsplit("/", 1)[-1] or "file"
    return resp.content, media_type, filename


def _wati_send_file_args(
    tool_input: dict[str, Any], active_business: str | None
) -> tuple[str, str, bytes]:
    """Resolve (filename, media_type, raw_bytes) for wati_send_file from either
    an `attachment_id` (a file already uploaded to this chat) or a `file_url`."""
    attachment_id = tool_input.get("attachment_id")
    file_url = tool_input.get("file_url")
    if attachment_id:
        scope = _attachment_scope(tool_input, active_business)
        meta = attachment_store.meta(attachment_id, scope)
        block = attachment_store.block(attachment_id, scope)
        if not meta or not block:
            raise MatonError(f"Unknown or inaccessible attachment_id: {attachment_id}")
        source = block.get("source") or {}
        if source.get("type") != "base64":
            raise MatonError(
                "This attachment kept no original file bytes (only its extracted text "
                "was stored) — re-upload the file, or pass `file_url` instead."
            )
        raw_bytes = base64.standard_b64decode(source["data"])
        media_type = source.get("media_type") or "application/octet-stream"
        filename = meta.get("filename") or "file"
        return filename, media_type, raw_bytes
    if file_url:
        raw_bytes, media_type, filename = _fetch_url_bytes(file_url)
        return filename, media_type, raw_bytes
    raise MatonError("Provide either `attachment_id` or `file_url`.")


def _wati_get_media(
    client: MatonClient, tool_input: dict[str, Any], active_business: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Download WATI media. When S3 is configured, persist it under the
    conversation's business (`wati-media/{business}/...`) so it survives past
    this tool result and can be rendered inline in the chat UI; the caller
    folds the returned `media` dict into the tool call's trace entry, which
    flows into the conversation transcript. Falls back to inlining a capped
    base64 preview when S3_BUCKET isn't configured.
    """
    wati = WatiClient(client)
    file_name = tool_input["file_name"]
    dl = wati.download_media(file_name)

    if not dl.get("ok"):
        raw = dl.get("raw") or b""
        content_type = dl.get("content_type") or ""
        text = raw.decode("utf-8", errors="replace")
        body: Any = text
        if "json" in content_type:
            try:
                body = json.loads(text)
            except ValueError:
                pass
        return {"status": dl.get("status"), "ok": False, "data": body}, None

    raw: bytes = dl["raw"]
    content_type = dl.get("content_type") or "application/octet-stream"
    filename = file_name.rsplit("/", 1)[-1] or "file"
    kind = "image" if content_type.startswith("image/") else "file"

    if storage.enabled():
        scope = _attachment_scope(tool_input, active_business) or "unsorted"
        ext = f".{filename.rsplit('.', 1)[-1]}" if "." in filename else ""
        key = f"wati-media/{scope}/{uuid.uuid4().hex}{ext}"
        s3_uri = storage.put(key, raw, content_type)
        media = {
            "kind": kind,
            "s3_uri": s3_uri,
            "content_type": content_type,
            "filename": filename,
            "size_bytes": len(raw),
        }
        result = {
            "status": dl["status"],
            "ok": True,
            "data": {**media, "stored": True},
        }
        return result, media

    # No S3 bucket configured — fall back to inlining a capped base64 preview,
    # so the model still gets something even though it won't persist or render.
    b64 = base64.standard_b64encode(raw).decode("utf-8")
    data: dict[str, Any] = {
        "stored": False,
        "content_type": content_type,
        "filename": filename,
        "size_bytes": len(raw),
    }
    if len(b64) > _MAX_MEDIA_INLINE_B64_CHARS:
        data["_truncated"] = True
        data["_note"] = (
            "S3_BUCKET isn't configured so this media can't be stored or shown in the "
            f"chat UI — base64 truncated to {_MAX_MEDIA_INLINE_B64_CHARS} chars."
        )
        data["base64_preview"] = b64[:_MAX_MEDIA_INLINE_B64_CHARS]
    else:
        data["base64"] = b64
    return {"status": dl["status"], "ok": True, "data": data}, None


def dispatch(
    name: str,
    tool_input: dict[str, Any],
    active_business: str | None = None,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Execute a local tool. Returns (content_string, is_error, media).

    `media` is non-None only for tools that fetched a file worth persisting
    and showing in the chat UI (currently just wati_get_media) — the caller
    (hermes.agent) folds it into that tool call's trace entry so it survives
    into the conversation transcript.
    """
    media: dict[str, Any] | None = None
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
            elif name in WATI_TOOLS:
                wati = WatiClient(client)
                if name == "wati_list_contacts":
                    result = wati.list_contacts(
                        name=tool_input.get("name"),
                        attribute_filters=tool_input.get("attribute_filters"),
                        created_date=tool_input.get("created_date"),
                        page_size=tool_input.get("page_size"),
                        page_number=tool_input.get("page_number"),
                    )
                elif name == "wati_add_contact":
                    result = wati.add_contact(
                        whatsapp_number=tool_input["whatsapp_number"],
                        name=tool_input.get("name"),
                        custom_params=tool_input.get("custom_params"),
                    )
                elif name == "wati_update_contact_attributes":
                    result = wati.update_contact_attributes(
                        whatsapp_number=tool_input["whatsapp_number"],
                        custom_params=tool_input["custom_params"],
                    )
                elif name == "wati_get_messages":
                    result = wati.get_messages(
                        whatsapp_number=tool_input["whatsapp_number"],
                        page_size=tool_input.get("page_size"),
                        page_number=tool_input.get("page_number"),
                    )
                elif name == "wati_send_message":
                    result = wati.send_session_message(
                        whatsapp_number=tool_input["whatsapp_number"],
                        message_text=tool_input["message_text"],
                    )
                elif name == "wati_send_file":
                    filename, media_type, raw_bytes = _wati_send_file_args(
                        tool_input, active_business
                    )
                    result = wati.send_session_file(
                        whatsapp_number=tool_input["whatsapp_number"],
                        filename=filename,
                        media_type=media_type,
                        raw_bytes=raw_bytes,
                        caption=tool_input.get("caption"),
                    )
                elif name == "wati_assign_operator":
                    result = wati.assign_operator(
                        whatsapp_number=tool_input["whatsapp_number"],
                        email=tool_input["email"],
                    )
                else:  # wati_get_media
                    result, media = _wati_get_media(client, tool_input, active_business)
            else:
                return (f"Unknown tool: {name}", True, None)
    except MatonError as exc:
        return (json.dumps({"error": str(exc)}), True, None)
    except KeyError as exc:
        return (json.dumps({"error": f"Missing required argument: {exc}"}), True, None)

    # A non-2xx upstream status is surfaced to the model but not treated as a
    # hard tool error, so Claude can read the error body and adjust.
    is_error = not result.get("ok", False)
    return (json.dumps(result, default=str), is_error, media)
