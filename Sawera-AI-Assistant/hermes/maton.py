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
#
# The old code serialised the whole payload and sliced it at a byte offset. That
# destroyed the JSON structure (a truncated list came back as a dict), produced
# invalid mid-object JSON, and — worst — handed the model a body that simply
# trailed off with no in-band signal at the cut, so it would confidently
# describe content that lay past the slice it never read. The replacement below
# is STRUCTURE-PRESERVING and LOUD: it keeps container types, truncates
# individual leaf strings / over-long lists *in place*, splices an unmissable
# marker exactly where content was dropped, and hoists a `_truncation` report to
# the top of the envelope so the model reads the boundary before the data.
#
# The ceiling is also far higher than the old 20k: the common case (e.g. a long
# multi-question email) now returns COMPLETE, and only genuinely huge payloads
# are reduced — honestly.
_MAX_RESULT_CHARS = 120_000   # whole-payload ceiling (serialised chars)
_MAX_LEAF_CHARS = 40_000      # a single string field's ceiling
_MAX_LIST_ITEMS = 500         # elements kept per list before the tail is dropped
_TIMEOUT = 45

# In-band, unmissable, greppable, and ASCII-only — the dispatch layer serialises
# with json.dumps(ensure_ascii=True), so non-ASCII brackets would arrive at the
# model as \uXXXX escapes. If the model sees this it has NOT seen the omitted
# content and must not describe it.
_MARK = "<<TRUNCATED: {shown} of {total} chars shown, {omitted} unread - the rest was NOT retrieved>>"


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

        return _envelope(resp)

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

        return _envelope(resp)

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
        return _envelope(resp)

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
        return _envelope(resp)

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
        return _envelope(resp)


def _envelope(resp: requests.Response) -> dict[str, Any]:
    """Build the standard `{status, ok, data}` result.

    Truncates the body structure-preservingly and, when anything was cut, hoists
    a `_truncation` report to the TOP of the dict so the model reads the boundary
    before wading into the data.
    """
    try:
        parsed = resp.json()
    except ValueError:
        parsed = resp.text
    data, report = _truncate(parsed)
    env: dict[str, Any] = {}
    if report is not None:
        env["_truncation"] = report          # inserted first → serialised first
    env["status"] = resp.status_code
    env["ok"] = resp.ok
    env["data"] = data
    return env


def _dumps(data: Any) -> str:
    return json.dumps(data, default=str, ensure_ascii=False)


def _cut_string(s: str, path: str, cap: int, cuts: list[dict]) -> str:
    total = len(s)
    omitted = total - cap
    cuts.append({"path": path, "kind": "string", "shown": cap,
                 "total": total, "omitted": omitted})
    return s[:cap] + " " + _MARK.format(shown=cap, total=total, omitted=omitted)


def _reduce(data: Any, path: str, cuts: list[dict], leaf_cap: int, list_cap: int) -> Any:
    """Recursively shrink `data` while KEEPING its type at every level: a list
    stays an indexable list, a dict stays a dict. Over-long leaf strings get an
    in-band marker; over-long lists drop their tail. Every cut is recorded."""
    if isinstance(data, str):
        return _cut_string(data, path, leaf_cap, cuts) if len(data) > leaf_cap else data
    if isinstance(data, dict):
        return {k: _reduce(v, f"{path}.{k}", cuts, leaf_cap, list_cap) for k, v in data.items()}
    if isinstance(data, list):
        if len(data) > list_cap:
            cuts.append({"path": path, "kind": "list", "shown": list_cap,
                         "total": len(data), "omitted": len(data) - list_cap})
            data = data[:list_cap]
        return [_reduce(v, f"{path}[{i}]", cuts, leaf_cap, list_cap) for i, v in enumerate(data)]
    return data


def _truncate(data: Any) -> tuple[Any, dict | None]:
    """Structure-preserving, loud truncation.

    Returns `(payload, report)`. `payload` keeps the type and shape of the
    original — a list stays an indexable list, a dict stays a dict — with any
    over-long leaf string or over-long list cut in place and marked in-band.
    `report` is None when nothing was cut, else a dict naming every cut, which
    the caller hoists to the top of the response envelope.
    """
    if len(_dumps(data)) <= _MAX_RESULT_CHARS:
        return data, None

    leaf_cap, list_cap = _MAX_LEAF_CHARS, _MAX_LIST_ITEMS
    cuts: list[dict] = []
    reduced = _reduce(data, "$", cuts, leaf_cap, list_cap)
    # Guarantee the ceiling holds even for pathological fan-out — a huge leaf
    # string, OR thousands of medium items that each individually clear the leaf
    # cap. Tighten BOTH the per-leaf and per-list caps and re-reduce until under
    # budget. Always terminates: both halve toward their floors.
    while len(_dumps(reduced)) > _MAX_RESULT_CHARS and (leaf_cap > 500 or list_cap > 10):
        leaf_cap = max(500, leaf_cap // 2)
        list_cap = max(10, list_cap // 2)
        cuts = []
        reduced = _reduce(data, "$", cuts, leaf_cap, list_cap)

    report = {
        "truncated": True,
        "chars_omitted_total": sum(c["omitted"] for c in cuts),
        "cuts": cuts,
        "warning": (
            "This tool result was REDUCED to fit the context window. Every place "
            "content was dropped is marked in-band with a <<TRUNCATED ...>> "
            "sentinel, and the cuts are listed above. You have NOT seen the "
            "omitted content: do not describe, summarise, quote, count, or answer "
            "questions about anything past a <<TRUNCATED>> marker or beyond a "
            "listed cut. Say the data was truncated and re-request it with a "
            "narrower query, a filter, or the API's paging parameter."
        ),
    }
    return reduced, report
