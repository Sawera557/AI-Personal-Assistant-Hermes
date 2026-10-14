"""FastAPI routes for the GitHub -> Jira automation.

    POST /api/webhooks/github         the webhook GitHub delivers to
    GET  /api/webhooks/github/health  config check (no secrets in the response)

Mounted by app.py so it rides the existing deployment and gets a public HTTPS
URL without standing up a second service.

Status codes are chosen for how GitHub treats them: it shows non-2xx deliveries
as failures and will disable a hook that keeps failing. So 401 is reserved for a
genuinely unauthenticated caller, and every downstream problem (Jira down, bad
issue key, DB unreachable) comes back as 200 with the detail in the body.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import config, service, signature

log = logging.getLogger("gh_jira.router")

router = APIRouter(prefix="/api/webhooks", tags=["github-jira"])


@router.post("/github")
async def github_webhook(request: Request):
    """Verify, then hand the delivery to the service layer."""
    secret = config.webhook_secret()
    if not secret:
        # Fail closed. Without a secret there is no way to tell GitHub from
        # anyone else who found the URL, and this endpoint writes to a live board.
        log.error("GITHUB_WEBHOOK_SECRET is not set — refusing the delivery")
        return JSONResponse(
            {"ok": False, "error": "GITHUB_WEBHOOK_SECRET is not configured on this deployment."},
            status_code=503,
        )

    # The raw bytes, not the parsed body: the HMAC is over exactly what was sent.
    raw = await request.body()
    if not signature.verify(secret, raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("Rejected a GitHub webhook with a missing/invalid signature")
        return JSONResponse({"ok": False, "error": "Invalid signature."}, status_code=401)

    event = request.headers.get("X-GitHub-Event", "").strip()
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return JSONResponse({"ok": False, "error": f"Body is not valid JSON: {exc}"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "Body is not a JSON object."}, status_code=400)

    try:
        result = await run_in_threadpool(service.handle, event, delivery_id, payload)
    except Exception as exc:  # noqa: BLE001 — never let GitHub see a 500
        log.exception("Unhandled error processing delivery %s (%s)", delivery_id, event)
        return {"ok": False, "action": "error", "event": event, "error": str(exc)}

    return {**result, "delivery_id": delivery_id}


@router.get("/github/health")
async def github_webhook_health():
    """Is this deployment configured to accept and route GitHub deliveries?

    Reports whether a secret is set, never what it is.
    """
    cfg = config.snapshot()
    return {
        "ok": bool(cfg["secret_configured"]) and bool(cfg["enabled"]),
        "endpoint": "/api/webhooks/github",
        "supported_events": ["push", "pull_request", "pull_request_review", "ping"],
        "config": cfg,
    }
