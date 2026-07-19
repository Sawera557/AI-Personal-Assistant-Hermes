"""Per-business S3 file listing for the Files explorer tab.

Turns the flat S3 bucket into a scoped, attributable list the frontend can
render as a folder tree. All tenant scoping happens HERE (there is no auth
layer) — a request for one business must never see another business's keys.

Key attribution reuses the canonical prefix rules in `businesses.key_business`.
The one case that helper can't resolve is `bills/{business_id}/...` (keyed by a
business *id*, not a slug), so we resolve those ids -> slugs from the DB.
"""
from __future__ import annotations

from . import storage
from .businesses import key_business, to_entity_slug


def _bills_id_to_slug() -> dict[str, str]:
    """Map `businesses.business_id` -> slug, for attributing `bills/{id}/...`
    keys. Best-effort: returns {} if the DB is unreachable."""
    try:
        from .db import bills as bills_repo

        return {
            str(b["business_id"]): b["slug"]
            for b in bills_repo.list_businesses()
            if b.get("business_id") and b.get("slug")
        }
    except Exception:
        return {}


def _attribute(key: str, bills_map: dict[str, str]) -> str | None:
    """The business slug a key belongs to, or None if unattributable."""
    biz = key_business(key)
    if biz:
        return biz
    parts = [p for p in key.split("/") if p]
    if len(parts) >= 3 and parts[0] == "bills":
        return bills_map.get(parts[1])
    return None


def _row(key: str, size, last_modified, business_slug: str | None) -> dict:
    return {
        "key": key,
        "s3_uri": f"s3://{storage.bucket()}/{key}",
        "size": size,
        "last_modified": last_modified,
        "business_slug": business_slug,
    }


def list_business_files(business_slug: str) -> list[dict]:
    """List files for one business, or every business when ``business_slug`` is
    ``"all"``. Each item: ``{key, s3_uri, size, last_modified, business_slug}``.

    Returns ``[]`` when storage is disabled.
    """
    if not storage.enabled():
        return []

    if business_slug == "all":
        bills_map = _bills_id_to_slug()
        return [
            _row(o["key"], o["size"], o["last_modified"],
                 _attribute(o["key"], bills_map))
            for o in storage.list_objects("")
        ]

    # Specific business: list only the prefixes it owns (efficient + inherently
    # scoped — we never touch another tenant's keys).
    entity = to_entity_slug(business_slug) or business_slug
    prefixes = [
        f"{entity}/",
        f"generated/{business_slug}/",
        f"drive-files/{business_slug}/",
        f"wati-media/{business_slug}/",
    ]
    bills_map = _bills_id_to_slug()
    for biz_id, slug in bills_map.items():
        if slug == business_slug:
            prefixes.append(f"bills/{biz_id}/")

    out: list[dict] = []
    seen: set[str] = set()
    for prefix in prefixes:
        for o in storage.list_objects(prefix):
            if o["key"] in seen:
                continue
            seen.add(o["key"])
            out.append(_row(o["key"], o["size"], o["last_modified"], business_slug))
    return out
