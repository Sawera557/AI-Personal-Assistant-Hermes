"""One-shot health check of the whole current system. Read-only.
Run: PYTHONUTF8=1 .venv/Scripts/python.exe scripts/_full_verify.py
"""
from __future__ import annotations
import os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_p=_f=0
def ok(label, cond, extra=""):
    global _p,_f
    if cond: _p+=1; print(f"  PASS  {label}  {extra}")
    else: _f+=1; print(f"  FAIL  {label}  {extra}")

print("== A. Main-app imports ==")
try:
    import app  # noqa
    from hermes.config import Config
    ok("import app + config", True)
except Exception as e:
    ok("import app + config", False, repr(e)); traceback.print_exc()

from hermes.config import Config
from hermes.businesses import to_entity_slug, BUSINESS_TO_ENTITY_SLUG
from hermes.db.bills import list_businesses
from hermes.db import documents as docs
from hermes.db.connection import cursor

print("\n== B. Businesses resolve (incl comfort_pca) ==")
biz_rows = {b['slug'] for b in list_businesses()}
for slug in ["afk","gak","gif","gakc","nh","per","comfort_pca"]:
    ent = to_entity_slug(slug)
    with cursor(commit=False) as cur:
        resolved = docs.resolve_entity(cur, ent) if ent else None
    key = bool(Config.maton_key_for_slug(slug))
    ok(f"business {slug}", bool(ent) and bool(resolved) and slug in biz_rows and key,
       f"-> {ent} | in_dropdown={slug in biz_rows} | key={key}")

print("\n== C. Pinecone corpus per business ==")
from pinecone import Pinecone
ns = Pinecone(api_key=Config.PINECONE_API_KEY).Index(Config.PINECONE_INDEX).describe_index_stats().get("namespaces",{}) or {}
for slug,label in [("afro_farm","afk"),("gak_properties","gak"),("gif_properties","gif"),
                   ("gak_commodities","gakc"),("nour_hammam","nh"),("personal","per"),
                   ("comfort_services","comfort_pca")]:
    c=(ns.get("entity__"+slug,{}) or {}).get("vector_count",0)
    ok(f"pinecone {label}", c>0, f"{c} vectors")

print("\n== D. RAG retrieval per business (federated_search) ==")
from hermes.rag import federated_search
for ent,q in [("gak_properties","tenant lease renewal"),("gif_properties","Clear Channel billboard"),
              ("gak_commodities","commodities trading"),("afro_farm","payroll supervisors"),
              ("comfort_services","client care schedule")]:
    try:
        hits=federated_search(ent,q,top_k=3)
        ok(f"RAG {ent}", len(hits)>0, f"{len(hits)} hits; top={str((hits[0].get('title') if hits else ''))[:40]!r}")
    except Exception as e:
        ok(f"RAG {ent}", False, repr(e))

print("\n== E. Memory layers ==")
from hermes.memory_retrieve import build_memory_block
from hermes.context_packs import packs_for, invalidate
with cursor(commit=False) as cur:
    cur.execute("SELECT count(*) FROM memories WHERE status='active';"); mc=cur.fetchone()[0]
ok("durable memories present", mc>50, f"{mc} active")
mb=build_memory_block(active_business="afk", latest_user_text="payroll rules")
ok("build_memory_block(afk)", len(mb)>100, f"{len(mb)} chars")
invalidate(); pk=packs_for("gak")
ok("context pack (gak) loads", len(pk)>1000, f"{len(pk)} chars")
ok("convo memory dark by default", Config.convo_memory_enabled() is False)

print("\n== F. Conversation-memory storage round-trip ==")
import uuid
os.environ["HERMES_CONVO_MEMORY_WRITE"]="true"; os.environ["HERMES_CONVO_MEMORY"]="true"
import importlib; import hermes.config as _c
from hermes.db import conversation_memory as cm
u=str(uuid.uuid4()); s=str(uuid.uuid4()); vec=[0.05]*1536
tid=cm.write_turn(user_id=u,session_id=s,seq=1,text="test verify turn",vector=vec,business_slug="gak")
got=cm.semantic_search(vec,user_id=u,top_k=3)
ok("convo write+recall", bool(tid) and len(got)>=1, f"wrote={bool(tid)} recalled={len(got)}")
with cursor() as cur: cur.execute("DELETE FROM conversation_memory WHERE user_id=%s;",(u,))
os.environ.pop("HERMES_CONVO_MEMORY_WRITE",None); os.environ.pop("HERMES_CONVO_MEMORY",None)

print("\n== G. Ingestion service ==")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"ingestion-service"))
try:
    from ingestion import workers, accounts, relevance  # noqa
    from ingestion import sensitivity as sg
    from ingestion.config import Config as IC
    ok("ingestion imports", True)
    ok("accounts afk (2 accounts)", len(accounts.accounts_for("afk"))==2)
    ok("accounts gak filtered", all(a["filtered"] for a in accounts.accounts_for("gak")))
    ok("relevance terms gak", len(relevance.terms_for("gak"))>5, f"{len(relevance.terms_for('gak'))} terms")
    g1=sg.evaluate(entity_slug="comfort_services",declared_level="confidential",text="patient PCA care")
    g2=sg.evaluate(entity_slug="afro_farm",declared_level="internal",text="patient medication")
    ok("PHI exemption: comfort allowed", g1.allow_cloud is True)
    ok("PHI guard intact for others", g2.allow_cloud is False)
except Exception as e:
    ok("ingestion imports", False, repr(e)); traceback.print_exc()

print(f"\n==== {_p} passed, {_f} failed ====")
