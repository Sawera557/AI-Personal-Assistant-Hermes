"""Tool definitions exposed to Claude, plus the dispatcher that executes them."""
from __future__ import annotations

import base64
import json
import uuid
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import requests

from . import extract
from . import storage
from . import worldbank
from .attachments import store as attachment_store
from .businesses import BUSINESS_TO_ENTITY_SLUG, to_business_slug
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
    drive_props: dict[str, Any] = {
        "file_id": {
            "type": "string",
            "description": (
                "The Drive file's `id`, from a prior maton_request call to "
                "app='google-drive', path='drive/v3/files'."
            ),
        },
    }
    read_document_props: dict[str, Any] = {
        "s3_uri": {
            "type": "string",
            "description": (
                "The exact `s3://...` URI of the file to read. Use the URI listed under "
                "'Files shared in this conversation' — copy it verbatim, never invent one."
            ),
        },
    }
    send_email_props: dict[str, Any] = {
        "to": {
            "type": "array", "items": {"type": "string"},
            "description": "Recipient email address(es).",
        },
        "cc": {
            "type": "array", "items": {"type": "string"},
            "description": "Optional CC address(es).",
        },
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Plain-text email body."},
    }
    zoom_invite_props: dict[str, Any] = {
        "meeting_id": {
            "type": "string",
            "description": (
                "The Zoom meeting's numeric id, from a prior maton_request call to "
                "app='zoom', path='/v2/users/me/meetings' (or app='zoom-admin')."
            ),
        },
        "to": {
            "type": "array", "items": {"type": "string"},
            "description": "Recipient email address(es) to invite.",
        },
        "note": {
            "type": "string",
            "description": "Optional extra note/context to include in the invite email.",
        },
        "zoom_app": {
            "type": "string",
            "enum": ["zoom", "zoom-admin"],
            "description": "Which Zoom connection owns this meeting. Defaults to 'zoom'.",
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
        drive_props["business"] = business_param
        read_document_props["business"] = business_param
        send_email_props["business"] = business_param
        zoom_invite_props["business"] = business_param

    tools: list[dict[str, Any]] = [
        {
            "name": "maton_request",
            "description": (
                "Call a connected third-party app through the Maton API gateway. "
                "Maton injects the OAuth token and proxies to the app's native REST API. "
                f"Allowed app slugs: {app_hint}. "
                "`path` is the native API path for that app (everything after the app slug). "
                "Examples: app='google-mail', path='/gmail/v1/users/me/messages'. "
                "To SEND an email, use the dedicated `send_email` tool instead of building the "
                "raw Gmail payload here yourself — hand-encoding the base64 MIME message risks "
                "mojibake and mistyped tokens. For a Zoom meeting invite specifically, use "
                "`zoom_send_invite` (it also auto-CCs the user). "
                "app='google-sheets', path='/v4/spreadsheets/{id}/values/Sheet1!A1:C10'. "
                "app='quickbooks', path='/quickbooks/v2/company/:realmId/query' "
                "(`:realmId` is auto-substituted). "
                "Notion (app='notion') — everything is JSON, no OCR/binary handling needed: "
                "search the whole workspace with method='POST', path='/v1/search', "
                "body={'query': '...', 'filter': {'value': 'page'|'database', 'property': 'object'}}. "
                "List/filter a database's rows with method='POST', "
                "path='/v1/databases/{database_id}/query', body={'filter': {...}, 'sorts': [...]} "
                "(get the database_id and its property schema first via method='GET', "
                "path='/v1/databases/{database_id}' so you use the right property names/types). "
                "Read a single page's properties with method='GET', path='/v1/pages/{page_id}'; "
                "read its body content with method='GET', path='/v1/blocks/{page_id}/children'. "
                "Read comments with method='GET', path='/v1/comments?block_id={page_or_block_id}'. "
                "Create a page/database row with method='POST', path='/v1/pages', "
                "body={'parent': {'database_id': '...'} or {'page_id': '...'}, 'properties': {...}} "
                "— each property value must match its type in the database schema, e.g. "
                "{'Name': {'title': [{'text': {'content': '...'}}]}}, "
                "{'Status': {'status': {'name': 'Done'}}}, {'Due': {'date': {'start': 'YYYY-MM-DD'}}}. "
                "Add content blocks to an existing page with method='PATCH', "
                "path='/v1/blocks/{page_id}/children', body={'children': [{'object': 'block', "
                "'type': 'paragraph', 'paragraph': {'rich_text': [{'text': {'content': '...'}}]}}]}. "
                "Post a comment with method='POST', path='/v1/comments', "
                "body={'parent': {'page_id': '...'}, 'rich_text': [{'text': {'content': '...'}}]}. "
                "Update properties (incl. status/assignee/due date) or archive/delete a page with "
                "method='PATCH', path='/v1/pages/{page_id}', body={'properties': {...}} or "
                "body={'archived': true}. "
                "Zoom (app='zoom' for the connected user's own account; app='zoom-admin' for "
                "account-wide access to any user — same endpoints, but zoom-admin uses an "
                "explicit {userId} or email instead of 'me' and can also list/inspect other "
                "users). Get your profile: method='GET', path='/v2/users/me'. "
                "List upcoming meetings: method='GET', path='/v2/users/me/meetings', "
                "query={'type': 'upcoming'} (types: scheduled|live|upcoming|previous_meetings). "
                "Get one meeting's details: method='GET', path='/v2/meetings/{meetingId}'. "
                "Create a meeting: method='POST', path='/v2/users/me/meetings', "
                "body={'topic': '...', 'type': 2, 'start_time': 'YYYY-MM-DDTHH:MM:SSZ', "
                "'duration': 30, 'timezone': 'UTC'} (type: 1=instant, 2=scheduled, "
                "3=recurring no fixed time, 8=recurring fixed time). "
                "Update a meeting: method='PATCH', path='/v2/meetings/{meetingId}', "
                "body={...changed fields...}. Delete: method='DELETE', "
                "path='/v2/meetings/{meetingId}'. "
                "List a user's cloud recordings: method='GET', path='/v2/users/me/recordings' "
                "(optionally query={'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'}); recordings for "
                "one meeting: method='GET', path='/v2/meetings/{meetingId}/recordings'; delete "
                "with method='DELETE', path='/v2/meetings/{meetingId}/recordings'. "
                "Webinars (needs the Webinar add-on): list method='GET', "
                "path='/v2/users/me/webinars'; create/view/update/delete mirror the meetings "
                "endpoints with 'webinars' in place of 'meetings'. "
                "Registrants: list method='GET', path='/v2/meetings/{meetingId}/registrants'; "
                "add one with method='POST', path='/v2/meetings/{meetingId}/registrants', "
                "body={'email': '...', 'first_name': '...'}. "
                "Past-meeting details/participants: method='GET', "
                "path='/v2/past_meetings/{meetingId}' or "
                "path='/v2/past_meetings/{meetingId}/participants'. "
                "zoom-admin only — list every user in the account: method='GET', "
                "path='/v2/users'; a specific user's profile/settings: method='GET', "
                "path='/v2/users/{userId}' or path='/v2/users/{userId}/settings'; another "
                "user's meetings/recordings/webinars: same paths as above with "
                "'/v2/users/{userId}/...' instead of '/v2/users/me/...'; account-wide settings "
                "(paid plans only): method='GET', path='/v2/accounts/{accountId}/settings'. "
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
        {
            "name": "drive_read_file",
            "description": (
                "Fetch a Google Drive file's actual bytes (PDF or image) and hand them to "
                "you to read directly — you read PDFs and images natively, no OCR step "
                "needed. Use this whenever you need to see what's INSIDE a Drive file, not "
                "just its name/metadata (which maton_request's `drive/v3/files` list already "
                "gives you, including the `id` this tool needs). Only PDF/JPEG/PNG content "
                "can be shown inline; other file types, or files the gateway can't proxy as "
                "binary, come back as metadata + a webViewLink instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": drive_props,
                "required": ["file_id"],
            },
        },
        {
            "name": "read_document",
            "description": (
                "Read the text of a document that was previously shared into this "
                "conversation and stored in the file locker (S3) — e.g. a PDF, photo, or "
                "invoice a contact sent over WhatsApp. Fetches the file by its `s3_uri`, "
                "runs OCR on scanned pages and phone photos, and returns the extracted "
                "text so you can summarize or answer questions about it — with no need for "
                "the user to download and re-upload anything. The available files and their "
                "`s3_uri`s are listed under 'Files shared in this conversation' in your "
                "context. Supports PDF, JPEG/PNG images, DOCX, and plain-text files."
            ),
            "input_schema": {
                "type": "object",
                "properties": read_document_props,
                "required": ["s3_uri"],
            },
        },
        {
            "name": "send_email",
            "description": (
                "Send an email through the connected Gmail account. The MIME message and "
                "base64 encoding are built in code from the plain `to`/`subject`/`body` you "
                "pass — any characters (em dashes, curly quotes, etc.) are encoded correctly, "
                "unlike hand-building the raw Gmail payload via maton_request, which risks "
                "encoding corruption."
            ),
            "input_schema": {
                "type": "object",
                "properties": send_email_props,
                "required": ["to", "subject", "body"],
            },
        },
        {
            "name": "zoom_send_invite",
            "description": (
                "Send a Zoom meeting invite by email. Fetches the meeting's real topic, time, "
                "and join link directly from Zoom and builds the email in code, so the join "
                "URL is always copied byte-for-byte — never retyped/hallucinated. Automatically "
                "also sends a copy to the user's own email."
            ),
            "input_schema": {
                "type": "object",
                "properties": zoom_invite_props,
                "required": ["meeting_id", "to"],
            },
        },
        {
            "name": "worldbank_data",
            "description": (
                "Fetch authoritative agriculture & livestock statistics from the World Bank "
                "Open Data API — reliable, machine-readable numbers to ground data-driven "
                "farming decisions (crop strategy, livestock benchmarking, export context) in "
                "real figures instead of guesses. Defaults to Kenya. Use this whenever the user "
                "wants a defensible number, a trend over time, or a country comparison for "
                "agriculture/livestock — prefer it over web_search for these, since it returns "
                "clean multi-year series you can cite.\n"
                "`indicators` accepts these curated keys (or any raw World Bank indicator code): "
                "crop_production_index, livestock_production_index, food_production_index, "
                "cereal_yield, cereal_production, agricultural_land_pct, arable_land_pct, "
                "irrigated_land_pct, fertilizer_use, agri_pct_gdp, agri_value_added_usd, "
                "employment_in_agri_pct, rural_population_pct, food_exports_pct, "
                "agri_raw_exports_pct.\n"
                "`countries` accepts names (e.g. 'kenya', 'ethiopia', 'netherlands') or ISO3 "
                "codes ('KEN'), plus aggregates 'world'/'WLD' and 'sub-saharan africa'/'SSF' — "
                "pass several to compare Kenya against peers or export markets. Omit for Kenya.\n"
                "Use `most_recent: 1` for 'the latest figure', or `start_year`/`end_year` for a "
                "trend. Data is annual and typically lags 1-2 years. Cite the source and year in "
                "your answer."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "indicators": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more catalog keys or raw World Bank indicator codes.",
                    },
                    "countries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Country names/ISO3 codes or aggregates. Defaults to Kenya.",
                    },
                    "start_year": {"type": "integer", "description": "Inclusive start year of the range."},
                    "end_year": {"type": "integer", "description": "Inclusive end year of the range."},
                    "most_recent": {
                        "type": "integer",
                        "description": "Keep only the N most recent non-null points per series (e.g. 1 for the latest).",
                    },
                },
                "required": ["indicators"],
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
        # allowed_callers=["direct"] restricts these to normal tool_use calls
        # (what the dispatch loop already handles) instead of also allowing
        # the programmatic/code-execution caller, which not every model
        # supports (e.g. Claude Haiku rejects the request outright without it).
        tools.append({"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]})
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "allowed_callers": ["direct"]})

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
LOCAL_TOOLS = {
    "maton_request", "maton_list_connections", "maton_create_connection",
    "drive_read_file", "read_document", "send_email", "zoom_send_invite",
    "worldbank_data", *WATI_TOOLS,
}

# Drive mime types we can hand to Claude as a native document/image block, and
# the magic bytes that confirm Maton actually proxied real binary content and
# not a JSON error body pretending to be a file (mirrors ingestion-service's
# ingestion/connectors/drive.py, which proved this download path works).
_DRIVE_READABLE_MIMES = {"application/pdf", "image/jpeg", "image/jpg", "image/png"}
_DRIVE_MAGIC = {
    "application/pdf": (b"%PDF",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/jpg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG",),
}


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


# Max bytes to pull back from S3 for reading, and max extracted chars to hand
# the model (keeps a single huge scan from blowing the context window).
_MAX_READ_BYTES = _MAX_FETCH_BYTES
_MAX_EXTRACTED_CHARS = 40_000


def _key_business(key: str) -> str | None:
    """The business slug an S3 key belongs to, by our prefix conventions:

    * ``wati-media/{business}/...`` and ``drive-files/{business}/...`` are keyed
      by the *business* slug (afk, gak, ...) — see `_wati_get_media`/`_drive_read_file`.
    * A bare ``{entity}/...`` KB-document key is keyed by the *entity* slug
      (afro_farm, ...); map it back to its business slug.

    Returns None for anything that doesn't resolve to a known business (the
    `unsorted/` hole, `bills/gmail/...`, or an unknown tenant) — such keys are
    not readable through this tool.
    """
    parts = [p for p in key.split("/") if p]
    if not parts:
        return None
    if parts[0] in ("wati-media", "drive-files"):
        seg = parts[1] if len(parts) >= 3 else None
        return seg if seg in BUSINESS_TO_ENTITY_SLUG else None
    return to_business_slug(parts[0])


def _read_scope_ok(
    s3_uri: str, active_business: str | None, requested_business: str | None
) -> tuple[bool, str]:
    """Enforce entity-prefix scoping on an S3 read. Returns (ok, reason).

    The key must belong to the business this turn is scoped to, so one business's
    conversation can never read another's files. In "All Businesses" mode with no
    explicit `business`, the target business is derived from the key itself and
    only needs to be a known business.
    """
    key = storage.key_of(s3_uri)
    if not key:
        return False, "That isn't a valid file URI in the configured storage bucket."
    if any(seg in ("", "..", ".") for seg in key.split("/")):
        return False, "That file path isn't allowed."

    key_biz = _key_business(key)
    if not key_biz:
        return False, "That file isn't in a readable per-business location."

    allowed = requested_business or (
        active_business if active_business != ALL_BUSINESSES else None
    )
    if allowed and key_biz != allowed:
        return (
            False,
            f"That file belongs to a different business ('{key_biz}') than this "
            f"conversation ('{allowed}'). I can't read another business's files.",
        )
    return True, ""


def _read_document(
    tool_input: dict[str, Any], active_business: str | None
) -> dict[str, Any]:
    """Fetch a stored file by its s3_uri, extract its text, and return it so the
    model can read the document directly — scoped to the active business."""
    if not storage.enabled():
        return {
            "status": 400, "ok": False,
            "data": {"error": "File storage (S3) isn't configured, so documents can't be read back."},
        }
    s3_uri = (tool_input.get("s3_uri") or "").strip()
    if not s3_uri:
        return {"status": 400, "ok": False,
                "data": {"error": "Provide the `s3_uri` of the file to read."}}

    ok, reason = _read_scope_ok(s3_uri, active_business, tool_input.get("business"))
    if not ok:
        return {"status": 403, "ok": False, "data": {"error": reason}}

    filename = (storage.key_of(s3_uri) or "file").rsplit("/", 1)[-1]
    try:
        raw, mime = storage.get_bytes(s3_uri, max_bytes=_MAX_READ_BYTES)
    except Exception as exc:  # noqa: BLE001 — surface a readable error to the model
        return {"status": 404, "ok": False,
                "data": {"error": f"Couldn't fetch the file: {exc}"}}

    try:
        text = extract.extract_text(filename, raw, mime or "")
    except extract.ExtractionError as exc:
        return {"status": 422, "ok": False, "data": {"error": str(exc)}}

    truncated = len(text) > _MAX_EXTRACTED_CHARS
    if truncated:
        text = text[:_MAX_EXTRACTED_CHARS]
    return {
        "status": 200, "ok": True,
        "data": {
            "filename": filename,
            "content_type": mime,
            "truncated": truncated,
            "text": text,
        },
    }


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


def _drive_read_file(
    client: MatonClient, tool_input: dict[str, Any], active_business: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]] | None]:
    """Download a Drive file's real bytes through Maton and hand them back as a
    native document/image content block so Claude can read the file directly —
    no OCR step needed. Falls back to metadata + webViewLink when the type
    isn't PDF/JPEG/PNG, or the gateway can't proxy real binary content for it
    (checked via magic bytes — the same check ingestion-service's Drive
    connector uses, which proved this `alt=media` download path works)."""
    file_id = tool_input["file_id"]

    meta_resp = client.request(
        "google-drive", "GET", f"drive/v3/files/{file_id}",
        query={"fields": "id,name,mimeType,webViewLink"},
    )
    if not meta_resp.get("ok"):
        return meta_resp, None, None

    meta = meta_resp.get("data") or {}
    name = meta.get("name", "untitled")
    mime = (meta.get("mimeType") or "").lower()
    web_link = meta.get("webViewLink")

    def _locator(note: str) -> dict[str, Any]:
        return {
            "status": 200,
            "ok": True,
            "data": {
                "readable": False, "name": name, "mimeType": mime,
                "webViewLink": web_link, "note": note,
            },
        }

    if mime not in _DRIVE_READABLE_MIMES:
        return _locator("Not a PDF/JPEG/PNG — share the webViewLink with the user instead."), None, None

    dl = client.download_binary(
        "google-drive", "GET", f"drive/v3/files/{file_id}", query={"alt": "media"},
    )
    raw: bytes = dl.get("raw") or b""
    magic_ok = any(raw[: len(m)] == m for m in _DRIVE_MAGIC.get(mime, ()))
    if not dl.get("ok") or not magic_ok:
        return _locator(
            "Couldn't fetch readable binary content for this file — share the "
            "webViewLink with the user instead."
        ), None, None
    if len(raw) > _MAX_FETCH_BYTES:
        return _locator(
            f"File exceeds the {_MAX_FETCH_BYTES // (1024 * 1024)}MB limit for inline "
            "reading — share the webViewLink with the user instead."
        ), None, None

    b64 = base64.standard_b64encode(raw).decode("utf-8")
    block_type = "document" if mime == "application/pdf" else "image"
    block: dict[str, Any] = {
        "type": block_type,
        "source": {"type": "base64", "media_type": mime, "data": b64},
    }
    if block_type == "document":
        block["title"] = name

    media: dict[str, Any] | None = None
    if storage.enabled():
        scope = _attachment_scope(tool_input, active_business) or "unsorted"
        ext = f".{name.rsplit('.', 1)[-1]}" if "." in name else ""
        key = f"drive-files/{scope}/{uuid.uuid4().hex}{ext}"
        s3_uri = storage.put(key, raw, mime)
        media = {
            "kind": "file", "s3_uri": s3_uri, "content_type": mime,
            "filename": name, "size_bytes": len(raw),
        }

    result = {
        "status": 200,
        "ok": True,
        "data": {
            "readable": True, "name": name, "mimeType": mime,
            "webViewLink": web_link, "size_bytes": len(raw),
        },
    }
    return result, media, [block]


def _build_mime_raw(
    *, to: list[str], subject: str, body: str, cc: list[str] | None = None
) -> str:
    """Build an RFC 2822 message and base64url-encode it for Gmail's `raw`
    field, using Python's email module so UTF-8 headers/body are always
    encoded correctly (RFC 2047 for headers) — no hand-rolled byte/base64
    arithmetic for the model to get wrong."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _send_email(client: MatonClient, tool_input: dict[str, Any]) -> dict[str, Any]:
    to = tool_input["to"]
    to = to if isinstance(to, list) else [to]
    cc = tool_input.get("cc")
    if isinstance(cc, str):
        cc = [cc]
    raw = _build_mime_raw(to=to, subject=tool_input["subject"], body=tool_input["body"], cc=cc)
    return client.request(
        "google-mail", "POST", "/gmail/v1/users/me/messages/send", body={"raw": raw}
    )


def _zoom_send_invite(client: MatonClient, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Fetch the meeting straight from Zoom and build the invite email in code,
    so the join URL is copied verbatim from the API response — never retyped
    by the model — and always CC the user's own email."""
    zoom_app = tool_input.get("zoom_app") or "zoom"
    meeting_id = tool_input["meeting_id"]
    meeting = client.request(zoom_app, "GET", f"/v2/meetings/{meeting_id}")
    if not meeting.get("ok"):
        return meeting

    m = meeting.get("data") or {}
    topic = m.get("topic") or "Zoom Meeting"
    join_url = m.get("join_url") or ""
    start_time = m.get("start_time")
    duration = m.get("duration")
    timezone = m.get("timezone") or "UTC"

    to = tool_input["to"]
    to = to if isinstance(to, list) else [to]

    own_email: str | None = None
    profile = client.request("google-mail", "GET", "/gmail/v1/users/me/profile")
    if profile.get("ok"):
        own_email = (profile.get("data") or {}).get("emailAddress")

    lines = [f"You're invited to: {topic}", ""]
    if start_time:
        lines.append(f"When: {start_time} ({timezone})")
    if duration:
        lines.append(f"Duration: {duration} minutes")
    note = tool_input.get("note")
    if note:
        lines += ["", note]
    lines += ["", f"Join Zoom Meeting: {join_url}"]

    raw = _build_mime_raw(
        to=to,
        cc=[own_email] if own_email and own_email not in to else None,
        subject=f"Zoom Invite: {topic}",
        body="\n".join(lines),
    )
    result = client.request(
        "google-mail", "POST", "/gmail/v1/users/me/messages/send", body={"raw": raw}
    )
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {**result, "data": {**data, "sent_to": to, "cc": own_email, "join_url": join_url}}


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
) -> tuple[str, bool, dict[str, Any] | None, list[dict[str, Any]] | None]:
    """Execute a local tool. Returns (content_string, is_error, media, blocks).

    `media` is non-None only for tools that fetched a file worth persisting
    and showing in the chat UI (wati_get_media, drive_read_file) — the caller
    (hermes.agent) folds it into that tool call's trace entry so it survives
    into the conversation transcript.

    `blocks` is non-None only for drive_read_file: real document/image content
    blocks the caller appends to the conversation so Claude can read the file
    directly on its next turn, instead of only seeing a text tool result.
    """
    media: dict[str, Any] | None = None
    blocks: list[dict[str, Any]] | None = None
    try:
        if name == "worldbank_data":
            # Public data source — no Maton key / business scoping needed.
            result = {
                "status": 200,
                "ok": True,
                "data": worldbank.fetch(
                    indicators=tool_input.get("indicators") or [],
                    countries=tool_input.get("countries"),
                    start_year=tool_input.get("start_year"),
                    end_year=tool_input.get("end_year"),
                    most_recent=tool_input.get("most_recent"),
                ),
            }
        elif name == "read_document":
            # Reads bytes straight from the S3 locker — no Maton client needed.
            # Business scoping is enforced inside via _read_scope_ok.
            result = _read_document(tool_input, active_business)
        elif name == "maton_create_connection":
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
            elif name == "drive_read_file":
                result, media, blocks = _drive_read_file(client, tool_input, active_business)
            elif name == "send_email":
                result = _send_email(client, tool_input)
            elif name == "zoom_send_invite":
                result = _zoom_send_invite(client, tool_input)
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
                return (f"Unknown tool: {name}", True, None, None)
    except MatonError as exc:
        return (json.dumps({"error": str(exc)}), True, None, None)
    except KeyError as exc:
        return (json.dumps({"error": f"Missing required argument: {exc}"}), True, None, None)

    # A non-2xx upstream status is surfaced to the model but not treated as a
    # hard tool error, so Claude can read the error body and adjust.
    is_error = not result.get("ok", False)
    return (json.dumps(result, default=str), is_error, media, blocks if not is_error else None)
