"""Meeting copilot — private guidance and prep, grounded in real records, never guessed.

get_guidance() and prepare_brief() deliberately avoid the tool-use agent
(hermes.agent.run_turn): they never call Maton, never send anything, never
speak in the meeting — they only read what's already been indexed by the
ingestion pipeline. Fast on purpose: one semantic search + one plain Claude
call, no tool loop, no extended thinking.

generate_ai_notes() (bottom of this file) is the one deliberate exception —
it DOES call the full tool-use agent, reaching live into Gmail/Drive on the
owner's explicit request, for when the fast path isn't thorough enough. See
its own docstring for why that's a conscious, separate decision.
"""
from __future__ import annotations

import json
import re

import anthropic

from .config import Config
from .search import semantic_search

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _client


_SYSTEM_TEMPLATE = """You are Gandi's private meeting copilot for {business_name}. \
Nobody in the meeting can see or hear you — you are a silent sidebar only Gandi reads.

Your job: given what was just said in the meeting and the retrieved records below \
(real emails, files, and notes from {business_name}'s own history), write SHORT, \
SPECIFIC guidance for Gandi.

Rules — follow exactly:
- Output ONLY bullet points, nothing else. No greeting, no "Here are some thoughts", \
no summary line before or after.
- Each bullet is one of: a question to ask, a fact to verify, a risk to flag, or a \
relevant fact worth knowing right now.
- Every factual claim must name its source in parentheses, e.g. \
"(source: email from Fatima-Ezzahra, Jun 30)" or "(source: floor plan PDF)".
- If nothing in the retrieved records supports a claim, say so — write \
"no record of this — verify live" instead of guessing or inventing anything.
- Keep every bullet under 25 words. Gandi is reading this while actively talking.
- Give at most 4 bullets. Fewer, sharper bullets beat a long list.
- If the retrieved records don't relate to what was just said, your only bullet \
should say so plainly — do not pad with generic advice.

RETRIEVED RECORDS (from {business_name}'s own indexed emails/files — this is your \
only source of ground truth; do not use outside knowledge about this business):
{context}
"""


def _format_context(hits: list[dict]) -> str:
    if not hits:
        return "(no matching records found)"
    lines = []
    for h in hits:
        source = h.get("source_system") or h.get("doc_type") or "record"
        title = h.get("title") or "untitled"
        snippet = (h.get("snippet") or "").strip().replace("\n", " ")[:280]
        lines.append(f"- [{source}] {title}: {snippet}")
    return "\n".join(lines)


def _parse_bullets(text: str) -> list[str]:
    """Pull bullet lines out of the reply. Tolerates -, •, *, or "1." style."""
    bullets = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0] in "-•*":
            bullets.append(line.lstrip("-•* ").strip())
        elif len(line) > 2 and line[0].isdigit() and line[1] in ").":
            bullets.append(line[2:].strip())
    return bullets


