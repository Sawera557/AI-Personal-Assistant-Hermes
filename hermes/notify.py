"""The agent → owner notification channel.

Hermes proactively emails the owner (and any per-task "notify to" address) when a
scheduled task finishes. This is deliberately separate from the *task* email tools
(`send_email` / `email_with_attachment` in tools.py) that reach third parties:

  * The recipient here is always a deliberate, human-typed address — the owner's own
    inbox for the daily attention report, or the address(es) set on a task's "Notify
    to" field. It can't misfire onto a tenant/vendor the way an agent-chosen `to:`
    could, so it does NOT need the outbound approval/confirm firewall.
  * It is driven in code from the scheduler (see `deliver_run_result`, called from
    `scheduler.execute_and_record` right after a run's outcome is persisted), NOT by
    the agent — so a `readonly` task can still have its result mailed, and delivery
    never depends on the model choosing to send.

Anti-spam: there are no per-completion owner emails. Owner-facing completion news is
rolled into the single once-a-day attention report (a normal scheduled task, seeded
by `ensure_digest_task`). Per-task "Notify to" delivery is opt-in — nothing is sent
unless a task carries an address.

Transport reuses the same Maton→Gmail path everything else uses; there is no separate
mailer. Mail is sent through one business's connected Gmail (Config.notify_business()),
since there is no dedicated Hermes sender address yet.
"""
from __future__ import annotations

import logging
from typing import Any

from . import storage
from .config import Config
from .email_attachments import MAX_ATTACH_BYTES, _build_mime
from .maton import MatonClient
from .tools import _build_mime_raw

log = logging.getLogger("hermes.notify")

# Keep the owner from wading through a wall of agent prose in the notification —
# the full reply lives behind the deep link (in the task's run history).
_BODY_PREVIEW_CHARS = 4000


def _notify_client() -> MatonClient | None:
    """A Maton client keyed to the business whose Gmail sends notifications, or
    None if no business has a connected Maton key (delivery is then a no-op)."""
    slug = Config.notify_business()
    if not slug:
        log.warning("notify: no business with a connected Maton key — cannot send")
        return None
    key = Config.maton_key_for_slug(slug)
    if not key:
        return None
    return MatonClient(api_key=key)


def _parse_recipients(raw: str | None) -> list[str]:
    """Split a stored comma-separated 'notify to' string into addresses."""
    return [a.strip() for a in (raw or "").split(",") if a.strip()]


