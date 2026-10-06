"""A failed upload must not leave a half-saved document behind.

Regression: when the embedding call failed (OpenAI 429), POST /api/documents
answered 500 but the documents row, its un-embedded chunks and the S3 object
all stayed — the note showed on the Context page yet could never be searched.
Offline: database, S3 and embeddings are stubbed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from hermes import ingest, storage


class _Boom(RuntimeError):
    pass


@pytest.fixture
def calls(monkeypatch):
    log = {"created": [], "deleted_docs": [], "deleted_s3": [], "chunks": 0}
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda key, data, ct=None: f"s3://bucket/{key}")
    monkeypatch.setattr(storage, "delete", lambda uri: log["deleted_s3"].append(uri) or True)
    monkeypatch.setattr(ingest.docs_repo, "create_document", lambda **kw: log["created"].append(kw["document_id"]))
    monkeypatch.setattr(ingest.docs_repo, "add_chunks", lambda rows: log.__setitem__("chunks", len(rows)) or len(rows))
    monkeypatch.setattr(ingest.docs_repo, "delete_document", lambda doc_id: log["deleted_docs"].append(doc_id) or {"s3_uri": None})
    return log


def _ingest():
    return ingest.ingest_document(None, filename="note.md", mime="text/markdown", raw=b"# Note\n\nHello there.")


def test_embedding_failure_removes_the_row_and_the_s3_object(monkeypatch, calls):
    def fail(namespace, chunks):
        raise _Boom("429 Too Many Requests")
    monkeypatch.setattr(ingest.ps, "upsert_chunks", fail)

    with pytest.raises(_Boom):  # the ORIGINAL error reaches the caller
        _ingest()

    assert calls["chunks"] == 1                        # chunks had been written…
    assert calls["deleted_docs"] == calls["created"]   # …and the row (cascading them) is gone
    assert len(calls["deleted_s3"]) == 1 and calls["deleted_s3"][0].startswith("s3://bucket/global/")


def test_a_failure_before_the_row_exists_still_removes_the_s3_object(monkeypatch, calls):
    monkeypatch.setattr(ingest.docs_repo, "create_document", lambda **kw: (_ for _ in ()).throw(_Boom("db down")))

    with pytest.raises(_Boom):
        _ingest()

    assert calls["deleted_docs"] == []   # nothing recorded, nothing to delete
    assert len(calls["deleted_s3"]) == 1


def test_cleanup_trouble_never_masks_the_original_error(monkeypatch, calls):
    monkeypatch.setattr(ingest.ps, "upsert_chunks", lambda ns, ch: (_ for _ in ()).throw(_Boom("embed failed")))
    monkeypatch.setattr(ingest.docs_repo, "delete_document", lambda doc_id: (_ for _ in ()).throw(RuntimeError("cleanup failed")))

    with pytest.raises(_Boom, match="embed failed"):
        _ingest()


def test_a_successful_ingest_is_left_alone(monkeypatch, calls):
    monkeypatch.setattr(ingest.ps, "upsert_chunks", lambda ns, chunks: len(list(chunks)))

    out = _ingest()

    assert out["chunks"] == 1 and out["embedded"] == 1
    assert calls["deleted_docs"] == [] and calls["deleted_s3"] == []