def get_guidance(
    *,
    entity_id: str,
    business_name: str,
    latest_text: str,
    notes_so_far: str = "",
    top_k: int = 6,
    max_sensitivity: str = "confidential",
) -> dict:
    """Ground one live meeting moment in the business's own indexed knowledge
    and return short bullet-point guidance. Read-only, fast, private.

    `entity_id` is an entities.slug or uuid (NOT a businesses.slug) — same
    convention as hermes.search.semantic_search. Callers holding a business
    slug (e.g. "nh") must resolve it first via hermes.businesses.to_entity_slug.
    """
    query = (latest_text or notes_so_far[-500:] or "").strip()
    hits: list[dict] = []
    if query:
        try:
            hits = semantic_search(
                entity_id, query, max_sensitivity=max_sensitivity, top_k=top_k,
            )
        except Exception:
            hits = []  # degrade to "no records" rather than failing the turn

    system = _SYSTEM_TEMPLATE.format(
        business_name=business_name, context=_format_context(hits)
    )
    user_message = (
        f"What was just said: {latest_text}\n\n"
        f"Meeting so far (most recent last):\n{notes_so_far[-1500:]}"
        if notes_so_far else f"What was just said: {latest_text}"
    )

    client = _anthropic()
    resp = client.messages.create(
        model=Config.MODEL,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(
        getattr(block, "text", "") for block in resp.content
        if getattr(block, "type", None) == "text"
    )
    bullets = _parse_bullets(text)

    return {
        "bullets": bullets or ([text.strip()] if text.strip() else []),
        "sources": _sources_of(hits),
    }


def _sources_of(hits: list[dict]) -> list[dict]:
    return [
        {
            "title": h.get("title"),
            "source_system": h.get("source_system"),
            "score": h.get("score"),
            "download_url": h.get("download_url"),
        }
        for h in hits
    ]


def _call_claude_json(*, system: str, user_message: str, max_tokens: int, fallback_key: str) -> dict:
    """Call Claude expecting a JSON object back. Tolerates a stray ```json
    fence around the object (models do this despite instructions not to).
    On any parse failure, degrades to {fallback_key: <raw text>} rather than
    raising — a malformed brief/summary is still better than a crashed turn."""
    client = _anthropic()
    resp = client.messages.create(
        model=Config.MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(
        getattr(block, "text", "") for block in resp.content
        if getattr(block, "type", None) == "text"
    ).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {fallback_key: text}


_BRIEF_SYSTEM_TEMPLATE = """You are Gandi's private meeting-preparation assistant for \
{business_name}. You are producing a brief he'll read privately before the meeting starts.

Given the meeting details and the retrieved records below (real emails, files, and notes \
from {business_name}'s own history), write a short prep brief.

Respond with ONLY a single JSON object, no other text, in exactly this shape:
{{
  "background": "2-4 sentence background on this meeting, the attendees, and relevant \
business context, grounded only in the retrieved records below",
  "must_ask_questions": ["short specific question", "..."],
  "risks": ["short risk or thing to watch for", "..."],
  "key_facts": ["short factual point worth having in mind, with its source in parentheses", "..."]
}}

Rules:
- Ground every claim in the retrieved records. If they don't cover something relevant \
(e.g. pricing, scope, prior commitments), say so plainly in that field rather than guessing.
- Keep every list short (2-5 items) and every item under 30 words.
- If the retrieved records are empty or unrelated, say so in "background" and leave the \
other lists mostly empty rather than inventing generic meeting advice.

RETRIEVED RECORDS (from {business_name}'s own indexed emails/files):
{context}
"""


def prepare_brief(
    *,
    entity_id: str,
    business_name: str,
    meeting_title: str,
    attendees: list[dict] | None = None,
    description: str | None = None,
    top_k: int = 8,
    max_sensitivity: str = "confidential",
) -> dict:
    """Generate a pre-meeting brief: background, must-ask questions, risks,
    key facts — plus the sources it drew on. Read-only, same grounding rule
    as get_guidance: only what's already indexed, nothing invented."""
    attendee_names = ", ".join(
        a.get("name") or a.get("email") or "" for a in (attendees or []) if a.get("name") or a.get("email")
    )
    query = " ".join(p for p in (meeting_title, attendee_names, description or "") if p).strip()
    hits: list[dict] = []
    if query:
        try:
            hits = semantic_search(entity_id, query, max_sensitivity=max_sensitivity, top_k=top_k)
        except Exception:
            hits = []

    system = _BRIEF_SYSTEM_TEMPLATE.format(business_name=business_name, context=_format_context(hits))
    user_message = f"Meeting: {meeting_title}\nAttendees: {attendee_names or 'unknown'}"
    if description:
        user_message += f"\nDescription: {description}"

    brief = _call_claude_json(
        system=system, user_message=user_message, max_tokens=900, fallback_key="background",
    )
    brief.setdefault("background", "")
    brief.setdefault("must_ask_questions", [])
    brief.setdefault("risks", [])
    brief.setdefault("key_facts", [])
    return {"brief": brief, "sources": _sources_of(hits)}


_SUMMARY_SYSTEM_TEMPLATE = """You are summarizing a meeting that just happened for \
{business_name}, from Gandi's own live notes/transcript taken during it.

Respond with ONLY a single JSON object, no other text, in exactly this shape:
{{
  "summary": "2-4 sentence summary of what the meeting covered",
  "decisions": ["decision made", "..."],
  "action_items": ["action item, with owner if known", "..."],
  "open_questions": ["question left unresolved", "..."],
  "follow_up_email_draft": "a short, plain draft follow-up email — DRAFT ONLY, never sent automatically"
}}

Rules:
- Base this ONLY on the notes/transcript given below. Do not invent decisions, commitments, \
or facts that aren't in the notes.
- If the notes are too thin to identify decisions/action items/open questions, leave those \
lists empty rather than padding with generic content.
- Keep the summary and email draft concise — this is a working note, not a formal report.
"""


def summarize_meeting(*, business_name: str, turns: list[dict]) -> dict:
    """Post-meeting summary from the turns already captured live. Read-only —
    the follow-up email is always a draft; nothing here sends anything."""
    lines = [t.get("heard_text") for t in (turns or []) if t.get("heard_text")]
    transcript = "\n".join(f"- {line}" for line in lines)
    if not transcript.strip():
        return {
            "summary": "No notes were captured during this meeting.",
            "decisions": [], "action_items": [], "open_questions": [],
            "follow_up_email_draft": "",
        }

    system = _SUMMARY_SYSTEM_TEMPLATE.format(business_name=business_name)
    user_message = f"Meeting notes/transcript (in order):\n{transcript}"

    summary = _call_claude_json(
        system=system, user_message=user_message, max_tokens=900, fallback_key="summary",
    )
    summary.setdefault("summary", "")
    summary.setdefault("decisions", [])
    summary.setdefault("action_items", [])
    summary.setdefault("open_questions", [])
    summary.setdefault("follow_up_email_draft", "")
    return summary


# ─────────────────────────────────────────────────────────────────────────
# AI meeting notes — the ONE deliberate exception to this module's "never
# calls Maton" rule. prepare_brief() above only reads what's already been
# indexed (fast, cheap, always available). This instead runs the full Hermes
# agent (hermes.agent.run_turn) with real tool use, so it can find things the
# autopilot ingestion hasn't picked up yet — reaching live into Gmail and
# Drive on the owner's explicit request. Slower (multiple tool round trips),
# used on demand only, and re-runnable any time the owner wants fresh notes.
# ─────────────────────────────────────────────────────────────────────────

def generate_ai_notes(*, business_slug: str, business_name: str, meeting_title: str) -> dict:
    """Build detailed pre-meeting notes via the full agent: live Gmail search,
    Drive file search, read the contents, write clean bullet-point notes, and
    end with links to the relevant Drive files.

    Runs in a fresh, throwaway conversation — nothing is added to the regular
    chat history sidebar; the caller decides whether/where to persist the
    result (see hermes.db.meetings.save_ai_notes).
    """
    import uuid as _uuid

    from .agent import run_turn  # local import: keep this module's normal
    # (non-tool-use) path free of the agent's heavier import chain

    prompt = (
        f'Fetch all relevant information regarding "{meeting_title}" from my Gmail, '
        f"and any files relevant to {business_name} from Google Drive. Read through "
        f"everything you find and prepare detailed pre-meeting notes in clean language "
        f"and bullet points for this meeting. At the end, include links to all relevant "
        f"files from Drive."
    )
    throwaway_session = str(_uuid.uuid4())
    result = run_turn(throwaway_session, prompt, voice=False, active_business=business_slug)
    return {
        "notes": result.get("reply") or "",
        "tool_calls": result.get("tool_calls") or [],
    }
