"""Download links for agent-generated files.

Two things matter here and both have bitten us:

1. The link handed to the model must be short and stable. A ~500-char presigned
   URL gets shortened when the model retypes it into its reply, and an unsigned
   S3 URL is a permanent 403 — the user gets a dead link.
2. `public_url` must NEVER return a URL for anything outside `generated/`.
   Publishing an ingested source document (patient records, bills, WATI media)
   would be a serious data leak, so the prefix guard is asserted directly rather
   than trusted to the bucket policy alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes import storage, tools  # noqa: E402
from hermes.config import Config  # noqa: E402

BUCKET_ORIGIN = "https://bkt.s3.us-east-1.amazonaws.com"
FILE_ID = "11111111-2222-3333-4444-555555555555"
SIGNED = "https://s3.example/o?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"


@pytest.fixture
def s3(monkeypatch):
    """S3 turned on, with put/presign faked so no AWS call is made."""
    monkeypatch.setenv("S3_BUCKET", "bkt")
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "put", lambda key, data, ct=None: f"s3://bkt/{key}")
    monkeypatch.setattr(storage, "presign", lambda uri, expires=0: SIGNED)


@pytest.fixture
def indexed(monkeypatch):
    """Stub the generated_files index so no DB is touched.

    Patch the real module's attribute rather than sys.modules: `_create_file`
    does `from .db import generated_files`, which resolves to the attribute
    already bound on the `hermes.db` package once anything else in the suite has
    imported it — a sys.modules stub is silently ignored in that case, so the
    test passes alone and fails in a full run.
    """
    from hermes.db import generated_files as real

    monkeypatch.setattr(real, "record_file", lambda **kw: FILE_ID)
    return real


def create(**overrides):
    payload = {"filename": "a.md", "content": "hi", **overrides}
    return tools._create_file(payload, "afk")


# ---- public_url: the guard rails ----

def test_public_url_built_for_generated_keys(monkeypatch):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    url = storage.public_url("s3://bkt/generated/afk/abc/report.md")
    assert url == f"{BUCKET_ORIGIN}/generated/afk/abc/report.md"


def test_public_url_percent_encodes_filenames(monkeypatch):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    url = storage.public_url("s3://bkt/generated/afk/abc/my report (final).md")
    assert " " not in url and url.endswith("my%20report%20%28final%29.md")


@pytest.mark.parametrize("uri", [
    "s3://bkt/ingest/afk/patient-records.pdf",
    "s3://bkt/attachments/afk/invoice.png",
    "s3://bkt/wati/media/voice-note.ogg",
    "s3://bkt/generated-elsewhere/x.md",   # prefix must not match by substring
    "s3://bkt/nested/generated/x.md",      # ...nor anywhere but the start
])
def test_public_url_refuses_keys_outside_generated_prefix(monkeypatch, uri):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    assert storage.public_url(uri) is None


def test_public_url_disabled_without_env(monkeypatch):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "")
    assert storage.public_url("s3://bkt/generated/afk/abc/report.md") is None


def test_public_url_ignores_malformed_uri(monkeypatch):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    assert storage.public_url("https://not-an-s3-uri/x") is None


# ---- _create_file: link preference, best first ----

def test_prefers_permanent_public_url(monkeypatch, s3, indexed):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://app.example.com")
    out = create()
    assert out["download_url"].startswith(f"{BUCKET_ORIGIN}/generated/afk/")
    assert out["link_expires"] is False
    assert out["public_link"] is True
    assert "X-Amz-Signature" not in out["download_url"]
    assert "expires_in_hours" not in out


def test_falls_back_to_app_route_when_not_public(monkeypatch, s3, indexed):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://app.example.com")
    out = create()
    assert out["download_url"] == f"https://app.example.com/api/generated-files/{FILE_ID}/download"
    assert out["link_expires"] is False
    assert out["public_link"] is False


def test_falls_back_to_presigned_without_any_public_origin(monkeypatch, s3, indexed):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "")
    out = create()
    assert out["download_url"] == SIGNED
    assert out["expires_in_hours"] == 24
    assert "link_expires" not in out


def test_falls_back_to_presigned_when_indexing_returns_no_row(monkeypatch, s3, indexed):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://app.example.com")
    indexed.record_file = lambda **kw: None
    assert create()["download_url"] == SIGNED


def test_indexing_failure_never_breaks_file_creation(monkeypatch, s3, indexed):
    """The index is best-effort; a dead DB must not cost the user their file."""
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://app.example.com")

    def boom(**kw):
        raise RuntimeError("db down")

    indexed.record_file = boom
    out = create()
    # Still public: the public URL derives from the key, not the index row.
    assert out["ok"] is True and out["public_link"] is True


def test_generated_key_is_scoped_to_the_business(monkeypatch, s3, indexed):
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", BUCKET_ORIGIN)
    out = create()
    assert out["s3_uri"].startswith("s3://bkt/generated/afk/")
    assert out["business"] == "afk"
