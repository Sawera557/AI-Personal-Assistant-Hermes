"""Re-embed stored vectors with the configured EMBED_PROVIDER (e.g. gemini).

Order: memories -> document_chunks (pgvector, updated in place) -> Pinecone
(copied from the source index into PINECONE_INDEX, which is created if missing;
the source index is never modified). Rate-limited for the Gemini free tier and
resumable: pgvector rows already tagged with the new EMBED_MODEL are skipped,
Pinecone ids already present in the target index are skipped.

    python scripts/reembed_gemini.py --source-index hermes --per-namespace 40
    python scripts/reembed_gemini.py --source-index hermes --per-namespace 0   # all
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from pgvector import Vector  # noqa: E402

from hermes.config import Config  # noqa: E402
from hermes.db.connection import cursor  # noqa: E402
from hermes.vector_store import EMBED_DIM, EMBED_MODEL, embed  # noqa: E402

BATCH = 50          # texts per embed call
PER_MINUTE = 90     # stay under the free tier's 100/min


class _Pacer:
    def __init__(self) -> None:
        self.window: list[tuple[float, int]] = []

    def wait(self, n: int) -> None:
        while True:
            now = time.time()
            self.window = [(t, k) for t, k in self.window if now - t < 60]
            if sum(k for _, k in self.window) + n <= PER_MINUTE:
                self.window.append((now, n))
                return
            time.sleep(1)


pacer = _Pacer()


def _embed(texts: list[str]) -> list[list[float]]:
    for attempt in range(6):
        pacer.wait(len(texts))
        try:
            return embed(texts)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  429 — backing off ({attempt + 1}/6)", flush=True)
                time.sleep(30)
                continue
            raise
    raise RuntimeError("Embedding quota exhausted — rerun later to resume.")


def _pg(table: str, id_col: str, text_col: str) -> None:
    with cursor(commit=False) as cur:
        cur.execute(
            f"SELECT {id_col}, {text_col} FROM {table} "
            f"WHERE embedding IS NOT NULL AND embed_model IS DISTINCT FROM %s",
            (EMBED_MODEL,),
        )
        rows = cur.fetchall()
    print(f"{table}: {len(rows)} to re-embed", flush=True)
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        vecs = _embed([r[1] or "" for r in batch])
        with cursor() as cur:
            cur.executemany(
                f"UPDATE {table} SET embedding = %s, embed_model = %s WHERE {id_col} = %s",
                [(Vector(v), EMBED_MODEL, r[0]) for r, v in zip(batch, vecs)],
            )
        print(f"  {table}: {i + len(batch)}/{len(rows)}", flush=True)


def _pinecone(source_name: str, per_namespace: int) -> None:
    from pinecone import Pinecone, ServerlessSpec

    target_name = Config.PINECONE_INDEX
    if target_name == source_name:
        raise SystemExit("PINECONE_INDEX must differ from --source-index.")
    pc = Pinecone(api_key=Config.PINECONE_API_KEY)
    if target_name not in {i.name for i in pc.list_indexes()}:
        print(f"creating Pinecone index {target_name!r}", flush=True)
        pc.create_index(
            name=target_name, dimension=EMBED_DIM, metric="cosine",
            spec=ServerlessSpec(cloud=Config.PINECONE_CLOUD, region=Config.PINECONE_REGION),
        )
        while not pc.describe_index(target_name).status["ready"]:
            time.sleep(2)
    src, dst = pc.Index(source_name), pc.Index(target_name)

    for ns in src.describe_index_stats().namespaces:
        ids: list[str] = []
        for page in src.list(namespace=ns):
            # Depending on SDK version a page is a list of ids or of ListItem(id=...).
            ids.extend(getattr(x, "id", x) for x in page)
            if per_namespace and len(ids) >= per_namespace:
                ids = ids[:per_namespace]
                break
        print(f"{ns}: {len(ids)} ids", flush=True)
        for i in range(0, len(ids), BATCH):
            chunk_ids = ids[i : i + BATCH]
            have = set(dst.fetch(ids=chunk_ids, namespace=ns).vectors)
            todo = [x for x in chunk_ids if x not in have]
            if not todo:
                continue
            fetched = src.fetch(ids=todo, namespace=ns).vectors
            items = [(vid, v.metadata or {}) for vid, v in fetched.items()]
            items = [(vid, md) for vid, md in items if md.get("text")]
            if not items:
                continue
            vecs = _embed([md["text"] for _, md in items])
            dst.upsert(
                vectors=[{"id": vid, "values": vec, "metadata": md}
                         for (vid, md), vec in zip(items, vecs)],
                namespace=ns,
            )
            print(f"  {ns}: {min(i + BATCH, len(ids))}/{len(ids)}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-index", default="hermes")
    ap.add_argument("--per-namespace", type=int, default=40,
                    help="Pinecone vectors per namespace to copy (0 = all)")
    ap.add_argument("--skip-pg", action="store_true")
    ap.add_argument("--skip-pinecone", action="store_true")
    args = ap.parse_args()
    print(f"provider model={EMBED_MODEL} dim={EMBED_DIM}", flush=True)
    if not args.skip_pg:
        _pg("memories", "memory_id", "text")
        _pg("document_chunks", "chunk_id", "text_preview")
    if not args.skip_pinecone:
        _pinecone(args.source_index, args.per_namespace)
    print("done", flush=True)


if __name__ == "__main__":
    main()
