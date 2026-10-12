"""HMAC verification for GitHub webhook deliveries.

GitHub signs the raw request body with the webhook secret and sends the digest
as `X-Hub-Signature-256: sha256=<hex>`. Verify against the EXACT bytes received:
re-serialising the parsed JSON changes key order and whitespace and will not
reproduce the digest.
"""
from __future__ import annotations

import hashlib
import hmac


def verify(secret: str, body: bytes, header: str | None) -> bool:
    """True only if `header` is a valid sha256 HMAC of `body` under `secret`.

    Every failure mode — no secret, no header, wrong scheme, wrong digest —
    returns False rather than raising, so the route has a single reject path.
    """
    if not secret or not header:
        return False

    scheme, _, sent = header.strip().partition("=")
    if scheme.lower() != "sha256" or not sent:
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # compare_digest, not ==, so a mismatch does not leak its position by timing.
    return hmac.compare_digest(expected, sent.strip().lower())
