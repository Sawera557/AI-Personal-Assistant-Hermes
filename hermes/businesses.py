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
    # Comfort PCA (home personal-care / PHI) — reuses the existing
    # comfort_services entity; ingested to Pinecone per explicit owner
    # authorization (the ingestion PHI cloud-exemption). Its Pinecone namespace
    # is entity__comfort_services.
    "comfort_pca": "comfort_services",
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


def key_business(key: str) -> str | None:
    """The business slug an S3 key belongs to, by our prefix conventions:

    * ``wati-media/{business}/...``, ``drive-files/{business}/...`` and
      ``generated/{business}/...`` are keyed by the *business* slug (afk, gak, ...)
      — see `_wati_get_media`/`_drive_read_file`/`_create_file`. The `generated/`
      case is what lets Hermes read back a file it created with `create_file`.
    * A bare ``{entity}/...`` KB-document key is keyed by the *entity* slug
      (afro_farm, ...); map it back to its business slug.

    Returns None for anything that doesn't resolve to a known business (the
    `unsorted/` hole, `bills/...` — keyed by a business *id*, not slug, so
    resolve those separately — `generated/general/...`, or an unknown tenant).
    """
    parts = [p for p in key.split("/") if p]
    if not parts:
        return None
    if parts[0] in ("wati-media", "drive-files", "generated"):
        seg = parts[1] if len(parts) >= 3 else None
        return seg if seg in BUSINESS_TO_ENTITY_SLUG else None
    return to_business_slug(parts[0])
