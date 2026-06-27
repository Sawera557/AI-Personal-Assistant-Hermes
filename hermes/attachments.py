"""In-memory attachment store keyed by attachment id.

Mirrors hermes.memory: swap this for Redis / object storage / a database to
persist across restarts and share across instances. The public interface
(add / block / meta / remove) is all the API depends on.

Attachments are scoped by `entity_id` so one tenant can never read another's
uploads — every lookup requires the same entity_id the file was uploaded with.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

# Public metadata fields (everything except the heavy Claude content block).
_META_FIELDS = ("attachment_id", "filename", "type", "bytes", "entity_id")


def _norm(entity_id: str | None) -> str:
    return (entity_id or "").strip()


class AttachmentStore:
    def __init__(self, max_items: int = 500):
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._max_items = max_items

    def add(
        self,
        entity_id: str | None,
        filename: str,
        kind: str,
        size: int,
        block: dict[str, Any],
    ) -> dict[str, Any]:
        """Store a pre-built Claude content block and return its metadata record."""
        attachment_id = uuid.uuid4().hex
        record = {
            "attachment_id": attachment_id,
            "entity_id": _norm(entity_id),
            "filename": filename,
            "type": kind,
            "bytes": size,
            "block": block,
        }
        with self._lock:
            # Bound memory growth: drop the oldest attachment (FIFO) when full.
            if len(self._items) >= self._max_items:
                self._items.pop(next(iter(self._items)))
            self._items[attachment_id] = record
        return record

    def _owned(self, attachment_id: str, entity_id: str | None) -> dict[str, Any] | None:
        rec = self._items.get(attachment_id)
        if rec is None or rec["entity_id"] != _norm(entity_id):
            return None
        return rec

    def block(self, attachment_id: str, entity_id: str | None) -> dict[str, Any] | None:
        """Return the stored Claude content block, or None if missing/not owned."""
        with self._lock:
            rec = self._owned(attachment_id, entity_id)
            return rec["block"] if rec else None

    def meta(self, attachment_id: str, entity_id: str | None) -> dict[str, Any] | None:
        """Return public metadata (no content block), or None if missing/not owned."""
        with self._lock:
            rec = self._owned(attachment_id, entity_id)
            return {k: rec[k] for k in _META_FIELDS} if rec else None

    def remove(self, attachment_id: str, entity_id: str | None) -> bool:
        """Delete an attachment. Returns True if it existed and was owned."""
        with self._lock:
            if self._owned(attachment_id, entity_id) is None:
                return False
            self._items.pop(attachment_id, None)
            return True


store = AttachmentStore()
