"""S3 — the locker. Stores the real file bytes; the app serves them via short,
temporary presigned links.

One deliberate exception: objects under the ``generated/`` prefix (files the
agent authors for the user with create_file) can be served as permanent, unsigned
public URLs — see `public_url`. That is opt-in via S3_PUBLIC_BASE_URL *and* a
bucket policy that grants anonymous reads on that prefix only. Everything else —
ingested source documents, attachments, media — stays private and presigned-only.

Optional: if S3_BUCKET is not set, ingestion still works (documents are recorded
and made searchable) but the original bytes are not persisted to S3. Set
S3_BUCKET to turn the locker on.
"""
from __future__ import annotations

import os
import re
import uuid
from urllib.parse import quote


def bucket() -> str:
    return os.getenv("S3_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


# ── Per-project key conventions (PLAN §9b.7) ─────────────────────────────────
# Everything lives under the project slug so a bucket listing is a project
# folder tree and `key_business` attributes one segment:
#     <project-slug>/uploads/…    — files the owner uploads (chat, Library)
#     <project-slug>/generated/…  — files the agent writes (create_file)
# Legacy prefixes (`generated/<slug>/…`, `wati-media/<slug>/…`) are read-only
# history; new writes always use the project-first layout.

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]")


def sanitize_filename(filename: str) -> str:
    """Keep a filename readable but S3-safe (no slashes, no control chars)."""
    name = (filename or "").replace("/", "-").replace("\\", "-").strip()
    name = _UNSAFE_FILENAME.sub("_", name)
    return name[:120] or "file"


def project_file_key(area: str, slug: str | None, filename: str, subdir: str | None = None) -> str:
    """Build a per-project S3 key: ``<slug>/<area>/[<subdir>/]<uuid>-<filename>``
    ; slug None/blank -> 'general'. Lexical sort inside the folder is by
    recency when callers pass a date or session id as ``subdir``."""
    scope = (slug or "").strip() or "general"
    tail = f"{uuid.uuid4().hex}-{sanitize_filename(filename)}"
    if subdir:
        return f"{scope}/{area.strip('/')}/{subdir.strip('/')}/{tail}"
    return f"{scope}/{area.strip('/')}/{tail}"


