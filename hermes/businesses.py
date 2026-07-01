"""Bridges the two business-identity schemes in this app.

`businesses` (Postgres table, powers Bills — slugs like "afk", "gak") and
`entities` (powers chat history + document search — slugs like "afro_farm",
"gak_properties") were built by separate features and never reconciled. This
module is the single place that maps between them, so the rest of the app can
talk in `businesses.slug` (what the frontend's business dropdown sends) and
still reach `entities`-keyed storage correctly.

Two `entities` rows — "comfort_services" and "kenya_ventures" — are archived
and intentionally excluded here; they have no corresponding business.
"""
from __future__ import annotations

BUSINESS_TO_ENTITY_SLUG: dict[str, str] = {
    "afk": "afro_farm",
    "gak": "gak_properties",
    "gif": "gif_properties",
    "gakc": "gak_commodities",
    "nh": "nour_hammam",
    "per": "personal",
}

ENTITY_TO_BUSINESS_SLUG: dict[str, str] = {
    v: k for k, v in BUSINESS_TO_ENTITY_SLUG.items()
}


def to_entity_slug(business_slug: str | None) -> str | None:
    """businesses.slug -> entities.slug (or None if unmapped/blank)."""
    if not business_slug:
        return None
    return BUSINESS_TO_ENTITY_SLUG.get(business_slug)


def to_business_slug(entity_slug: str | None) -> str | None:
    """entities.slug -> businesses.slug (or None if unmapped/blank/archived)."""
    if not entity_slug:
        return None
    return ENTITY_TO_BUSINESS_SLUG.get(entity_slug)
