"""Tests for the federated RAG layer (hermes/rag.py, hermes/pinecone_store.py).

All offline / pure — nothing here touches Postgres, OpenAI, or Pinecone. Every
case exercises either a pure helper or a flag-gated early return that fires
BEFORE any DB/network call, so the suite runs with no services and no keys.
Covers:

* Sensitivity cap — 'restricted' is never in the allowed set, at any clearance.
* Feature flags default OFF (merges dark) and gate correctly.
* pinecone_store.query returns [] when RAG-on-Pinecone is off (no network).
* build_context_block returns "" when off / "All Businesses" / blank text.
* De-dupe key collapses the same chunk from both stores.
* Block formatting: header, one line per hit, and the char cap.
* vector_store.query guards a blank query (no embed call).

Run directly:

    python tests/test_rag.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("HERMES_DISABLE_DB", "1")
# First line of defence for the default-state assertions. NOTE: config.py runs
# load_dotenv(override=True) at import, so a developer's .env re-populates these
# right after — test_flags_default_off therefore clears them again at call time.
os.environ.pop("HERMES_RAG_PINECONE", None)
os.environ.pop("HERMES_RAG_CONTEXT_INJECTION", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes import pinecone_store as pc  # noqa: E402
from hermes import rag  # noqa: E402
from hermes import vector_store as vs  # noqa: E402
from hermes.config import Config  # noqa: E402

_passed = 0


def check(label: str, cond: bool) -> None:
    global _passed
    assert cond, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


# ---- Sensitivity cap: 'restricted' must never be served -------------------
def test_restricted_never_allowed():
    for level in ("public", "internal", "confidential", "restricted", "bogus"):
        allowed = pc._allowed_sensitivities(level)
        check(f"restricted excluded at clearance={level}", "restricted" not in allowed)
    check("confidential clearance includes the full non-restricted ladder",
          pc._allowed_sensitivities("confidential") == ["public", "internal", "confidential"])
    check("unknown clearance degrades to public-only",
          pc._allowed_sensitivities("bogus") == ["public"])


# ---- Flags default OFF (dark merge) ---------------------------------------
def test_flags_default_off():
    # config.py runs load_dotenv(override=True) AT IMPORT, which re-populates
    # these from a developer's .env even after the module-level pop above — so
    # the readers must be cleared here, at call time, where they look lazily.
    # Save and restore so the rest of the suite is unaffected.
    saved = {k: os.environ.pop(k, None)
             for k in ("HERMES_RAG_PINECONE", "HERMES_RAG_CONTEXT_INJECTION")}
    try:
        check("rag_pinecone_enabled off by default", Config.rag_pinecone_enabled() is False)
        check("rag_context_injection off by default", Config.rag_context_injection() is False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_pinecone_key_gates_enable():
    # Even with the flag on, no key => stays disabled (fail-safe, never errors).
    os.environ["HERMES_RAG_PINECONE"] = "true"
    old_key = Config.PINECONE_API_KEY
    try:
        Config.PINECONE_API_KEY = ""
        check("flag on but no key => disabled", Config.rag_pinecone_enabled() is False)
        Config.PINECONE_API_KEY = "pc-xxx"
        check("flag on + key => enabled", Config.rag_pinecone_enabled() is True)
    finally:
        Config.PINECONE_API_KEY = old_key
        os.environ.pop("HERMES_RAG_PINECONE", None)


# ---- pinecone_store.query is inert when disabled (no network) --------------
def test_pinecone_query_disabled_returns_empty():
    # RAG-on-Pinecone off => returns [] before ever importing/calling the SDK.
    check("disabled Pinecone query => []",
          pc.query(entity_slug="afro_farm", query_vector=[0.0] * 1536) == [])


# ---- Namespace convention matches the writer ------------------------------
def test_namespace_convention():
    check("pinecone namespace is entity__<slug>",
          pc.entity_namespace("nour_hammam") == "entity__nour_hammam")
    check("pgvector + pinecone agree on namespace",
          pc.entity_namespace("gak_properties") == vs.entity_namespace("gak_properties"))


# ---- Always-on block: flag-gated early returns (no DB) ---------------------
def test_context_block_off_by_default():
    check("injection off => empty block",
          rag.build_context_block(active_business="afk", latest_user_text="rent arrears") == "")


def test_context_block_all_businesses_and_blank():
    os.environ["HERMES_RAG_CONTEXT_INJECTION"] = "true"
    try:
        check("All Businesses => empty (no single namespace)",
              rag.build_context_block(active_business="all", latest_user_text="hi") == "")
        check("no business => empty",
              rag.build_context_block(active_business=None, latest_user_text="hi") == "")
        check("blank text => empty",
              rag.build_context_block(active_business="afk", latest_user_text="   ") == "")
    finally:
        os.environ.pop("HERMES_RAG_CONTEXT_INJECTION", None)


# ---- Merge de-dup key -----------------------------------------------------
def test_dedupe_key_collapses_same_chunk():
    a = {"chunk_id": "c1", "document_id": "d1", "text": "hello", "origin": "pgvector"}
    b = {"chunk_id": "c1", "document_id": "d1", "text": "hello", "origin": "pinecone"}
    check("same chunk_id => same key", rag._dedupe_key(a) == rag._dedupe_key(b))
    c = {"chunk_id": "c2", "document_id": "d1", "text": "hello"}
    check("different chunk_id => different key", rag._dedupe_key(a) != rag._dedupe_key(c))
    # No chunk_id: falls back to document + snippet head.
    d = {"document_id": "d9", "text": "same head text here"}
    e = {"document_id": "d9", "text": "same head text here"}
    check("no chunk_id, same doc+head => same key", rag._dedupe_key(d) == rag._dedupe_key(e))


# ---- Block formatting + cap -----------------------------------------------
def test_format_block_structure():
    hits = [
        {"title": "Lease 720 E Lake", "source_system": "google-drive", "score": 0.81,
         "snippet": "Tenant   Amal   owes   two   months"},
        {"title": None, "source_system": None, "origin": "pinecone", "score": 0.42,
         "snippet": "meeting note"},
    ]
    block = rag._format_block("gak", hits)
    check("block has the RETRIEVED CONTEXT header", block.startswith("RETRIEVED CONTEXT"))
    check("business name appears in header", "'gak'" in block)
    check("first hit title rendered", "Lease 720 E Lake" in block)
    check("score formatted to 2dp", "0.81" in block)
    check("whitespace in snippet collapsed", "Tenant Amal owes two months" in block)
    check("missing title falls back to 'untitled'", "untitled" in block)
    check("missing source falls back to origin", "pinecone" in block)
    check("one bullet line per hit", block.count("\n  - ") == 2)


def test_format_block_char_cap():
    old = Config.RAG_CONTEXT_MAX_CHARS
    try:
        Config.RAG_CONTEXT_MAX_CHARS = 200
        hits = [{"title": f"Doc {i}", "source_system": "gmail", "score": 0.5,
                 "snippet": "x" * 400} for i in range(10)]
        block = rag._format_block("afk", hits)
        check("block respects the char cap", len(block) <= 200 + len("\n  ...(truncated)"))
        check("truncation marker appended", block.endswith("...(truncated)"))
    finally:
        Config.RAG_CONTEXT_MAX_CHARS = old


# ---- vector_store.query guards a blank query ------------------------------
def test_vector_store_blank_query_guard():
    # No text and no vector => [] without attempting to embed (no network).
    check("blank query_text + no vector => []",
          vs.query(namespace="entity__afro_farm", query_text="   ") == [])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} RAG test groups...\n")
    for t in tests:
        print(t.__name__)
        t()
    print(f"\n{_passed} checks passed.")