def send_system_email(
    to: list[str],
    subject: str,
    body: str,
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bool:
    """Send a Hermes notification email. Prefixes the subject with '[Hermes] ' so
    the owner can filter/label it. Reuses the shared MIME builders and the Maton→
    Gmail transport. Returns True on a 2xx; never raises."""
    to = [a for a in (to or []) if a]
    if not to:
        return False
    client = _notify_client()
    if client is None:
        return False
    subject = subject if subject.startswith("[Hermes]") else f"[Hermes] {subject}"
    try:
        if attachments:
            raw = _build_mime(to=to, subject=subject, body=body, cc=None, attachments=attachments)
        else:
            raw = _build_mime_raw(to=to, subject=subject, body=body)
        resp = client.request(
            "google-mail", "POST", "/gmail/v1/users/me/messages/send", body={"raw": raw}
        )
        if not resp.get("ok"):
            log.warning("notify: send failed (status %s): %s", resp.get("status"), resp.get("data"))
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — delivery is best-effort (incl. MatonError)
        log.warning("notify: send raised: %s", exc)
        return False


def _run_files(result: dict[str, Any]) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    """Collect files a run produced, using the same tool-trace shape as
    app._enrich_run (each create_file call carries a `file` dict with a stable
    `s3_uri`). Returns (attachments, oversize_notes):
      * attachments — (filename, media_type, bytes) for files under the Gmail cap.
      * oversize_notes — human lines for files too big to attach (linked instead).
    Unreadable files are skipped silently — a missing byte blob must never block
    the notification itself."""
    attachments: list[tuple[str, str, bytes]] = []
    oversize: list[str] = []
    for call in (result.get("tool_calls") or []):
        f = call.get("file") if isinstance(call, dict) else None
        if not f or not f.get("s3_uri"):
            continue
        filename = f.get("filename") or (storage.key_of(f["s3_uri"]) or "file").rsplit("/", 1)[-1]
        try:
            raw, ctype = storage.get_bytes(f["s3_uri"])
        except Exception as exc:  # noqa: BLE001 — skip a file we can't read
            log.warning("notify: could not read %s: %s", f.get("s3_uri"), exc)
            continue
        if raw is None:
            continue
        if len(raw) > MAX_ATTACH_BYTES:
            link = None
            try:
                link = storage.presign(f["s3_uri"], expires=7 * 24 * 3600)
            except Exception:  # noqa: BLE001
                link = None
            oversize.append(f"  • {filename} (too large to attach){f' — {link}' if link else ''}")
            continue
        attachments.append((filename, ctype or f.get("content_type") or "application/octet-stream", raw))
    return attachments, oversize


def _task_deep_link(task_id: str | None) -> str | None:
    """A link that opens this task's detail in the Schedule tab (see static/app.js
    hash routing). None when no public origin is configured."""
    if not task_id or not Config.PUBLIC_BASE_URL:
        return None
    return f"{Config.PUBLIC_BASE_URL}/#task/{task_id}"


def _headline(reply: str | None) -> str:
    """The one-line result: the first non-empty line of the agent's reply."""
    for line in (reply or "").splitlines():
        line = line.strip()
        if line:
            return line
    return "Task completed."


def deliver_run_result(task: dict[str, Any], run_id: str | None, result: dict[str, Any]) -> None:
    """Email a finished run's result to the task's 'notify to' address(es).

    A no-op when the task has no recipients. Composes a one-line headline + a
    trimmed reply preview + a deep link to the full output, and attaches any files
    the run produced. Never raises — a delivery failure must not lose the already-
    persisted run (the caller also guards this)."""
    recipients = _parse_recipients(task.get("notify_to"))
    if not recipients:
        return

    name = task.get("name") or "Task"
    reply = result.get("reply") or ""
    headline = _headline(reply)
    attachments, oversize = _run_files(result)
    link = _task_deep_link(task.get("task_id"))

    # The subject already carries the headline; the body leads with the full reply
    # (whose first line IS the headline), then attachments and the deep link.
    lines: list[str] = []
    preview = reply.strip() or headline
    if len(preview) > _BODY_PREVIEW_CHARS:
        preview = preview[:_BODY_PREVIEW_CHARS].rstrip() + "\n…(truncated — open the full output below)"
    lines.append(preview)
    if attachments:
        lines += ["", f"Attached: {', '.join(a[0] for a in attachments)}"]
    if oversize:
        lines += ["", "Files too large to attach:"] + oversize
    if link:
        lines += ["", f"Full output & run history: {link}"]
    lines += ["", f'-- Hermes | scheduled task "{name}"']

    subject = f"{name}: {headline}"
    ok = send_system_email(recipients, subject, "\n".join(lines), attachments=attachments or None)
    log.info("notify: delivered run %s of task %s to %d recipient(s): %s",
             run_id, task.get("task_id"), len(recipients), "ok" if ok else "FAILED")


# ── Daily attention report ───────────────────────────────────────────────────
# The report is just a normal scheduled task owned by the owner, so it fires on
# the shared scheduler and shows up in the Schedule tab. Its `notify_to` is the
# owner's own inbox, so the same `deliver_run_result` path emails it. The prompt
# makes the agent's FIRST reply line a headline count (e.g. "3 approvals required
# today") — which becomes the email's one-line summary — and enumerates the items
# below it, so opening the deep link lands on this task where the full list lives.

_DIGEST_TASK_NAME = "Daily attention report"

_DIGEST_PROMPT = (
    "Produce today's attention report for the owner: a concise digest of everything "
    "that needs their eyes or a decision right now.\n\n"
    "Survey what you can see across the connected businesses and recent activity, and "
    "call out: items awaiting the owner's approval or a go/no-go decision; anything "
    "blocked or waiting on the owner; overdue or failing items; and anything else "
    "flagged for attention.\n\n"
    "FORMAT — this is emailed to the owner, so it must be scannable:\n"
    "1. Make the VERY FIRST LINE a one-line headline stating how many items need the "
    "owner's approval/attention today, e.g. \"3 approvals required today\" (or "
    "\"Nothing needs your approval today\" if the list is empty).\n"
    "2. Below it, list each item as a short bullet: what it is, which business, and "
    "what decision is needed.\n"
    "Keep it factual and brief. Do NOT send anything, modify anything, or take any "
    "action — this is a read-only report."
)


def ensure_digest_task() -> str | None:
    """Idempotently ensure the once-a-day attention-report task exists for the
    owner. Returns its task_id (existing or newly created), or None on failure.

    Safe to call on every boot: it looks the task up by name first and only creates
    it if absent. Delivery to the owner is wired via its `notify_to`."""
    from .db import scheduled_tasks as repo
    from . import scheduler

    try:
        for t in repo.list_tasks(None):
            if (t.get("name") or "").strip().lower() == _DIGEST_TASK_NAME.lower():
                return t.get("task_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_digest_task: list failed: %s", exc)
        return None

    tz = Config.SCHEDULER_DEFAULT_TZ
    schedule = {"frequency": "daily", "times": ["07:00"], "days_of_week": []}
    try:
        next_run = scheduler.initial_next_run(schedule, tz, True, None)
        task_id = repo.create_task(
            entity_slug=None,                 # all businesses
            name=_DIGEST_TASK_NAME,
            prompt=_DIGEST_PROMPT,
            schedule=schedule,
            timezone_name=tz,
            allowed_apps=[],                  # all apps available for the survey
            autonomy="readonly",
            enabled=True,
            next_run_at=next_run,
            notify_to=Config.owner_email(),
        )
        if task_id:
            log.info("ensure_digest_task: created '%s' (%s) → %s",
                     _DIGEST_TASK_NAME, task_id, Config.owner_email())
        return task_id
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_digest_task: create failed: %s", exc)
        return None
