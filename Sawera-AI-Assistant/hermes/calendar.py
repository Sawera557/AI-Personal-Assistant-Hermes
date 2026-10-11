"""Google Calendar — list a business's own upcoming events, via Maton.

Same pattern as hermes/bills.py's Gmail/Drive scanning: one business's own
connected account, read-only, no writes. Powers the Meetings tab's Upcoming
view so the owner can see what's coming and prepare a brief ahead of time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .maton import MatonClient, MatonError

log = logging.getLogger("hermes.calendar")

_APP = "google-calendar"


def list_upcoming(business_slug: str, days: int = 14, max_results: int = 20) -> list[dict[str, Any]]:
    """Upcoming events on `business_slug`'s own primary calendar.

    Raises ValueError if the business has no Maton connection configured;
    raises RuntimeError on a Maton/network failure. Returns [] if the
    calendar simply has nothing in the window — that's not an error.
    """
    key = Config.maton_key_for_slug(business_slug)
    if not key:
        raise ValueError(f"'{business_slug}' has no Maton connection configured yet.")
    maton = MatonClient(api_key=key)

    now = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = maton.request(
            _APP, "GET", "calendar/v3/calendars/primary/events",
            query={
                "timeMin": time_min, "timeMax": time_max,
                "maxResults": max_results, "singleEvents": "true", "orderBy": "startTime",
            },
        )
    except MatonError as exc:
        raise RuntimeError(f"Calendar list failed: {exc}") from exc

    events = (resp.get("data") or {}).get("items") or []
    out: list[dict[str, Any]] = []
    for e in events:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")
        if not start:
            continue
        attendees = [
            {
                "email": a.get("email"),
                "name": a.get("displayName") or a.get("email"),
                "organizer": bool(a.get("organizer")),
            }
            for a in (e.get("attendees") or []) if a.get("email")
        ]
        out.append({
            "event_id": e.get("id"),
            "title": e.get("summary") or "(no title)",
            "start": start,
            "end": end,
            "location": e.get("location"),
            "description": e.get("description"),
            "attendees": attendees,
            "html_link": e.get("htmlLink"),
        })
    return out
