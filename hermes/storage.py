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
from urllib.parse import quote


def bucket() -> str:
    return os.getenv("S3_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def _client():
    import boto3  # lazy

    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))


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
    """Store bytes at ``key``; return the ``s3://`` URI (or None if disabled)."""
    if not enabled():
        return None
    extra = {"ContentType": content_type} if content_type else {}
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
PUBLIC_PREFIX = "generated/"


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
    if not key or not key.startswith(PUBLIC_PREFIX):
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
