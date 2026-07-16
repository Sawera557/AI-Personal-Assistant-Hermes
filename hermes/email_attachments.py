"""The `email_with_attachment` tool: a deterministic prepare-then-send flow for
emailing files through the connected Gmail account.

Split out of `tools.py` for readability. `tools.py` owns the tool's JSON schema,
its status text, and the dispatch line; everything about *how* the flow works
lives here. The single entry point is `run()`.

Flow (identical every call):
  * confirm omitted/false   -> generate/collect attachments, build the message,
    save it as a Gmail draft, return draft_id + preview. Nothing is sent.
  * confirm=true + draft_id  -> send exactly that reviewed draft (byte-for-byte).
  * confirm=true, no draft_id -> build and send fresh (fallback).

Any single attachment over ~25 MB is uploaded to Drive and linked in the body
instead of attached, so the send never fails on size.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from . import documents
from . import storage
from .attachments import store as attachment_store
from .maton import MatonClient, MatonError

# Per-file ceiling for a Gmail attachment. Gmail's raw MIME message tops out
# around 35 MB, but base64 encoding inflates bytes ~4/3, so a raw file above
# ~25 MB risks pushing the encoded message over the limit. Files larger than
# this are uploaded to Drive and linked in the body instead of attached.
MAX_ATTACH_BYTES = 25 * 1024 * 1024


def _mime_split(media_type: str) -> tuple[str, str]:
    """Split 'application/pdf' -> ('application', 'pdf') for EmailMessage.
    Drops any parameters and falls back to a generic binary type."""
    main, _, sub = (media_type or "").partition("/")
    sub = sub.split(";")[0].strip()
    if not main or not sub:
        return "application", "octet-stream"
    return main.strip(), sub


def _build_mime(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    attachments: list[tuple[str, str, bytes]],
) -> str:
    """Build a multipart RFC 2822 message with file attachments and base64url-
    encode it for Gmail's `raw` field. `attachments` is a list of
    (filename, media_type, raw_bytes). Uses Python's email module so UTF-8
    headers/body are always encoded correctly."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, media_type, raw in attachments:
        maintype, subtype = _mime_split(media_type)
        msg.add_attachment(raw, maintype=maintype, subtype=subtype, filename=filename)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _resolve_attachments(
    tool_input: dict[str, Any], active_business: str | None
) -> list[tuple[str, str, bytes]]:
    """Turn each attachment spec into (filename, media_type, raw_bytes).

    Three shapes, checked in order: generate a file (filename+content), a file
    already uploaded in this chat (attachment_id), or a previously stored file
    (s3_uri). Business scoping is enforced the same way as elsewhere — an
    attachment_id/s3_uri from another tenant is not resolvable here.
    """
    # Lazy import: tools.py imports this module, so importing these scoping
    # helpers at module load would be a cycle. By call time tools is fully loaded.
    from .tools import _attachment_scope, _read_scope_ok

    specs = tool_input.get("attachments") or []
    if not isinstance(specs, list):
        raise MatonError("`attachments` must be a list.")
    scope = _attachment_scope(tool_input, active_business)
    requested_business = tool_input.get("business") or None

    out: list[tuple[str, str, bytes]] = []
    for item in specs:
        if not isinstance(item, dict):
            raise MatonError("Each attachment must be an object.")
        filename = (item.get("filename") or "").strip()
        content = item.get("content")
        attachment_id = item.get("attachment_id")
        s3_uri = item.get("s3_uri")

        if content is not None and filename:
            try:
                raw, media_type = documents.generate_document(
                    filename, content, item.get("content_type")
                )
            except Exception as exc:  # noqa: BLE001 — clean tool error
                raise MatonError(
                    f"Could not generate attachment {filename!r}: {exc}"
                ) from exc
            out.append((filename, media_type, raw))
        elif attachment_id:
            meta = attachment_store.meta(attachment_id, scope)
            block = attachment_store.block(attachment_id, scope)
            if not meta or not block:
                raise MatonError(f"Unknown or inaccessible attachment_id: {attachment_id}")
            source = block.get("source") or {}
            if source.get("type") != "base64":
                raise MatonError(
                    "That uploaded file kept no original bytes (only its extracted "
                    "text was stored) — re-upload it, or attach it as generated content."
                )
            raw = base64.standard_b64decode(source["data"])
            out.append((
                meta.get("filename") or "file",
                source.get("media_type") or "application/octet-stream",
                raw,
            ))
        elif s3_uri:
            if not storage.enabled():
                raise MatonError(
                    "File storage (S3) isn't configured, so s3_uri attachments can't be read."
                )
            ok, reason = _read_scope_ok(s3_uri, active_business, requested_business)
            if not ok:
                raise MatonError(reason)
            try:
                raw, ctype = storage.get_bytes(s3_uri)
            except ValueError as exc:
                raise MatonError(str(exc)) from exc
            out.append((
                filename or (storage.key_of(s3_uri) or "file").rsplit("/", 1)[-1] or "file",
                ctype or "application/octet-stream",
                raw,
            ))
        else:
            raise MatonError(
                "Each attachment needs one of: filename+content (generate a file), "
                "attachment_id (a chat upload), or s3_uri (a stored file)."
            )
    return out


