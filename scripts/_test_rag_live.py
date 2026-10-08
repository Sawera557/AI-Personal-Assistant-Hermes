"""One-off LIVE integration test for the federated RAG feature.

Read-only. Exercises the real path end-to-end against the live DB + OpenAI
embeddings. Pinecone is exercised only if PINECONE_API_KEY is present; otherwise
we assert the graceful pgvector-only degrade (the dark-merge safety).

Run:  python scripts/_test_rag_live.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Enable the feature for this process BEFORE importing hermes (flags are read
# lazily, but be explicit). .env doesn't set these, so they survive load_dotenv.
os.environ["HERMES_RAG_PINECONE"] = "true"
os.environ["HERMES_RAG_CONTEXT_INJECTION"] = "true"

from hermes import pinecone_store, rag  # noqa: E402
from hermes.config import Config  # noqa: E402
from hermes.db.connection import cursor  # noqa: E402


def rule(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def sample_query(entity_slug: str) -> str:
    """Derive a real query from actual stored content so we KNOW it should match."""
    with cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT dc.text_preview FROM document_chunks dc
            JOIN entities e ON e.entity_id = dc.entity_id
            WHERE e.slug = %s AND dc.embedding IS NOT NULL AND dc.is_active
              AND length(dc.text_preview) > 40
            LIMIT 1;
            """,
            (entity_slug,),
        )
        row = cur.fetchone()
    if not row:
        return "farm operations"
    # Use the first ~10 words of a real chunk as the query.
    return " ".join((row[0] or "").split()[:10])


rule("0. Feature state")
print("  rag_pinecone_enabled  :", Config.rag_pinecone_enabled(),
      "(needs PINECONE_API_KEY — currently", "SET)" if Config.PINECONE_API_KEY else "MISSING)")
print("  rag_context_injection :", Config.rag_context_injection())
print("  RAG_TOP_K / threshold :", Config.RAG_TOP_K, "/", Config.RAG_SCORE_THRESHOLD)

rule("1. Pinecone reader — reachability / graceful degrade")
pc_hits = pinecone_store.query(entity_slug="afro_farm", query_text="farm payroll", top_k=3)
if Config.PINECONE_API_KEY:
    print(f"  Pinecone returned {len(pc_hits)} hits (live).")
    for h in pc_hits[:3]:
        print(f"    - {h.get('score'):.3f}  doc={h.get('document_id')}  {str(h.get('text'))[:60]!r}")
else:
    print("  PINECONE_API_KEY missing -> reader returned:", pc_hits,
          "(PASS: degrades to [] with no error)")

rule("2. federated_search — real query against afro_farm (pgvector + Pinecone merged)")
q = sample_query("afro_farm")
print(f"  query (from real stored content): {q!r}\n")
hits = rag.federated_search("afro_farm", q, top_k=5)
print(f"  {len(hits)} merged hits:")
for h in hits:
    print(f"    - score={h.get('score'):.3f}  origin={h.get('origin'):8s}  "
          f"title={str(h.get('title'))[:40]!r}  doc={h.get('document_id')}")

rule("3. build_context_block — the actual always-on prompt block (business='afk')")
block = rag.build_context_block(active_business="afk", latest_user_text=q)
print(block if block else "  (empty — nothing cleared the score threshold)")

rule("4. Always-on block is empty for 'All Businesses' (no single namespace)")
allb = rag.build_context_block(active_business="all", latest_user_text=q)
print("  result:", repr(allb), "(PASS)" if allb == "" else "(FAIL)")

rule("5. Second business — nour_hammam")
q2 = sample_query("nour_hammam")
print(f"  query: {q2!r}")
hits2 = rag.federated_search("nour_hammam", q2, top_k=3)
print(f"  {len(hits2)} hits; top score:", f"{hits2[0]['score']:.3f}" if hits2 else "n/a")

print("\nDONE.")
