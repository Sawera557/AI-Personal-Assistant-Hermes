"""Thin client for the Maton API Gateway.

Maton proxies native third-party API calls and injects the managed OAuth token:

    https://api.maton.ai/{app}/{native-api-path}
    Authorization: Bearer {MATON_API_KEY}

It also exposes connection-management endpoints under /connections.
Docs: https://github.com/maton-ai/api-gateway-skill
"""
from __future__ import annotations

import json
from typing import Any

import requests

from .config import Config

# Cap how much of a response body we feed back into the model's context.
_MAX_RESULT_CHARS = 20_000
_TIMEOUT = 45
_SENSITIVE_CONNECTION_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "auth_url",
    "client_secret",
    "connect_url",
    "connection_url",
    "key",
    "link",
    "password",
    "redirect_url",
    "refresh_token",
    "secret",
    "session_token",
    "token",
    "url",
}
_SENSITIVE_QUERY_MARKERS = (
    "access_token=",
    "api_key=",
    "authorization=",
    "refresh_token=",
    "session_token=",
    "token=",
)


class MatonError(RuntimeError):
    pass


class MatonClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or Config.MATON_API_KEY
        self.base_url = (base_url or Config.MATON_BASE_URL).rstrip("/")

    def _headers(self, connection_id: str | None = None) -> dict[str, str]:
        if not self.api_key:
            raise MatonError("MATON_API_KEY is not configured.")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if connection_id:
            headers["Maton-Connection"] = connection_id
        return headers

    # ---- Generic gateway passthrough --------------------------------------

    def request(
        self,
        app: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Proxy a native API call for `app` through Maton.

        Returns a JSON-serialisable dict: {status, ok, data}.
        """
        method = method.upper()
        url = f"{self.base_url}/{app.strip('/')}/{path.lstrip('/')}"
        headers = self._headers(connection_id)
        if body is not None and method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["Content-Type"] = "application/json"

        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=query or None,
                json=body if body is not None else None,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error calling Maton: {exc}") from exc

        return {
            "status": resp.status_code,
            "ok": resp.ok,
            "data": _parse_body(resp),
        }

    # ---- Connection management --------------------------------------------

    def list_connections(
        self, app: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if app:
            query["app"] = app
        if status:
            query["status"] = status
        try:
            resp = requests.get(
                f"{self.base_url}/connections",
                headers=self._headers(),
                params=query or None,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error listing connections: {exc}") from exc
        return {"status": resp.status_code, "ok": resp.ok, "data": _parse_body(resp)}

    def create_connection(self, app: str) -> dict[str, Any]:
        """Start an OAuth connection. Response contains a `url` to open in a browser."""
        try:
            resp = requests.post(
                f"{self.base_url}/connections",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"app": app},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error creating connection: {exc}") from exc
        return {"status": resp.status_code, "ok": resp.ok, "data": _parse_body(resp)}


def sanitize_connection_payload(data: Any, include_authorization_url: bool = False) -> Any:
    """Remove connector-management links and token-bearing fields from Maton data."""
    return _sanitize_connection_payload(data, include_authorization_url, "")


def _sanitize_connection_payload(
    data: Any, include_authorization_url: bool, key_name: str
) -> Any:
    key = key_name.lower()
    if isinstance(data, dict):
        return {
            item_key: _sanitize_connection_payload(
                item_value, include_authorization_url, str(item_key)
            )
            for item_key, item_value in data.items()
        }
    if isinstance(data, list):
        return [
            _sanitize_connection_payload(item, include_authorization_url, key_name)
            for item in data
        ]
    if key in _SENSITIVE_CONNECTION_KEYS:
        if include_authorization_url and key in {"url", "auth_url", "connect_url", "connection_url"}:
            return data
        return "[REDACTED]"
    if isinstance(data, str) and any(marker in data.lower() for marker in _SENSITIVE_QUERY_MARKERS):
        return data if include_authorization_url else "[REDACTED]"
    return data


def _parse_body(resp: requests.Response) -> Any:
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return _truncate(data)


def _truncate(data: Any) -> Any:
    """Keep tool results from blowing up the context window."""
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    if len(text) <= _MAX_RESULT_CHARS:
        return data
    return {
        "_truncated": True,
        "_note": f"Response truncated to {_MAX_RESULT_CHARS} characters.",
        "preview": text[:_MAX_RESULT_CHARS],
    }
