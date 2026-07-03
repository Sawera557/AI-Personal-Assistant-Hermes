"""Fathom — best-effort lookup of a meeting's recording link, via Maton.

Fathom's exact Maton catalog path isn't confirmed (same caveat as the
ingestion service's fathom connector), so this tries several plausible list
paths and several plausible link field names, matching by title first, then
falling back to the most recent recording if nothing matches by title.
Returns None on any failure or no match — callers must treat this as
best-effort and always offer a manual "paste the link" fallback in the UI.
"""
from __future__ import annotations

import logging
from typing import Any

from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.fathom")

_APP = "fathom"
_LIST_PATHS = ["meetings", "v1/meetings", "calls", "v1/calls"]
_LINK_FIELDS = ("url", "share_url", "recording_url", "link", "meeting_url")
_TITLE_FIELDS = ("title", "name", "subject", "meeting_title")


def _rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "meetings", "calls", "results", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _link_of(row: dict) -> str | None:
    for f in _LINK_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _title_of(row: dict) -> str:
    for f in _TITLE_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def find_recording_link(business_slug: str, *, title_hint: str | None = None) -> str | None:
    """Best-effort: find a Fathom recording link, preferring one whose title
    matches `title_hint`. Returns None (never raises) if nothing is found —
    the API shape here is unverified, so failure must be silent and cheap."""
    from .config import Config  # local import: avoid a cycle with config at module load

    key = Config.maton_key_for_slug(business_slug)
    if not key:
        return None
    maton = MatonClient(api_key=key)

    for path in _LIST_PATHS:
        try:
            resp = maton.request(_APP, "GET", path, query={"limit": 20})
        except MatonError:
            continue
        if not resp.get("ok"):
            continue
        rows = _rows(resp.get("data"))
        if not rows:
            continue

        if title_hint:
            hint = title_hint.lower()
            for row in rows:
                if hint in _title_of(row).lower():
                    link = _link_of(row)
                    if link:
                        return link

        # No title match (or no hint) — fall back to the most recent recording
        # on this path, since Fathom lists are typically newest-first.
        for row in rows:
            link = _link_of(row)
            if link:
                return link

    return None
