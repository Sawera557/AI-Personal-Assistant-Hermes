"""Create the Hermes Pinecone index and verify the namespace plan.

Creates one serverless index (idempotent), then prints the namespace each of the
8 businesses will use, plus the private user-memory namespace pattern. Pinecone
creates namespaces lazily on first upsert, so this verifies config + connectivity
rather than pre-creating empty namespaces.

Needs PINECONE_API_KEY in .env. Usage:
    python scripts/init_pinecone.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes.config import Config  # noqa: E402  (loads .env)
from hermes import pinecone_store as ps  # noqa: E402

# The 8 business slugs (must match entities.slug in 0002_seed.sql).
ENTITY_SLUGS = [
    "gak_properties", "gif_properties", "comfort_services", "afro_farm",
    "gak_commodities", "nour_hammam", "personal", "kenya_ventures",
]


def main() -> int:
    _ = Config  # ensure .env loaded
    print(f"Ensuring index '{ps.INDEX_NAME}' ({ps.CLOUD}/{ps.REGION}, "
          f"{ps.EMBED_DIM} dims, cosine)...")
    info = ps.ensure_index()
    status = info.get("status", {})
    print(f"  ready={status.get('ready')} host={info.get('host')}")

    print("\nBusiness namespaces (created lazily on first upsert):")
    for slug in ENTITY_SLUGS:
        print(f"  {ps.entity_namespace(slug)}")
    print("\nPrivate per-user memory namespace pattern:")
    print(f"  {ps.user_namespace('<user_id>')}  (never shared)")

    print("\nSanity check — embedding one string with "
          f"{ps.EMBED_MODEL}...")
    vec = ps.embed(["hello hermes"])[0]
    ok = len(vec) == ps.EMBED_DIM
    print(f"  got {len(vec)}-dim vector (expected {ps.EMBED_DIM}): "
          f"{'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
