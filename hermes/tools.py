"""Tool definitions exposed to Claude, plus the dispatcher that executes them."""
from __future__ import annotations

import base64
import json
import uuid
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import requests

from . import calendar as hermes_calendar
from . import storage
from . import worldbank
from .attachments import store as attachment_store
from .businesses import BUSINESS_TO_ENTITY_SLUG, to_entity_slug
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


def build_tools(
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the tool list passed to the Messages API.

    `active_business` is the business slug locked to this turn/conversation, or
    ALL_BUSINESSES ("all") when no single business is scoped. Only in the "all"
    case do the Maton tools get an extra `business` parameter, so Claude can
    pick which connected account to use per call — for a locked single-business
    conversation, the parameter is omitted entirely, making it structurally
    impossible for a tool call to reach a different business's data.

    `allowed_apps` restricts which connected apps this run may use — passed by
    Scheduled Tasks so an unattended run only ever touches the apps the user
    picked for it. It's a list of Maton app slugs (plus the pseudo-slug "web"
    for web search/fetch). When given, `maton_request`'s app enum is narrowed to
    it and every app-gated dedicated tool (send_email, calendar_list_upcoming,
    the wati_* tools, …) is dropped unless its app is in the list. App-agnostic
    tools (search_business_knowledge, create_file, worldbank_data) are always
    kept. An empty/None list means no restriction (normal chat behavior).
    """
    apps = Config.apps()
    restricted = bool(allowed_apps)
    allowed_set = {a.strip() for a in (allowed_apps or []) if a.strip()}
    effective_apps = [a for a in apps if a in allowed_set] if restricted else apps
    app_hint = ", ".join(effective_apps) if effective_apps else ", ".join(apps)
    fan_out = active_business == ALL_BUSINESSES

    request_props: dict[str, Any] = {
        "app": {
            "type": "string",
            "description": f"Maton app slug. One of: {app_hint}",
            # When a Scheduled Task restricts the toolset, hard-narrow the enum so
            # the model structurally cannot reach an app the user didn't allow.
            **({"enum": effective_apps} if restricted and effective_apps else {}),
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
    zoom_list_props: dict[str, Any] = {
        "type": {
            "type": "string",
            "enum": ["scheduled", "live", "upcoming", "previous_meetings"],
            "description": "Which meetings to list. Defaults to 'upcoming'.",
        },
        "zoom_app": {
            "type": "string",
            "enum": ["zoom", "zoom-admin"],
            "description": "Which Zoom connection to list from. Defaults to 'zoom'.",
        },
    }
    knowledge_props: dict[str, Any] = {
        "query": {
            "type": "string",
            "description": "What to look for, in plain language (meaning-based search, not exact keywords).",
        },
    }
    create_file_props: dict[str, Any] = {
        "filename": {
            "type": "string",
            "description": "Name for the file, with extension, e.g. 'q3_summary.md' or 'notes.txt'.",
        },
        "content": {
            "type": "string",
            "description": "The full text content to write into the file.",
        },
        "content_type": {
            "type": "string",
            "description": (
                "Optional MIME type, e.g. 'text/markdown' or 'text/csv'. If omitted, it's "
                "guessed from the filename's extension (falls back to text/plain)."
            ),
        },
    }
    calendar_props: dict[str, Any] = {
        "days": {
            "type": "integer",
            "description": "How many days ahead to look, from now. Defaults to 14.",
        },
        "max_results": {
            "type": "integer",
            "description": "Cap on the number of events returned. Defaults to 20.",
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
        send_email_props["business"] = business_param
        # Knowledge search isn't gated by a Maton connection — any business with
        # uploaded context is searchable, whether or not it has an app connected.
        knowledge_props["business"] = {
            "type": "string",
            "enum": sorted(BUSINESS_TO_ENTITY_SLUG),
            "description": (
                "Required: which business's knowledge base to search. The user has 'All "
                "Businesses' selected, so you must pick one per call."
            ),
        }
        zoom_invite_props["business"] = business_param
        zoom_list_props["business"] = business_param
        calendar_props["business"] = business_param
        # Optional, unlike the others above — a created file isn't tied to any
        # business's connection, so leaving it out just files it generally.
        create_file_props["business"] = {
            "type": "string",
            "enum": sorted(BUSINESS_TO_ENTITY_SLUG),
            "description": (
                "Optional: which business to file this under, just for organizing in S3. "
                "Omit for a general file not tied to one business."
            ),
        }

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
            "name": "calendar_list_upcoming",
            "description": (
                "List a business's upcoming Google Calendar events (its primary calendar) — "
                "meetings, their time, location, and attendees. Use this whenever the user asks "
                "what meetings/events they have, today or in a coming window, instead of "
                "`maton_request` — it already handles the Calendar API's date-range query and "
                "returns a clean, flattened list instead of raw Google JSON. When the user asks "
                "generally 'what meetings do I have', also call `zoom_list_meetings` and combine "
                "both — a meeting may exist in only one of the two."
            ),
            "input_schema": {
                "type": "object",
                "properties": calendar_props,
                "required": [],
            },
        },
        {
            "name": "zoom_list_meetings",
            "description": (
                "List a business's Zoom meetings (scheduled/upcoming/live/past), with join links "
                "and meeting IDs — use this instead of `maton_request` for a clean flattened list "
                "instead of Zoom's raw paginated response. When the user asks generally 'what "
                "meetings do I have', also call `calendar_list_upcoming` and combine both — a "
                "meeting may exist in only one of the two (e.g. a Zoom call with no calendar "
                "invite, or a calendar event with no Zoom link)."
            ),
            "input_schema": {
                "type": "object",
                "properties": zoom_list_props,
                "required": [],
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

    tools.append({
        "name": "search_business_knowledge",
        "description": (
            "Search a business's uploaded knowledge base (context files, documents, and notes "
            "ingested for it) by meaning, not exact keywords. Check here before saying you lack "
            "information about a business's specific rules, background, or policies — this is "
            "where manually-added business context lives. Returns the most relevant snippets "
            "with their source; if nothing relevant comes back, say so rather than guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": knowledge_props,
            "required": ["query"],
        },
    })

    tools.append({
        "name": "create_file",
        "description": (
            "Create a new file from text you write and store it in S3, returning a download "
            "link. Use this whenever the user asks you to generate a document, report, note, "
            "CSV, or any other file they can download — write the full content yourself, then "
            "call this to persist it and get a real, working link. Always share the returned "
            "`download_url` with the user in your reply; never invent or guess a URL yourself. "
            "The link expires after 24 hours — tell the user if they'll need it longer than that. "
            "A `.pdf` filename is rendered into a REAL PDF automatically — but as plain text "
            "only: markdown syntax (#, **, etc.) is NOT interpreted/formatted, it just shows up "
            "as literal characters, so write clean prose for a PDF rather than markdown. Every "
            "other extension (.md, .txt, .csv, .json, .html, ...) is saved exactly as written."
        ),
        "input_schema": {
            "type": "object",
            "properties": create_file_props,
            "required": ["filename", "content"],
        },
    })

    if Config.WEB_SEARCH:
        # allowed_callers=["direct"] restricts these to normal tool_use calls
        # (what the dispatch loop already handles) instead of also allowing
        # the programmatic/code-execution caller, which not every model
        # supports (e.g. Claude Haiku rejects the request outright without it).
        tools.append({"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]})
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "allowed_callers": ["direct"]})

    # Scheduled-task authoring from chat. Deliberately NOT offered on a restricted
    # (scheduled/unattended) run — only a live chat turn, where the user is present
    # to command it, may create standing recurring tasks. So an unattended run can
    # never spawn more tasks. See SCHEDULED_TASKS.md.
    if not restricted:
        tools.append({
            "name": "create_scheduled_task",
            "description": (
                "Create a recurring Scheduled Task in the dashboard from the user's request, so "
                "Hermes runs a job automatically on a schedule (it appears in the Tasks tab and "
                "fires unattended). Use this whenever the user asks to schedule / automate / "
                "'every day/week' / 'remind me to run' something — e.g. 'every morning at 7 give "
                "me a farm briefing', 'each Monday pull the rent arrears'. "
                "Write `prompt` as a complete standalone instruction the agent will run each time "
                "(as if the user typed it in chat), not a paraphrase. "
                "`frequency`: daily (every day at each time), weekdays (Mon–Fri), weekly (only the "
                "`days_of_week`), or hourly (every hour at the minute of the first time). "
                "`days_of_week` uses 0=Mon … 6=Sun and is required for weekly. "
                "`times` are 24h \"HH:MM\" wall-clock in `timezone`. "
                "`business` scopes it to one entity — one of: afk (Afro Farm), gak (GAK "
                "Properties), gif (GIF Properties), gakc (GAK Commodities), nh (Nour Hammam), per "
                "(Personal & Family) — or 'all' for cross-business; default is the conversation's "
                "current business. "
                "`allowed_apps` optionally limits which connected apps the task may use (Maton "
                "slugs like google-mail, google-calendar, quickbooks, google-drive; empty = all). "
                "`autonomy` is the safety ceiling for the UNATTENDED run: 'readonly' (default — "
                "read/analyze/report only), 'draft' (may also create Gmail drafts, never send), or "
                "'full' (may actually send/modify — a standing authorization; only pick this if "
                "the user clearly said to act/send automatically, and confirm first). "
                "If the schedule, business, or autonomy is ambiguous, ask the user a quick "
                "clarifying question before creating; otherwise create it and report back the "
                "resulting schedule and next run."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short display name for the task."},
                    "prompt": {"type": "string", "description": "The full instruction the task runs each time, written as if asked in chat."},
                    "frequency": {"type": "string", "enum": ["daily", "weekdays", "weekly", "hourly"]},
                    "times": {"type": "array", "items": {"type": "string"}, "description": "24h wall-clock times, e.g. [\"07:00\",\"13:00\"]. Defaults to [\"09:00\"]."},
                    "days_of_week": {"type": "array", "items": {"type": "integer"}, "description": "For weekly: 0=Mon … 6=Sun."},
                    "business": {"type": "string", "description": "afk | gak | gif | gakc | nh | per | all"},
                    "allowed_apps": {"type": "array", "items": {"type": "string"}, "description": "Optional Maton app slugs the task may use; empty = all."},
                    "autonomy": {"type": "string", "enum": ["readonly", "draft", "full"]},
                    "timezone": {"type": "string", "description": "IANA tz, e.g. America/Chicago or Africa/Nairobi. Defaults to the app default."},
                    "enabled": {"type": "boolean", "description": "Whether it starts active (fires on schedule). Default true."},
                },
                "required": ["name", "prompt", "frequency"],
            },
        })
        tools.append({
            "name": "list_scheduled_tasks",
            "description": (
                "List the user's existing Scheduled Tasks (name, schedule, business, autonomy, "
                "enabled, next run, last status). Use to answer 'what do I have scheduled?' or "
                "before creating a similar task. Optional `business` slug (afk/gak/…/per or 'all') "
                "scopes the list; omit to use the conversation's current business."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "business": {"type": "string", "description": "afk | gak | gif | gakc | nh | per | all"},
                },
                "required": [],
            },
        })

    if restricted:
        tools = _restrict_tools(tools, allowed_set)

    return tools


# Which connected app(s) each app-gated dedicated tool needs. A tool survives an
# `allowed_apps` restriction only if one of its apps is allowed. Tools NOT listed
# here are app-agnostic (search_business_knowledge, create_file, worldbank_data,
# maton_request/maton_list_connections/maton_create_connection) and always kept —
# maton_request is already narrowed by its app enum above.
_TOOL_REQUIRED_APPS: dict[str, set[str]] = {
    "drive_read_file": {"google-drive"},
    "send_email": {"google-mail"},
    "zoom_send_invite": {"zoom", "zoom-admin"},
    "zoom_list_meetings": {"zoom", "zoom-admin"},
    "calendar_list_upcoming": {"google-calendar"},
    "web_search": {"web"},
    "web_fetch": {"web"},
    "wati_list_contacts": {"wati"},
    "wati_add_contact": {"wati"},
    "wati_update_contact_attributes": {"wati"},
    "wati_get_messages": {"wati"},
    "wati_send_message": {"wati"},
    "wati_send_file": {"wati"},
    "wati_assign_operator": {"wati"},
    "wati_get_media": {"wati"},
}


def _restrict_tools(
    tools: list[dict[str, Any]], allowed_set: set[str]
) -> list[dict[str, Any]]:
    """Drop app-gated tools whose app isn't in `allowed_set` (for Scheduled
    Tasks). App-agnostic tools are always kept."""
    out = []
    for tool in tools:
        needed = _TOOL_REQUIRED_APPS.get(tool.get("name"))
        if needed and not (needed & allowed_set):
            continue
        out.append(tool)
    return out


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
    "drive_read_file", "send_email", "zoom_send_invite", "search_business_knowledge",
    "create_file", "worldbank_data", "calendar_list_upcoming", "zoom_list_meetings",
    "create_scheduled_task", "list_scheduled_tasks",
    *WATI_TOOLS,
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


def _calendar_list_upcoming(tool_input: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    """Upcoming Google Calendar events for one business, via hermes.calendar
    (which already owns the Maton app slug / date-range / response-shaping
    boilerplate). No MatonClient here — just the same business-slug resolution
    precedence as _resolve_client, translated into the {status, ok, data}
    envelope every other tool result uses."""
    slug = tool_input.get("business") or None
    if not slug:
        if active_business == ALL_BUSINESSES:
            raise MatonError(
                "No business specified. In 'All Businesses' mode you must pass a "
                "`business` argument on every Maton tool call."
            )
        if not active_business:
            raise MatonError(
                "No business specified — calendar events are scoped per business."
            )
        slug = active_business

    try:
        events = hermes_calendar.list_upcoming(
            slug,
            days=tool_input.get("days") or 14,
            max_results=tool_input.get("max_results") or 20,
        )
    except (ValueError, RuntimeError) as exc:
        raise MatonError(str(exc)) from exc

    return {"status": 200, "ok": True, "data": {"business": slug, "events": events}}


def _search_business_knowledge(tool_input: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    """Meaning-search one business's ingested knowledge base (uploaded context
    files + any other documents). No Maton client needed — pure Postgres/pgvector
    via hermes.search, so this doesn't go through `_resolve_client`."""
    from .search import semantic_search  # local import: avoid a module-load cycle

    slug = tool_input.get("business") or None
    if not slug:
        if active_business == ALL_BUSINESSES:
            raise MatonError(
                "No business specified. In 'All Businesses' mode you must pass a "
                "`business` argument naming which business's knowledge base to search."
            )
        slug = active_business
    if not slug:
        raise MatonError("No business is scoped for this conversation — nothing to search.")

    entity_slug = to_entity_slug(slug)
    if not entity_slug:
        raise MatonError(f"Unknown business: {slug!r}.")

    query = (tool_input.get("query") or "").strip()
    if not query:
        raise MatonError("`query` is required.")

    try:
        hits = semantic_search(entity_slug, query, max_sensitivity="confidential", top_k=6)
    except ValueError as exc:
        raise MatonError(str(exc)) from exc

    if not hits:
        return {"ok": True, "results": [], "message": "No matching context found in this business's knowledge base."}
    return {
        "ok": True,
        "results": [
            {
                "title": h.get("title") or "untitled",
                "source_system": h.get("source_system"),
                "snippet": (h.get("snippet") or "").strip()[:500],
                "score": h.get("score"),
            }
            for h in hits
        ],
    }


