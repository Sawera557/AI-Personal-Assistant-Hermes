"""Per-session token-usage / cost aggregation for the Usage tab.

Reads the ai_runs rows written by conversations.record_turn (one per agent
turn) and rolls them up per chat session, plus grand totals. Plain functions
over raw SQL, mirroring hermes.db.settings; degrades to empty on any DB error
so the tab never hard-fails.
"""
from __future__ import annotations

import logging
from typing import Any

from .connection import cursor

log = logging.getLogger("hermes.db.usage")

_EMPTY: dict[str, Any] = {
    "totals": {"input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost_usd": 0.0},
    "sessions": [],
}


def session_usage(
    entity_slug: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Usage grouped by chat session (newest first) plus grand totals.

    `entity_slug` (a business slug) optionally scopes the report to one
    business; omit or pass "all"/None for every session. `start`/`end` are
    optional ISO-8601 timestamps bounding `ai_runs.created_at` (half-open:
    start <= created_at < end); omit either side for an open bound.
    """
    conds: list[str] = []
    params: list[Any] = []
    if entity_slug and entity_slug != "all":
        conds.append("r.entity_id = (SELECT entity_id FROM entities WHERE slug = %s)")
        params.append(entity_slug)
    if start:
        conds.append("r.created_at >= %s::timestamptz")
        params.append(start)
    if end:
        conds.append("r.created_at < %s::timestamptz")
        params.append(end)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    # cost lives in ai_runs.metadata->>'cost_usd' (a JSON number) so the feature
    # needs no schema migration; input/output tokens are base 0001 columns.
    sql = f"""
        SELECT r.session_id,
               s.title,
               s.updated_at,
               COALESCE(SUM(r.input_tokens), 0)                                   AS input_tokens,
               COALESCE(SUM(r.output_tokens), 0)                                  AS output_tokens,
               COALESCE(SUM(NULLIF(r.metadata->>'cost_usd', '')::numeric), 0)     AS cost_usd,
               COUNT(*)                                                           AS turns,
               (array_agg(r.model_used ORDER BY r.created_at DESC))[1]            AS model
          FROM ai_runs r
          LEFT JOIN hermes_sessions s ON s.session_id = r.session_id
          {where}
         GROUP BY r.session_id, s.title, s.updated_at
         ORDER BY s.updated_at DESC NULLS LAST;
    """
    try:
        with cursor(commit=False) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — the tab must never hard-fail
        log.warning("session_usage failed: %s", exc)
        return {"totals": dict(_EMPTY["totals"]), "sessions": []}

    sessions: list[dict[str, Any]] = []
    ti = to = 0
    tc = 0.0
    for sid, title, updated, inp, outp, cost, turns, model in rows:
        inp, outp, cost = int(inp or 0), int(outp or 0), float(cost or 0)
        ti += inp
        to += outp
        tc += cost
        sessions.append({
            "session_id": str(sid),
            "title": title or "New conversation",
            "updated_at": updated.isoformat() if updated else None,
            "model": model,
            "input_tokens": inp,
            "output_tokens": outp,
            "tokens": inp + outp,
            "cost_usd": round(cost, 6),
            "turns": int(turns or 0),
        })

    return {
        "totals": {
            "input_tokens": ti,
            "output_tokens": to,
            "tokens": ti + to,
            "cost_usd": round(tc, 6),
        },
        "sessions": sessions,
    }
