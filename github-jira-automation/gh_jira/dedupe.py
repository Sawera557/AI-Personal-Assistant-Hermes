"""Delivery-level idempotency so a redelivered webhook does not double-comment.

Every GitHub delivery carries a unique `X-GitHub-Delivery` uuid, and the same id
is reused when a delivery is replayed from the webhook's "Redeliver" button or
by an at-least-once retry. Recording processed ids makes the handler idempotent.

Degradation is deliberate and one-directional: if the database is unreachable,
`already_processed` returns False and the update is posted anyway. The cost of
that is an occasional duplicate comment on a replay; the cost of the opposite
default is silently swallowing real pushes whenever the DB blinks.

Backed by `github_jira_deliveries` (hermes/db/migrations/0019_*.sql).
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("gh_jira.dedupe")


def _cursor():
    from hermes.db import cursor  # deferred: keeps this importable with no DB

    return cursor()


def already_processed(delivery_id: str) -> bool:
    """True only if this delivery is recorded as handled. False on any error."""
    if not delivery_id:
        return False
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT 1 FROM github_jira_deliveries WHERE delivery_id = %s;",
                (delivery_id,),
            )
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 — see module docstring
        log.debug("Dedupe lookup unavailable; processing %s anyway", delivery_id, exc_info=True)
        return False


def record(
    delivery_id: str,
    event: str,
    repo: str,
    issue_keys: list[str],
    result: dict[str, Any],
) -> None:
    """Note a handled delivery. Best-effort; never raises into the request path."""
    if not delivery_id:
        return
    try:
        with _cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_jira_deliveries
                    (delivery_id, event, repo, issue_keys, result)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (delivery_id) DO NOTHING;
                """,
                (delivery_id, event, repo, issue_keys, json.dumps(result)),
            )
    except Exception:  # noqa: BLE001
        log.debug("Could not record delivery %s", delivery_id, exc_info=True)
