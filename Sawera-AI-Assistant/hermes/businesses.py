"""LEGACY SHIM for the old two-scheme business identity (PLAN.md §2.0).

Gandi's Hermes split business identity across two tables — `businesses`
(slugs like "afk") and `entities` (slugs like "afro_farm") — and bridged them
with hard-coded dicts here, plus per-business env keys in hermes/config.py.
Sawera's assistant replaces both with dynamic **Projects**: every project is
one `entities` row and its slug is the single handle (create/update/archive
from the UI, no code changes — see `hermes/db/migrations/0023_projects.sql`
and `hermes/projects.py`).

Everything below keeps the ~68 old call sites working: the public names are
now proxies onto `hermes.projects`, and the three `_LEGACY_*` dicts survive
ONLY as the fallback for runs with no database (local dev, the offline test
suite). PLAN §12 deletes this file once the old call sites are migrated.

New code: use `hermes.projects` directly, never this module.
"""
from __future__ import annotations

from collections.abc import Mapping

from . import projects as _projects

_LEGACY_BUSINESS_TO_ENTITY: dict[str, str] = {
    "afk": "afro_farm",
    "gak": "gak_properties",
    "gif": "gif_properties",
    "gakc": "gak_commodities",
    "nh": "nour_hammam",
    "per": "personal",
}
_LEGACY_ENTITY_TO_BUSINESS: dict[str, str] = {
    v: k for k, v in _LEGACY_BUSINESS_TO_ENTITY.items()
}
_LEGACY_DISPLAY_NAMES: dict[str, str] = {
    "afk": "Afro Farm",
    "gak": "GAK Properties",
    "gif": "GIF Properties",
    "gakc": "GAK Commodities",
    "nh": "Nour Hammam",
    "per": "Personal & Family",
    "comfort_pca": "Comfort PCA",
}


class _DynamicMap(Mapping):
    """A Mapping facade answered live by the project registry (or the legacy
    fallback), so `sorted(map)`, `map[k]` and `k in map` always see current
    projects without every call site caching a stale copy."""

    def __init__(self, resolve):
        self._resolve = resolve

    def _data(self) -> dict:
        try:
            return self._resolve()
        except Exception:  # noqa: BLE001 — fail safe to the legacy statics
            return {}

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


BUSINESS_TO_ENTITY_SLUG: Mapping[str, str] = _DynamicMap(_projects.identity_map)
ENTITY_TO_BUSINESS_SLUG: Mapping[str, str] = _DynamicMap(_projects.identity_map)
BUSINESS_DISPLAY_NAMES: Mapping[str, str] = _DynamicMap(_projects.display_names)


def to_entity_slug(business_slug: str | None) -> str | None:
    """businesses.slug -> entities.slug. With projects these are the same key
    (identity); this shim keeps the old call sites working."""
    return _projects.to_entity_slug(business_slug)


def to_business_slug(entity_slug: str | None) -> str | None:
    """entities.slug -> businesses.slug. Identity under projects."""
    return _projects.to_business_slug(entity_slug)


def key_business(key: str) -> str | None:
    """The project slug an S3 key belongs to, by the prefix conventions
    (`wati-media/<slug>/…`, `drive-files/<slug>/…`, `generated/<slug>/…`, or a
    bare `<slug>/…` KB key)."""
    return _projects.key_business(key)
