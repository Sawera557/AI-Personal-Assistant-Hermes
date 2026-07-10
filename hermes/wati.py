"""Typed client for the WATI WhatsApp Business API, proxied through Maton.

WATI's native REST API (https://docs.wati.io/reference) is reached through the
Maton gateway with `app="wati"`. These are thin, purpose-built wrappers around
the handful of endpoints Hermes exposes as dedicated agent tools (see
`hermes.tools`), so the model doesn't have to hand-construct raw
`maton_request` calls with the right WATI paths/params for common tasks.
"""
from __future__ import annotations

import json
from typing import Any

from .maton import MatonClient


class WatiClient:
    def __init__(self, maton: MatonClient):
        self._maton = maton

    # ---- Contacts ----------------------------------------------------------

    def list_contacts(
        self,
        name: str | None = None,
        attribute_filters: list[dict[str, Any]] | None = None,
        created_date: str | None = None,
        page_size: int | None = None,
        page_number: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if name:
            query["name"] = name
        if attribute_filters:
            query["attribute"] = json.dumps(attribute_filters, default=str)
        if created_date:
            query["createdDate"] = created_date
        if page_size:
            query["pageSize"] = page_size
        if page_number:
            query["pageNumber"] = page_number
        return self._maton.request("wati", "GET", "/api/v1/getContacts", query=query)

    def add_contact(
        self,
        whatsapp_number: str,
        name: str | None = None,
        custom_params: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if custom_params:
            body["customParams"] = custom_params
        return self._maton.request(
            "wati", "POST", f"/api/v1/addContact/{whatsapp_number}", body=body
        )

    def update_contact_attributes(
        self, whatsapp_number: str, custom_params: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._maton.request(
            "wati",
            "POST",
            f"/api/v1/updateContactAttributes/{whatsapp_number}",
            body={"customParams": custom_params},
        )

    # ---- Messages -----------------------------------------------------------

    def get_messages(
        self,
        whatsapp_number: str,
        page_size: int | None = None,
        page_number: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if page_size:
            query["pageSize"] = page_size
        if page_number:
            query["pageNumber"] = page_number
        return self._maton.request(
            "wati", "GET", f"/api/v1/getMessages/{whatsapp_number}", query=query
        )

    def send_session_message(self, whatsapp_number: str, message_text: str) -> dict[str, Any]:
        """Send a free-text message. Only works within WATI's 24-hour customer
        session window (i.e. the contact messaged in the last 24h) — outside
        that window WATI requires an approved template message instead."""
        return self._maton.request(
            "wati",
            "POST",
            f"/api/v1/sendSessionMessage/{whatsapp_number}",
            query={"messageText": message_text},
        )

    def send_session_file(
        self,
        whatsapp_number: str,
        filename: str,
        media_type: str,
        raw_bytes: bytes,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Send a file (PDF, image, etc). Same 24-hour session window rule as
        `send_session_message`."""
        files = {"file": (filename, raw_bytes, media_type or "application/octet-stream")}
        data = {"caption": caption} if caption else None
        return self._maton.request_multipart(
            "wati",
            "POST",
            f"/api/v1/sendSessionFile/{whatsapp_number}",
            files=files,
            data=data,
        )

    # ---- Operators ------------------------------------------------------------

    def assign_operator(self, whatsapp_number: str, email: str) -> dict[str, Any]:
        return self._maton.request(
            "wati",
            "POST",
            "/api/v1/assignOperator",
            query={"whatsappNumber": whatsapp_number, "email": email},
        )

    # ---- Media -----------------------------------------------------------

    def download_media(self, file_name: str) -> dict[str, Any]:
        """Fetch the full raw bytes of a media file (not base64/truncated) so
        the caller can persist it (e.g. to S3) rather than inlining it."""
        return self._maton.download_binary(
            "wati", "GET", "/api/v1/getMedia", query={"fileName": file_name}
        )
