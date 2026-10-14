"""Ask a model which Jira issue a piece of work belongs to.

This is the fallback for work that carries no explicit ticket reference. An
explicit key in the branch or commit message is a human stating intent and is
never routed through here — inference only fills the silence where the current
behaviour is to skip the push entirely.

Guardrails, because this writes to a live board on the strength of a guess:

* **Closed set.** The model picks from candidates fetched out of Jira and its
  answer is validated back against those keys. A key it invents is discarded,
  so it cannot comment on an issue that does not exist.
* **"None" is a first-class answer.** The prompt says so explicitly and the
  threshold enforces it. Skipping is the status quo, so declining costs nothing;
  a wrong attribution puts false progress on someone else's ticket.
* **Disclosed, never disguised.** An AI-matched comment says it was AI-matched,
  with the confidence and the model's stated reason, so a human reading the
  ticket can see the attribution is inferred and challenge it.
* **Cheap.** Runs on the smallest OpenAI model — one short call per otherwise
  unattributable push, and no call at all when a key was found the normal way.

Degrades to "no match" on every failure path: no API key, network error,
malformed JSON, unknown key, low confidence.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import config

log = logging.getLogger("gh_jira.matcher")

_SYSTEM = """You map a code change onto the Jira issue it belongs to.

You will be given a code change (branch, commit messages, changed files, pull \
request text) and a numbered list of candidate Jira issues.

Return ONLY a JSON object:
{"issue_key": "<key from the candidate list, or null>", "confidence": <0.0-1.0>, \
"reason": "<one sentence, max 25 words>"}

Rules:
- issue_key MUST be copied exactly from the candidate list, or be null.
- Return null whenever no candidate clearly describes this change. This is the \
correct, expected answer for routine work like dependency bumps, typo fixes, \
formatting, CI tweaks and refactors that no ticket tracks.
- Judge on what the code actually does, not on shared vocabulary. Two items \
both mentioning "webhook" are not a match unless the change does the work the \
issue describes.
- confidence is your probability that a reviewer would agree. Below 0.7 means \
you are guessing - prefer null.
- A wrong issue is far worse than no issue. When torn, return null."""


# A SECOND, separate call — not extra fields on the matching one.
#
# Both decisions were tried in a single call and it made the model worse at
# each: asked to match-or-propose, it proposed a duplicate of a ticket it had
# previously matched at 90%, and simultaneously declined to propose anything for
# genuinely untracked work. Two focused calls, only on the pushes that need
# them, are both more accurate and still trivially cheap on a nano model.
_PROPOSE_SYSTEM = """You turn a code change into a Jira ticket.

You will be given a code change that matched no existing issue on the board.

Return ONLY a JSON object:
{"summary": "<imperative work item, or null>", "description": "<2-3 sentences \
on what the change does and why, or null>"}