# Presigned link lifetime for create_file — much longer than the 900s used for
# internal search-result previews, since the user is meant to actually click
# this one, possibly a while after the turn it was created in.
_CREATED_FILE_LINK_EXPIRES = 24 * 60 * 60


def _render_text_as_pdf(content: str) -> bytes:
    """Render plain text into a REAL, valid PDF — a .pdf filename with raw
    UTF-8 text bytes dumped into it (the naive approach) isn't a valid PDF and
    fails to open in any viewer. Uses fpdf2 (pure Python, no system/native
    dependencies — safe on serverless). Markdown syntax (#, **, etc.) is not
    interpreted, just shown as literal characters — plain text only, for now.

    fpdf2's built-in core font is Latin-1 only, so common "smart" punctuation
    is normalized to its ASCII equivalent first; anything else outside Latin-1
    is replaced rather than crashing the render.
    """
    from fpdf import FPDF

    normalized = (
        content.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("…", "...")
    )
    safe = normalized.encode("latin-1", errors="replace").decode("latin-1")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in safe.split("\n"):
        # multi_cell(w=0, ...) leaves the cursor at the right edge afterward
        # rather than resetting to the left margin — without this, the next
        # call computes an ~empty width and raises FPDFException.
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, line or " ")
    return bytes(pdf.output())


