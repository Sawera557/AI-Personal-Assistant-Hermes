"""Harvest REAL retrievable facts per business, to build accurate test Q&A.

Runs the live federated retriever for candidate questions across each business
and prints what surfaces (score, origin=pgvector|pinecone, title, snippet), so
the test questions we hand the user have answers that actually exist in the data.
Read-only. Run:  PYTHONUTF8=1 python scripts/_harvest_qa.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HERMES_RAG_PINECONE"] = "true"
os.environ["HERMES_RAG_CONTEXT_INJECTION"] = "true"

from hermes.businesses import to_entity_slug  # noqa: E402
from hermes.rag import federated_search  # noqa: E402

PROBES = {
    "afk": ["supervisor payroll and arrears", "staff performance and reporting gaps", "dried chili stock"],
    "gak": ["rent arrears and tenants", "lease terms 720 E Lake"],
    "gif": ["rent and tenants", "property leases"],
    "gakc": ["commodity trading deals", "buyers and pricing"],
    "nh": ["spa buildout and vendors", "launch timeline and fees"],
    "per": ["father care and appointments", "prayer times scheduling"],
}


def snip(s, n=130):
    return " ".join((s or "").split())[:n]


for biz, queries in PROBES.items():
    ent = to_entity_slug(biz)
    print("\n" + "#" * 74 + f"\n# {biz}  ({ent})\n" + "#" * 74)
    for q in queries:
        try:
            hits = federated_search(ent, q, top_k=3)
        except Exception as e:  # noqa: BLE001
            print(f"  ? {q!r}: ERROR {e}")
            continue
        origins = {h.get("origin") for h in hits}
        print(f"\n  Q: {q!r}   ({len(hits)} hits, origins={origins or '{}'})")
        for h in hits:
            sc = h.get("score")
            print(f"    - {sc:.3f} [{h.get('origin'):8s}] {str(h.get('title'))[:44]!r}")
            print(f"        {snip(h.get('snippet'))}")

print("\nDONE.")