Rules:
- summary reads as a work item someone could pick up: "Add retry handling to \
the Pinecone ingestion worker". Not a commit log, no ticket keys, no "WIP".
- Return null for BOTH fields when the change is routine upkeep nobody wants a \
ticket for: dependency bumps, typo fixes, formatting, lockfiles, CI config, \
comment-only edits, version bumps.
- Anything that changes behaviour, adds a capability, or fixes a real bug DOES \
deserve a ticket. Err towards proposing one for substantive code changes."""


@dataclass(frozen=True)
class AiMatch:
    key: str
    confidence: float
    reason: str
    model: str


@dataclass(frozen=True)
class AiProposal:
    """A new ticket the model thinks this work deserves, when none exists."""

    summary: str
    description: str
    model: str


def _describe_change(update) -> str:
    """The work signals, as compact text for the prompt."""
    lines = [f"Repository: {update.repo}"]
    if update.branch:
        lines.append(f"Branch: {update.branch}")
    lines.append(f"Event: {update.event}" + (f" ({update.action})" if update.action else ""))

    for label, value in update.facts:
        if label in {"Author", "Pushed by", "Written by", "Reviewers requested"}:
            lines.append(f"{label}: {value}")

    if update.commits:
        lines.append("Commit messages:")
        lines += [f"  - {c.get('message', '')}" for c in update.commits]

    if getattr(update, "files", None):
        shown = update.files[:40]
        lines.append(f"Changed files ({len(update.files)}):")
        lines += [f"  - {path}" for path in shown]
        if len(update.files) > len(shown):
            lines.append(f"  ...and {len(update.files) - len(shown)} more")

    if getattr(update, "pr_text", None):
        lines.append(f"Pull request: {update.pr_text[:1000]}")

    return "\n".join(lines)


def _describe_candidates(candidates: list[dict]) -> str:
    out = []
    for c in candidates:
        bits = [f"{c['key']}: {c['summary']}"]
        if c.get("status"):
            bits.append(f"status={c['status']}")
        if c.get("epic"):
            bits.append(f"epic={c['epic']}")
        if c.get("labels"):
            bits.append(f"labels={','.join(c['labels'][:6])}")
        out.append("- " + " | ".join(bits))
    return "\n".join(out)


def _call_model(system: str, user: str, model: str) -> dict | None:
    """One JSON-mode chat completion. None on any failure."""
    from hermes.config import Config  # deferred: keeps this importable bare

    api_key = config.openai_api_key() or Config.OPENAI_API_KEY
    if not api_key:
        log.info("No OpenAI key configured — AI issue matching is inert")
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=Config.OPENAI_BASE_URL)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_completion_tokens=200,
            timeout=20,
        )
        raw = (resp.choices[0].message.content or "").strip()
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — a matcher outage must not fail the webhook
        log.warning("AI issue matching call failed", exc_info=True)
        return None


def assess(update, candidates: list[dict]) -> tuple[AiMatch | None, AiProposal | None]:
    """Which existing ticket is this, or what new one should exist?

    Two calls, and the second only runs when the first found nothing. Returns
    `(match, proposal)` with at most one populated. `(None, None)` means
    "nothing to do here" — the right answer for routine upkeep, and the same
    outcome as before this path existed.
    """
    model = config.ai_model()
    change = _describe_change(update)

    if candidates:
        parsed = _call_model(
            _SYSTEM,
            f"CODE CHANGE\n{change}\n\nCANDIDATE JIRA ISSUES\n{_describe_candidates(candidates)}",
            model,
        )
        if isinstance(parsed, dict):
            found = _validate_match(parsed, candidates, model)
            if found:
                return found, None

    if not config.auto_create():
        return None, None

    proposed = _call_model(_PROPOSE_SYSTEM, f"CODE CHANGE\n{change}", model)
    if not isinstance(proposed, dict):
        return None, None
    return None, _validate_proposal(proposed, model)


def match(update, candidates: list[dict]) -> AiMatch | None:
    """Pick the issue this change belongs to, or None to skip."""
    if not candidates:
        return None
    return assess(update, candidates)[0]


def _validate_proposal(parsed: dict, model: str) -> AiProposal | None:
    """Accept a proposed new ticket only if it is actually usable."""
    summary = str(parsed.get("summary") or parsed.get("proposed_summary") or "").strip()
    if not summary or summary.lower() in {"null", "none"}:
        return None
    if len(summary) < 8:
        # Too thin to be a ticket title anyone could act on.
        log.info("Discarded a proposed summary that was too short: %r", summary)
        return None
    description = str(parsed.get("description") or parsed.get("proposed_description") or "").strip()
    return AiProposal(summary=summary[:200], description=description[:1000], model=model)


def _validate_match(parsed: dict, candidates: list[dict], model: str) -> AiMatch | None:
    key = parsed.get("issue_key")
    if not key or not isinstance(key, str):
        log.info("AI matcher declined: no candidate fits this change")
        return None

    key = key.strip().upper()
    valid = {c["key"].upper(): c["key"] for c in candidates}
    if key not in valid:
        # The model named something outside the closed set. Discard rather than
        # trust it — this is exactly the fabrication the closed set exists to stop.
        log.warning("AI matcher returned %r, which is not a candidate — discarded", key)
        return None

    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    floor = config.ai_min_confidence()
    if confidence < floor:
        log.info("AI matched %s at %.2f, below the %.2f floor — skipped", key, confidence, floor)
        return None

    reason = str(parsed.get("reason") or "").strip()[:200] or "no reason given"
    return AiMatch(key=valid[key], confidence=confidence, reason=reason, model=model)