def _drive_upload_link(
    client: MatonClient, filename: str, media_type: str, raw: bytes
) -> str:
    """Upload an oversized attachment to Google Drive, grant anyone-with-link
    read access, and return the shareable webViewLink. Only used for files too
    big to attach to a Gmail message, so the send never fails on size."""
    up = client.upload_binary(
        "google-drive", "POST", "upload/drive/v3/files",
        data=raw, content_type=media_type or "application/octet-stream",
        query={"uploadType": "media", "fields": "id"},
    )
    if not up.get("ok"):
        raise MatonError(
            f"Couldn't upload the oversized attachment {filename!r} to Drive "
            f"(status {up.get('status')}). Is Google Drive connected for this business?"
        )
    file_id = (up.get("data") or {}).get("id")
    if not file_id:
        raise MatonError(f"Drive upload of {filename!r} returned no file id.")
    # A media upload creates the file as 'Untitled' — give it the real name.
    client.request("google-drive", "PATCH", f"drive/v3/files/{file_id}", body={"name": filename})
    # Anyone-with-the-link read access so the recipient can actually open it.
    client.request(
        "google-drive", "POST", f"drive/v3/files/{file_id}/permissions",
        body={"role": "reader", "type": "anyone"},
    )
    meta = client.request(
        "google-drive", "GET", f"drive/v3/files/{file_id}", query={"fields": "webViewLink"}
    )
    link = (meta.get("data") or {}).get("webViewLink")
    if not link:
        raise MatonError(f"Uploaded {filename!r} to Drive but couldn't get a shareable link.")
    return link


def _gmail_request(
    client: MatonClient, method: str, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """A google-mail request that turns a Gmail 429 (Google's 'user-rate limit
    exceeded', a temporary per-account throttle) into a clean, truthful
    MatonError — so it's reported as a plain 'try again shortly' message with
    the retry-after time, never as a raw JSON blob the model has to paraphrase
    (and can mislabel). Any other response is returned unchanged."""
    result = client.request("google-mail", method, path, body=body)
    if result.get("status") == 429:
        data = result.get("data")
        msg = ((data.get("error") or {}).get("message") or "") if isinstance(data, dict) else ""
        note = (
            "Gmail is temporarily rate-limiting this account (Google 'user-rate "
            "limit exceeded'). Nothing was sent — try again in a minute."
        )
        idx = msg.find("Retry after")
        if idx != -1:
            note += " " + msg[idx:].strip()
        raise MatonError(note)
    return result


def run(
    client: MatonClient, tool_input: dict[str, Any], active_business: str | None
) -> dict[str, Any]:
    """Entry point for the `email_with_attachment` tool (see module docstring)."""
    to = tool_input["to"]
    to = to if isinstance(to, list) else [to]
    cc = tool_input.get("cc")
    if isinstance(cc, str):
        cc = [cc]
    subject = tool_input["subject"]
    body = tool_input.get("body") or ""
    confirm = bool(tool_input.get("confirm"))
    draft_id = (tool_input.get("draft_id") or "").strip()

    # SEND stage — send exactly the reviewed draft. Deterministic happy path:
    # no rebuild, so what goes out is byte-for-byte what was approved.
    if confirm and draft_id:
        result = _gmail_request(
            client, "POST", "/gmail/v1/users/me/drafts/send", {"id": draft_id}
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {**result, "data": {**data, "sent": result.get("ok", False),
                                   "draft_id": draft_id, "to": to}}

    # Build the message (for a fresh confirmed send or for the prepare draft).
    attachments = _resolve_attachments(tool_input, active_business)
    inline: list[tuple[str, str, bytes]] = []
    linked: list[tuple[str, int, str]] = []
    for filename, media_type, raw in attachments:
        if len(raw) > MAX_ATTACH_BYTES:
            link = _drive_upload_link(client, filename, media_type, raw)
            linked.append((filename, len(raw), link))
        else:
            inline.append((filename, media_type, raw))

    if linked:
        lines = ["", "", "Some files were too large to attach and are shared via Google Drive instead:"]
        for filename, size, link in linked:
            lines.append(f"  - {filename} ({size // (1024 * 1024)} MB): {link}")
        body = body + "\n".join(lines)

    raw_msg = _build_mime(to=to, subject=subject, body=body, cc=cc, attachments=inline)
    preview = {
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "attached": [{"filename": f, "size_bytes": len(r)} for f, _, r in inline],
        "shared_via_drive": [
            {"filename": f, "size_bytes": s, "link": lk} for f, s, lk in linked
        ],
    }

    # SEND stage (fallback) — confirm=true but no draft_id to reference.
    if confirm:
        result = _gmail_request(
            client, "POST", "/gmail/v1/users/me/messages/send", {"raw": raw_msg}
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {**result, "data": {**data, "sent": result.get("ok", False), **preview}}

    # PREPARE stage — hold as a Gmail draft; do NOT send.
    result = _gmail_request(
        client, "POST", "/gmail/v1/users/me/drafts", {"message": {"raw": raw_msg}}
    )
    if not result.get("ok"):
        return result
    new_draft_id = (result.get("data") or {}).get("id")
    return {
        "status": result.get("status", 200),
        "ok": True,
        "data": {
            "stage": "awaiting_approval",
            "sent": False,
            "draft_id": new_draft_id,
            "preview": preview,
            "next_step": (
                "Show the user this preview (recipients, subject, attachments) and ask "
                "them to approve. To send, call email_with_attachment again with "
                f"confirm=true and draft_id={new_draft_id!r}."
            ),
        },
    }
