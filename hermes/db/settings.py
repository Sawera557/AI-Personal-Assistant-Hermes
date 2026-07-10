"""Postgres-backed key/value store for platform-wide settings.

Tiny and generic on purpose — the first (and so far only) consumer is
hermes.models (the active agent model), but any other global toggle can reuse
this instead of adding a dedicated table.
"""
from __future__ import annotations

from .connection import cursor


def get(key: str) -> str | None:
    with cursor(commit=False) as cur:
        cur.execute("SELECT value FROM hermes_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set(key: str, value: str) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO hermes_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
            """,
            (key, value),
        )
