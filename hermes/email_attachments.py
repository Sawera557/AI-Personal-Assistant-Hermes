"""The `email_with_attachment` tool: a deterministic prepare-then-send flow for
emailing files through the connected Gmail account.

Split out of `tools.py` for readability. `tools.py` owns the tool's JSON schema,
its status text, and the dispatch line; everything about *how* the flow works
lives here. The single entry point is `run()`.

Flow (identical every call):
  * confirm omitted/false   -> generate/collect attachments, build the message,
    save it as a Gmail draft, return draft_id + preview. Nothing is sent.
  * confirm=true + draft_id  -> send exactly that reviewed draft (byte-for-byte).
  * confirm=true, no draft_id -> refused; nothing is built or sent (B4 in
    docs/architecture/TURN_COST_PLAN.md — it used to rebuild and send unreviewed).

In a live chat the send also needs the approval_token handed out with the
draft, on a later turn than the prepare (hermes.guardrails draft gate).

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


#: Header on a prepared draft listing the Drive files its body links to, which
#: are shared only when that draft is actually sent (see _drive_upload).
DRIVE_FILES_HEADER = "X-Hermes-Drive-Files"


def _build_mime(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    attachments: list[tuple[str, str, bytes]],
    drive_file_ids: list[str] | None = None,
) -> str:
    """Build a multipart RFC 2822 message with file attachments and base64url-
    encode it for Gmail's `raw` field. `attachments` is a list of
    (filename, media_type, raw_bytes). Uses Python's email module so UTF-8
    headers/body are always encoded correctly."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if drive_file_ids:
        msg[DRIVE_FILES_HEADER] = ",".join(drive_file_ids)
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


def _drive_upload(
    client: MatonClient, filename: str, media_type: str, raw: bytes
) -> tuple[str, str]:
    """Upload an oversized attachment to Google Drive, PRIVATELY, and return
    (file_id, webViewLink). Only used for files too big to attach to a Gmail
    message, so the send never fails on size.

    This runs in the PREPARE step, before the user has approved anything, so it
    grants nobody access. It used to make the file readable by anyone with the
    link right here — public before the send was even confirmed, and for good
    if it never was. The file is shared in the SEND step instead
    (_share_drive_files), once the reviewed draft actually goes out; the draft
    carries the file ids in DRIVE_FILES_HEADER so the send can find them."""
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
    meta = client.request(
        "google-drive", "GET", f"drive/v3/files/{file_id}", query={"fields": "webViewLink"}
    )
    link = (meta.get("data") or {}).get("webViewLink")
    if not link:
        raise MatonError(f"Uploaded {filename!r} to Drive but couldn't get its link.")
    return file_id, link


def _draft_drive_files(client: MatonClient, draft_id: str) -> list[str]:
    """The Drive file ids a prepared draft links to (its DRIVE_FILES_HEADER)."""
    result = _gmail_request(
        client, "GET", f"/gmail/v1/users/me/drafts/{draft_id}",
        None, query={"format": "metadata", "metadataHeaders": DRIVE_FILES_HEADER},
    )
    if not result.get("ok"):
        raise MatonError(
            f"Couldn't read draft {draft_id} before sending (status {result.get('status')}). "
            "Nothing was sent."
        )
    message = (result.get("data") or {}).get("message") or {}
    headers = (message.get("payload") or {}).get("headers") or []
    value = next(
        (h.get("value") for h in headers
         if isinstance(h, dict) and str(h.get("name", "")).lower() == DRIVE_FILES_HEADER.lower()),
        "",
    )
    return [f.strip() for f in str(value or "").split(",") if f.strip()]


def _share_drive_files(client: MatonClient, file_ids: list[str]) -> None:
    """Share a sent draft's linked Drive files: anyone with the link may read,
    as before — now only once the reviewed email is actually going out. Link
    access (not per-person grants) because Drive refuses silent person grants
    to addresses without a Google account, which most external clients are."""
    for file_id in file_ids:
        granted = client.request(
            "google-drive", "POST", f"drive/v3/files/{file_id}/permissions",
            body={"role": "reader", "type": "anyone"},
        )
        if not granted.get("ok"):
            raise MatonError(
                f"Couldn't share the linked Drive file {file_id} (status {granted.get('status')}). "
                "Nothing was sent."
            )


def _gmail_request(
    client: MatonClient, method: str, path: str, body: dict[str, Any] | None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A google-mail request that turns a Gmail 429 (Google's 'user-rate limit
    exceeded', a temporary per-account throttle) into a clean, truthful
    MatonError — so it's reported as a plain 'try again shortly' message with
    the retry-after time, never as a raw JSON blob the model has to paraphrase
    (and can mislabel). Any other response is returned unchanged."""
    result = client.request("google-mail", method, path, query=query, body=body)
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
        # Files too big to attach were uploaded privately at prepare; they are
        # shared now, with the approved send, and not a moment earlier.
        _share_drive_files(client, _draft_drive_files(client, draft_id))
        result = _gmail_request(
            client, "POST", "/gmail/v1/users/me/drafts/send", {"id": draft_id}
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {**result, "data": {**data, "sent": result.get("ok", False),
                                   "draft_id": draft_id, "to": to}}

    # A send must reference a reviewed draft. There used to be a fallback here
    # that, given confirm=true and no draft_id, rebuilt the message from the
    # arguments and sent it straight away — an email nobody had reviewed, from
    # a tool whose whole contract is "a review always happens first" (B4). It
    # is refused before anything is generated or uploaded.
    if confirm:
        return {
            "status": 400,
            "ok": False,
            "error": (
                "confirm=true needs the draft_id from a prepare call. Call "
                "email_with_attachment WITHOUT confirm first: it saves a Gmail draft and returns "
                "a preview. In a live chat, show the user that preview and send only after they "
                "approve it on their next message; in an unattended run, send the prepared draft "
                "straight away with confirm=true and its draft_id. Nothing was sent."
            ),
        }

    # Build the message for the prepare draft.
    attachments = _resolve_attachments(tool_input, active_business)
    inline: list[tuple[str, str, bytes]] = []
    linked: list[tuple[str, int, str]] = []
    drive_file_ids: list[str] = []
    for filename, media_type, raw in attachments:
        if len(raw) > MAX_ATTACH_BYTES:
            file_id, link = _drive_upload(client, filename, media_type, raw)
            drive_file_ids.append(file_id)
            linked.append((filename, len(raw), link))
        else:
            inline.append((filename, media_type, raw))

    if linked:
        lines = ["", "", "Some files were too large to attach and are shared via Google Drive instead:"]
        for filename, size, link in linked:
            lines.append(f"  - {filename} ({size // (1024 * 1024)} MB): {link}")
        body = body + "\n".join(lines)

    raw_msg = _build_mime(
        to=to, subject=subject, body=body, cc=cc, attachments=inline,
        drive_file_ids=drive_file_ids,
    )
    preview = {
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "attached": [{"filename": f, "size_bytes": len(r)} for f, _, r in inline],
        "shared_via_drive": [
            {"filename": f, "size_bytes": s, "link": lk} for f, s, lk in linked
        ],
    }

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
