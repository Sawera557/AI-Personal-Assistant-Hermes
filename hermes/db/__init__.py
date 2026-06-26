"""Hermes Postgres layer: connection helpers + migration runner.

Postgres is the record keeper (see the project scope doc). This package owns
the connection (IAM or password auth), the SQL migrations under ``migrations/``,
and the helpers the rest of the app uses to read/write the truth.
"""
from __future__ import annotations

from .connection import connect, cursor, ping

__all__ = ["connect", "cursor", "ping"]