def _create_file(tool_input: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    """Write model-authored text content to S3 and return a download link. No
    Maton client involved — pure storage.py, same as _search_business_knowledge."""
    if not storage.enabled():
        raise MatonError(
            "File creation isn't available — S3_BUCKET isn't configured. Tell the user "
            "to set S3_BUCKET (and AWS credentials) before this tool can be used."
        )

    filename = (tool_input.get("filename") or "").strip()
    if not filename:
        raise MatonError("`filename` is required.")
    content = tool_input.get("content")
    if not content:
        raise MatonError("`content` is required.")

    if filename.lower().endswith(".pdf"):
        # A .pdf filename with plain UTF-8 text bytes isn't a valid PDF — render
        # a real one instead of just labeling text as application/pdf.
        try:
            raw = _render_text_as_pdf(content)
        except Exception as exc:  # noqa: BLE001 — surface as a clean tool error
            raise MatonError(f"Could not render this content as a PDF: {exc}") from exc
        content_type = "application/pdf"
    else:
        import mimetypes

        content_type = (tool_input.get("content_type") or "").strip() or (
            mimetypes.guess_type(filename)[0] or "text/plain"
        )
        raw = content.encode("utf-8")

    scope = _attachment_scope(tool_input, active_business) or "general"
    key = f"generated/{scope}/{uuid.uuid4().hex}/{filename}"

    s3_uri = storage.put(key, raw, content_type)
    download_url = storage.presign(s3_uri, expires=_CREATED_FILE_LINK_EXPIRES)
    if not download_url:
        raise MatonError("File was written but a download link could not be generated.")

    return {
        "ok": True,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(raw),
        "business": scope if scope != "general" else None,
        "download_url": download_url,
        "expires_in_hours": _CREATED_FILE_LINK_EXPIRES // 3600,
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


def _zoom_list_meetings(client: MatonClient, tool_input: dict[str, Any]) -> dict[str, Any]:
    """List Zoom meetings, shaped into a clean flat list (not Zoom's raw
    paginated envelope) so it reads consistently alongside
    calendar_list_upcoming's already-shaped output."""
    zoom_app = tool_input.get("zoom_app") or "zoom"
    meeting_type = tool_input.get("type") or "upcoming"
    result = client.request(zoom_app, "GET", "/v2/users/me/meetings", query={"type": meeting_type})
    if not result.get("ok"):
        return result

    raw_meetings = (result.get("data") or {}).get("meetings") or []
    meetings = [
        {
            "meeting_id": m.get("id"),
            "topic": m.get("topic"),
            "start_time": m.get("start_time"),
            "duration": m.get("duration"),
            "timezone": m.get("timezone"),
            "join_url": m.get("join_url"),
            "type": m.get("type"),
        }
        for m in raw_meetings
    ]
    return {**result, "data": {"meetings": meetings}}


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


def _create_scheduled_task(tool_input: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    """Create a recurring Scheduled Task from a chat request. Mirrors the
    validation/resolution the POST /api/scheduled-tasks endpoint does, so a
    task the agent makes is identical to one made in the UI."""
    from . import scheduler as sched
    from .db import scheduled_tasks as tasks_db

    name = (tool_input.get("name") or "").strip()
    prompt = (tool_input.get("prompt") or "").strip()
    if not name:
        return {"ok": False, "status": 400, "error": "A task name is required."}
    if not prompt:
        return {"ok": False, "status": 400, "error": "A task prompt is required."}

    frequency = (tool_input.get("frequency") or "daily").strip()
    if frequency not in sched.VALID_FREQUENCIES:
        return {"ok": False, "status": 400, "error": f"frequency must be one of {sorted(sched.VALID_FREQUENCIES)}."}
    autonomy = (tool_input.get("autonomy") or "readonly").strip()
    if autonomy not in sched.VALID_AUTONOMY:
        return {"ok": False, "status": 400, "error": f"autonomy must be one of {sorted(sched.VALID_AUTONOMY)}."}

    times = [t.strip() for t in (tool_input.get("times") or []) if isinstance(t, str) and t.strip()]
    days = []
    for d in tool_input.get("days_of_week") or []:
        try:
            di = int(d)
        except (TypeError, ValueError):
            continue
        if 0 <= di <= 6:
            days.append(di)
    schedule = {"frequency": frequency, "times": times or ["09:00"], "days_of_week": days}
    if frequency == "weekly" and not days:
        return {"ok": False, "status": 400, "error": "A weekly task needs at least one weekday in days_of_week (0=Mon … 6=Sun)."}

    business = (tool_input.get("business") or "").strip().lower()
    if not business:
        business = active_business if (active_business and active_business != ALL_BUSINESSES) else "all"
    entity_slug = None
    if business and business != "all":
        entity_slug = to_entity_slug(business)
        if not entity_slug:
            return {"ok": False, "status": 400,
                    "error": f"Unknown business '{business}'. Valid: {sorted(BUSINESS_TO_ENTITY_SLUG)} or 'all'."}

    tz = (tool_input.get("timezone") or Config.SCHEDULER_DEFAULT_TZ).strip() or Config.SCHEDULER_DEFAULT_TZ
    enabled = tool_input.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() not in ("false", "0", "no", "off", "")
    allowed_apps = [a.strip() for a in (tool_input.get("allowed_apps") or []) if isinstance(a, str) and a.strip()]

    next_run = sched.compute_next_run(schedule, tz) if enabled else None
    task_id = tasks_db.create_task(
        entity_slug=entity_slug, name=name, prompt=prompt, schedule=schedule,
        timezone_name=tz, allowed_apps=allowed_apps, autonomy=autonomy,
        enabled=enabled, next_run_at=next_run,
    )
    if not task_id:
        return {"ok": False, "status": 500, "error": "Could not create the task (storage error)."}
    return {
        "ok": True, "status": 200, "task_id": task_id, "name": name,
        "schedule": sched.describe_schedule(schedule, tz), "business": business,
        "allowed_apps": allowed_apps or "all connected apps", "autonomy": autonomy,
        "timezone": tz, "enabled": enabled,
        "next_run_at": next_run.isoformat() if next_run else None,
        "message": (f"Scheduled task '{name}' created — visible in the Tasks tab. "
                    + ("It will run " + sched.describe_schedule(schedule, tz) + f" ({tz})."
                       if enabled else "It is paused; enable it in the Tasks tab to start it.")),
    }


def _list_scheduled_tasks(tool_input: dict[str, Any], active_business: str | None) -> dict[str, Any]:
    """List existing Scheduled Tasks, optionally scoped to a business."""
    from . import scheduler as sched
    from .db import scheduled_tasks as tasks_db

    business = (tool_input.get("business") or "").strip().lower()
    scope: str | None = None
    if business and business != "all":
        scope = to_entity_slug(business) or business
    elif active_business and active_business != ALL_BUSINESSES:
        scope = to_entity_slug(active_business) or active_business
    rows = tasks_db.list_tasks(scope)
    tasks = [{
        "task_id": t.get("task_id"), "name": t.get("name"),
        "business": t.get("business") or "all",
        "schedule": sched.describe_schedule(t.get("schedule") or {}, t.get("timezone")),
        "autonomy": t.get("autonomy"), "enabled": t.get("enabled"),
        "next_run_at": t.get("next_run_at"), "last_status": t.get("last_status"),
    } for t in rows]
    return {"ok": True, "status": 200, "count": len(tasks), "tasks": tasks}


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
        elif name == "search_business_knowledge":
            # No Maton client involved at all — pure Postgres/pgvector search.
            result = _search_business_knowledge(tool_input, active_business)
        elif name == "create_file":
            # No Maton client involved at all — pure S3 write.
            result = _create_file(tool_input, active_business)
        elif name == "create_scheduled_task":
            # No Maton client — writes a row via the scheduled-tasks store.
            result = _create_scheduled_task(tool_input, active_business)
        elif name == "list_scheduled_tasks":
            result = _list_scheduled_tasks(tool_input, active_business)
        elif name == "calendar_list_upcoming":
            # hermes.calendar resolves its own per-business Maton key internally.
            result = _calendar_list_upcoming(tool_input, active_business)
        elif name == "worldbank_data":
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
            elif name == "zoom_list_meetings":
                result = _zoom_list_meetings(client, tool_input)
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


# ---- Friendly in-progress status text --------------------------------------
# What the chat UI shows WHILE a tool call is in flight, so a live turn reads
# like a person working ("📅 Peeking at the calendar…") instead of a debug log
# ("Calling calendar_list_upcoming…"). Keyed by tool name; `maton_request` is
# one tool serving every connected app, so its message is keyed by the `app`
# argument instead, via _MATON_APP_STATUS.
_TOOL_STATUS: dict[str, str] = {
    "maton_list_connections": "🔌 Checking what's connected…",
    "maton_create_connection": "🔌 Setting up a new connection…",
    "drive_read_file": "📁 Opening that file from Drive…",
    "send_email": "📧 Sending your email…",
    "zoom_send_invite": "🎥 Sending your Zoom invite…",
    "zoom_list_meetings": "🎥 Checking your Zoom meetings…",
    "calendar_list_upcoming": "📅 Peeking at your calendar…",
    "search_business_knowledge": "🔎 Searching your knowledge base…",
    "create_file": "📝 Putting your file together…",
    "worldbank_data": "🌍 Pulling stats from the World Bank…",
    "create_scheduled_task": "🗓️ Setting up your scheduled task…",
    "list_scheduled_tasks": "🗓️ Checking your scheduled tasks…",
    "wati_list_contacts": "💬 Looking up WhatsApp contacts…",
    "wati_add_contact": "💬 Adding a WhatsApp contact…",
    "wati_update_contact_attributes": "💬 Updating contact info…",
    "wati_get_messages": "💬 Checking WhatsApp messages…",
    "wati_send_message": "💬 Sending a WhatsApp message…",
    "wati_send_file": "💬 Sending a file over WhatsApp…",
    "wati_assign_operator": "💬 Assigning a WhatsApp operator…",
    "wati_get_media": "💬 Fetching WhatsApp media…",
}

_MATON_APP_STATUS: dict[str, str] = {
    "google-mail": "📧 Sorting through Gmail…",
    "google-drive": "📁 Rifling through Google Drive…",
    "google-calendar": "📅 Peeking at the calendar…",
    "google-sheets": "📊 Opening a spreadsheet…",
    "google-docs": "📄 Reading a Google Doc…",
    "google-forms": "📋 Checking Google Forms…",
    "google-contacts": "👤 Looking up a contact…",
    "google-meet": "🎥 Checking Google Meet…",
    "notion": "📝 Flipping through Notion…",
    "zoom": "🎥 Dialing into Zoom…",
    "zoom-admin": "🎥 Dialing into Zoom…",
    "quickbooks": "💰 Crunching the QuickBooks numbers…",
    "wati": "💬 Pinging WhatsApp…",
    "fathom": "🎙️ Pulling up the meeting recording…",
    "pdf-co": "📄 Processing a PDF…",
}


def tool_status(name: str, tool_input: dict[str, Any]) -> str:
    """A short, friendly 'what I'm doing right now' line for the chat UI to
    show while this tool call is in flight — never the raw tool/app name."""
    if name == "maton_request":
        app = (tool_input or {}).get("app") or ""
        return _MATON_APP_STATUS.get(app, f"🔌 Connecting to {app or 'a connected app'}…")
    return _TOOL_STATUS.get(name, f"⚙️ Running {name.replace('_', ' ')}…")