def _client():
    import boto3  # lazy

    kwargs: dict = {"region_name": os.getenv("AWS_REGION", "us-east-1")}
    # Cloudflare R2 (the free-tier file store, PLAN §9b.7) / MinIO / any
    # S3-compatible endpoint — set S3_ENDPOINT_URL to its host.
    endpoint = os.getenv("S3_ENDPOINT_URL", "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _split(s3_uri: str) -> tuple[str, str] | None:
    """Parse an ``s3://bucket/key`` URI into ``(bucket, key)``.

    Handles both plain bucket names and Access Point ARNs stored in S3_BUCKET:
    if the URI was built with the current bucket/ARN as prefix, that whole prefix
    is the bucket and the remainder is the key; otherwise fall back to splitting a
    legacy plain-bucket URI at the first '/'. Returns None if the URI is malformed
    or has no key.
    """
    if not s3_uri or not s3_uri.startswith("s3://"):
        return None
    rest = s3_uri[len("s3://"):]
    b = bucket()
    if b and rest.startswith(b + "/"):
        buck, key = b, rest[len(b) + 1:]
    else:
        buck, _, key = rest.partition("/")
    if not buck or not key:
        return None
    return buck, key


def key_of(s3_uri: str) -> str | None:
    """Return just the object key from an ``s3://`` URI (or None if malformed)."""
    parsed = _split(s3_uri)
    return parsed[1] if parsed else None


def put(key: str, data: bytes, content_type: str | None = None) -> str | None:
    """Store bytes at ``key``; return the ``s3://`` URI (or None if disabled).

    Objects under PUBLIC_PREFIX are written with SSE-S3 (AES256) rather than the
    bucket default when public serving is on: an anonymous GET cannot authenticate
    to KMS, so an SSE-KMS object answers a public request with "The authorization
    mechanism you have provided is not supported. Please use Signature Version 4."
    instead of the bytes. Scoped to that prefix on purpose — ingested documents and
    attachments keep whatever the bucket default is (typically SSE-KMS).
    """
    if not enabled():
        return None
    extra = {"ContentType": content_type} if content_type else {}
    if public_base() and key.startswith(PUBLIC_PREFIX):
        extra["ServerSideEncryption"] = "AES256"
    _client().put_object(Bucket=bucket(), Key=key, Body=data, **extra)
    return f"s3://{bucket()}/{key}"


def delete(s3_uri: str) -> bool:
    """Delete an object by its ``s3://`` URI. Returns True if the call succeeded."""
    parsed = _split(s3_uri)
    if not parsed:
        return False
    buck, key = parsed
    try:
        _client().delete_object(Bucket=buck, Key=key)
        return True
    except Exception:
        return False


def get_bytes(s3_uri: str, max_bytes: int | None = None) -> tuple[bytes, str | None]:
    """Fetch an object's raw bytes and content-type by its ``s3://`` URI.

    Handles both plain bucket names and Access Point ARNs (same parsing as
    presign/delete). Raises ValueError on a malformed URI or if the object
    exceeds ``max_bytes``. Callers are responsible for entity-prefix scoping —
    this is a dumb read primitive.
    """
    parsed = _split(s3_uri)
    if not parsed:
        raise ValueError(f"Malformed S3 URI: {s3_uri!r}")
    buck, key = parsed
    obj = _client().get_object(Bucket=buck, Key=key)
    body = obj["Body"].read()
    if max_bytes is not None and len(body) > max_bytes:
        raise ValueError(
            f"Object is {len(body)} bytes, exceeds the {max_bytes}-byte read limit."
        )
    return body, obj.get("ContentType")


def list_objects(prefix: str = "") -> list[dict]:
    """List every object under ``prefix`` in the configured bucket.

    Returns a list of ``{"key", "size", "last_modified"}`` (last_modified as an
    ISO-8601 string), paging through all results. Returns ``[]`` when storage is
    disabled. Callers are responsible for tenant/prefix scoping — this is a dumb
    listing primitive over whatever prefix it's given.
    """
    if not enabled():
        return []
    out: list[dict] = []
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            lm = obj.get("LastModified")
            out.append({
                "key": obj["Key"],
                "size": obj.get("Size", 0),
                "last_modified": lm.isoformat() if lm else None,
            })
    return out


# Only keys under this prefix may ever be handed out as public URLs. create_file
# writes here (see tools._create_file); ingestion and attachments do not.
# Matches both layouts: legacy "generated/<slug>/…" and project-first
# "<slug>/generated/…" (PLAN §9b.7).
PUBLIC_PREFIX = "generated/"


def _is_generated_key(key: str) -> bool:
    return key.startswith(PUBLIC_PREFIX) or "generated" in key.split("/")


def public_base() -> str:
    """Public origin for anonymously-readable objects, or "" when disabled.

    Set S3_PUBLIC_BASE_URL to the bucket's public origin (e.g.
    ``https://my-bucket.s3.us-east-1.amazonaws.com``) or a CloudFront domain in
    front of it. Note this is the *bucket* origin — an S3 Access Point host never
    serves anonymous requests, so it cannot be used here.
    """
    return os.getenv("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")


def public_url(s3_uri: str) -> str | None:
    """A permanent, unsigned URL for an object under PUBLIC_PREFIX.

    Returns None when public serving is off, the URI is malformed, or the key
    falls outside PUBLIC_PREFIX — so a caller can never accidentally publish an
    ingested document by passing the wrong uri. The URL only actually resolves if
    the bucket policy grants anonymous s3:GetObject on that prefix.
    """
    base = public_base()
    if not base:
        return None
    key = key_of(s3_uri)
    if not key or not _is_generated_key(key):
        return None
    return f"{base}/{quote(key)}"


def presign(s3_uri: str, expires: int = 900) -> str | None:
    """Return a short-lived GET URL for an ``s3://bucket/key`` URI.

    Handles both plain bucket names and Access Point ARNs stored in S3_BUCKET.
    """
    parsed = _split(s3_uri)
    if not parsed:
        return None
    buck, key = parsed
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": buck, "Key": key},
        ExpiresIn=expires,
    )
