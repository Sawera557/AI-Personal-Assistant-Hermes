"""One-off LIVE test: does Hermes 'remember everything'?

Assembles the ACTUAL system prompt the agent builds each turn and reports which
memory/knowledge layers are populated, for a real business + a real query.
Read-only. Run:  PYTHONUTF8=1 python scripts/_test_memory_full.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes.agent import _system_prompt  # noqa: E402
from hermes.config import Config  # noqa: E402
from hermes.db.connection import cursor  # noqa: E402
from hermes.memory_retrieve import build_memory_block  # noqa: E402
from hermes.rag import build_context_block  # noqa: E402

BUSINESS = "afk"
QUERY = "What should I focus on for farm payroll and staff performance this month?"


def rule(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def head(s, n=600):
    s = s or ""
    return s if len(s) <= n else s[:n] + " …"


rule("Layer 0 — durable persistent memory (pgvector `memories` table)")
with cursor(commit=False) as cur:
    cur.execute("SELECT scope_type, count(*) FROM memories WHERE status='active' GROUP BY scope_type ORDER BY 2 DESC;")
    rows = cur.fetchall()
    total = sum(r[1] for r in rows)
    print(f"  {total} active memories:")
    for r in rows:
        print(f"    {r[0]:10s} {r[1]}")
    cur.execute("SELECT memory_type, text FROM memories WHERE status='active' AND scope_type='owner' ORDER BY updated_at DESC LIMIT 3;")
    print("  sample owner-scope memories:")
    for mt, txt in cur.fetchall():
        print(f"    - [{mt}] {head(txt, 90)}")

rule("Layer 1 — MEMORY block (what gets injected: standing rules + semantic recall)")
memory_block = build_memory_block(active_business=BUSINESS, latest_user_text=QUERY)
print(f"  populated: {bool(memory_block)}  ({len(memory_block)} chars)")
print(head(memory_block, 700) if memory_block else "  (empty)")

rule("Layer 2 — BUSINESS CONTEXT PACK (context_packs.packs_for)")
try:
    from hermes.context_packs import packs_for
    context_pack = packs_for(BUSINESS)
except Exception as e:  # noqa: BLE001
    context_pack = ""
    print("  packs_for raised:", e)
print(f"  populated: {bool(context_pack)}  ({len(context_pack)} chars)")
if not context_pack:
    print("  (empty — expected: migration 0017 not applied in this DB, per PROJECT_CONTEXT §0.1)")

rule("Layer 3 — RETRIEVED CONTEXT (RAG: Pinecone corpus + Context files, federated)")
retrieved = build_context_block(active_business=BUSINESS, latest_user_text=QUERY)
print(f"  populated: {bool(retrieved)}  ({len(retrieved)} chars)")
print(head(retrieved, 900) if retrieved else "  (empty)")

rule("FULL system prompt — every layer assembled (what the model actually receives)")
full = _system_prompt(
    voice=False, active_business=BUSINESS,
    memory_block=memory_block, context_pack=context_pack, retrieved_context=retrieved,
)
print(f"  total system-prompt length: {len(full)} chars")
markers = {
    "Business scope (locked to afk)": f"locked to business '{BUSINESS}'",
    "MEMORY block": "MEMORY (what you already know",
    "BUSINESS CONTEXT PACK": "BUSINESS CONTEXT PACK",
    "RETRIEVED CONTEXT (RAG)": "RETRIEVED CONTEXT",
    "search_business_knowledge tool referenced": "search_business_knowledge",
    "Today's date": "Today's date is",
}
print("  sections present in the assembled prompt:")
for label, needle in markers.items():
    print(f"    [{'X' if needle in full else ' '}] {label}")

print("\nDONE.")
