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

    def request_multipart(
        self,
        app: str,
        method: str,
        path: str,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Like `request()`, but for endpoints that take multipart/form-data
        (e.g. uploading a file). `files` is `{field: (filename, bytes, content_type)}`.
        """
        method = method.upper()
        url = f"{self.base_url}/{app.strip('/')}/{path.lstrip('/')}"
        headers = self._headers(connection_id)  # no Content-Type: requests sets the boundary
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=query or None,
                files=files,
                data=data or None,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error calling Maton: {exc}") from exc

        return {
            "status": resp.status_code,
            "ok": resp.ok,
            "data": _parse_body(resp),
        }

    def download_binary(
        self,
        app: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a response body in full, uninterpreted (e.g. downloading media).

        Returns `{status, ok, content_type, raw}` — `raw` is the exact response
        bytes. Callers must check `ok` before treating `raw` as file content:
        a non-2xx response's bytes are just the (usually JSON) error body.
        """
        method = method.upper()
        url = f"{self.base_url}/{app.strip('/')}/{path.lstrip('/')}"
        headers = self._headers(connection_id)
        try:
            resp = requests.request(
                method, url, headers=headers, params=query or None, timeout=_TIMEOUT
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error calling Maton: {exc}") from exc
        return {
            "status": resp.status_code,
            "ok": resp.ok,
            "content_type": resp.headers.get("Content-Type", ""),
            "raw": resp.content,
        }

    def upload_binary(
        self,
        app: str,
        method: str,
        path: str,
        data: bytes,
        content_type: str,
        query: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a raw binary request body (e.g. a Google Drive simple media
        upload to `upload/drive/v3/files?uploadType=media`). `data` is forwarded
        verbatim as the body with the given `content_type` — no JSON wrapping,
        no multipart boundary. Returns `{status, ok, data}` like `request()`.
        """
        method = method.upper()
        url = f"{self.base_url}/{app.strip('/')}/{path.lstrip('/')}"
        headers = self._headers(connection_id)
        headers["Content-Type"] = content_type or "application/octet-stream"
        try:
            resp = requests.request(
                method, url, headers=headers, params=query or None,
                data=data, timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MatonError(f"Network error calling Maton: {exc}") from exc
        return {"status": resp.status_code, "ok": resp.ok, "data": _parse_body(resp)}

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
