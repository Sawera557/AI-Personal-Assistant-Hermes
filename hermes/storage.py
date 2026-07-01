"""S3 — the locker. Stores the real file bytes; the app serves them via short,
temporary presigned links (never a public URL).

Optional: if S3_BUCKET is not set, ingestion still works (documents are recorded
and made searchable) but the original bytes are not persisted to S3. Set
S3_BUCKET to turn the locker on.
"""
from __future__ import annotations

import os


def bucket() -> str:
    return os.getenv("S3_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def _client():
    import boto3  # lazy

    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))


def put(key: str, data: bytes, content_type: str | None = None) -> str | None:
    """Store bytes at ``key``; return the ``s3://`` URI (or None if disabled)."""
    if not enabled():
        return None
    extra = {"ContentType": content_type} if content_type else {}
    _client().put_object(Bucket=bucket(), Key=key, Body=data, **extra)
    return f"s3://{bucket()}/{key}"


def delete(s3_uri: str) -> bool:
    """Delete an object by its ``s3://`` URI. Returns True if the call succeeded."""
    if not s3_uri or not s3_uri.startswith("s3://"):
        return False
    rest = s3_uri[len("s3://"):]
    b = bucket()
    if b and rest.startswith(b + "/"):
        key = rest[len(b) + 1:]
        buck = b
    else:
        buck, _, key = rest.partition("/")
    if not key:
        return False
    try:
        _client().delete_object(Bucket=buck, Key=key)
        return True
    except Exception:
        return False


def presign(s3_uri: str, expires: int = 900) -> str | None:
    """Return a short-lived GET URL for an ``s3://bucket/key`` URI.

    Handles both plain bucket names and Access Point ARNs stored in S3_BUCKET.
    """
    if not s3_uri or not s3_uri.startswith("s3://"):
        return None
    rest = s3_uri[len("s3://"):]
    b = bucket()
    if b and rest.startswith(b + "/"):
        # URI was built with current bucket/ARN as prefix — works for ARNs too
        key = rest[len(b) + 1:]
        buck = b
    else:
        # Legacy plain-bucket URIs: s3://bucket-name/key
        buck, _, key = rest.partition("/")
    if not key:
        return None
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": buck, "Key": key},
        ExpiresIn=expires,
    )
