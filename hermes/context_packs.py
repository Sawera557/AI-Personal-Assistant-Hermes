"""Context-pack loader — assembles the always-present standing-facts block the
agent injects into the system prompt each turn (Plan task B2).

Two packs concatenate: the 'global' pack (owner + portfolio rules) and the
locked business's pack. In 'all businesses' mode (active_business None/'all')
only the global pack is used — a deliberate v1 simplification that mirrors how
memory retrieval treats 'all'.

Backed by the context_packs table (migration 0017); a short in-process TTL cache
keeps the per-turn cost ~0. EVERY failure path returns "" — a pack problem must
never break a turn.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .config import float_env, int_env
from .db import context_packs as repo

log = logging.getLogger("hermes.context_packs")

# Combined global+business cap (chars). The business pack tail is trimmed first;
# the global pack is never truncated. Curated packs sit well under this.
_MAX_PACK_CHARS = int_env("CONTEXT_PACK_MAX_CHARS", 40000)
_CACHE_TTL = float_env("CONTEXT_PACK_CACHE_TTL", 300.0)  # seconds

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _cached_pack(slug: str) -> str:
    """Fetch one pack's content_md with a TTL cache. Empty string if missing."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(slug)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]
    row = repo.get_pack(slug)  # fetch outside the lock
    content = (row or {}).get("content_md", "") or ""
    with _lock:
        _cache[slug] = (now, content)
    return content


def invalidate() -> None:
    """Drop the in-process cache. Note: the loader that writes packs runs in a
    separate process from the server, so after loading packs you still restart
    the server or wait out the TTL for a running server to see them."""
    with _lock:
        _cache.clear()


def packs_for(active_business: str | None) -> str:
    """Global pack + (optional) locked-business pack, ready to inject. Returns ""
    on any failure or when nothing is stored yet."""
    try:
        parts: list[str] = []
        g = _cached_pack("global")
        if g:
            parts.append(g)
        slug = (active_business or "").strip().lower()
        if slug and slug != "all":
            b = _cached_pack(slug)
            if b:
                parts.append(b)
        if not parts:
            return ""
        block = "\n\n---\n\n".join(parts)
        if len(block) > _MAX_PACK_CHARS:
            if len(parts) > 1:
                keep = max(0, _MAX_PACK_CHARS - len(parts[0]) - 20)
                block = parts[0] + "\n\n---\n\n" + parts[1][:keep].rstrip() + "\n[pack truncated]"
            else:
                block = block[:_MAX_PACK_CHARS].rstrip() + "\n[pack truncated]"
        return block
    except Exception as exc:  # noqa: BLE001 — never break a turn
        log.warning("packs_for(%s) failed: %s", active_business, exc)
        return ""
