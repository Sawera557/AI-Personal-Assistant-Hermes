"""Meeting copilot — private guidance and prep, grounded in real records, never guessed.

get_guidance() and prepare_brief() deliberately avoid the tool-use agent
(hermes.agent.run_turn): they never call Maton, never send anything, never
speak in the meeting — they only read what's already been indexed by the
ingestion pipeline. Fast on purpose: one semantic search + one plain model
call (background policy: NVIDIA, via llm_json), no tool loop, no extended
thinking.

generate_ai_notes() (bottom of this file) is the one deliberate exception —
it DOES call the full tool-use agent, reaching live into Gmail/Drive on the
owner's explicit request, for when the fast path isn't thorough enough. See
its own docstring for why that's a conscious, separate decision.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .llm_json import call_claude_json, call_text
from .search import semantic_search


_SYSTEM_TEMPLATE = """You are Gandi's private meeting copilot for {business_name}. \
Nobody in the meeting can see or hear you — you are a silent sidebar only Gandi reads \
in real time while he's actively talking.

Your job: read what was just said, the meeting so far, the guidance you've already \
given this meeting, this meeting's own prep, and the retrieved records below (real \
emails, files, and notes from {business_name}'s own history) — then think like a sharp \
colleague sitting next to Gandi who has actually read all of that, and tell him \
anything genuinely useful right now.

Rules — follow exactly:
- First check: is "what was just said" actually something said BY SOMEONE IN THE \
MEETING? If instead it's Gandi giving YOU (the copilot) an instruction — e.g. "read my \
emails", "are you there", "give me a response" — or a clearly unrelated aside (a \
different task entirely, small talk unrelated to this business) — output nothing. \
Never force meeting guidance onto input that isn't meeting dialogue.
- Check "GUIDANCE ALREADY GIVEN THIS MEETING" below before writing anything. If a point \
there already covers it — even worded differently — do not say it again in any form. \
If everything worth saying this turn has already been said, output nothing. This is \
the most common way this copilot fails, so check it carefully every time.
- "Output nothing" means EXACTLY THAT: a completely empty response, zero characters. \
Never write a bullet that explains you're not responding, describes your own reasoning \
("no output — this isn't meeting dialogue"), or uses a placeholder like "(nothing to \
add)". A meta-comment about your own silence is exactly the clutter you're avoiding.
- Output ONLY bullet points, nothing else. No greeting, no "Here are some thoughts", \
no summary line before or after.
- At most 3 bullets, often fewer, sometimes none. Every bullet must earn its place: a \
specific, well-reasoned point — never a vague, generic, or hedge-y filler line.
- Actually reason using everything available — connect specific facts, numbers, names, \
and prior commitments from the prep/records to what was just said. "No record of this \
— verify live" is a LAST RESORT for a specific factual claim that truly isn't covered \
anywhere above, not a default answer when you're unsure what else to say.
- Each bullet is a sharp question to ask, a concrete risk, a specific relevant fact, or \
an unaddressed must-ask item from this meeting's prep that still hasn't come up.
- Keep bullets tight but complete — a full, useful thought, not a cryptic fragment. \
Roughly 10-20 words. Do not cite sources inline; they're already listed separately below.
- If there's genuinely nothing useful to add — small talk, logistics, ground already \
covered — output nothing at all rather than forcing a bullet just to say something.

GUIDANCE ALREADY GIVEN THIS MEETING (do not repeat any of these, in any form):
{prior_guidance}

THIS MEETING'S OWN PREP (the brief and AI notes already built for this specific \
meeting — check this first, it's the most directly relevant source):
{meeting_prep}

RETRIEVED RECORDS (from {business_name}'s wider indexed emails/files — broader \
context beyond this one meeting):
{context}

Ground every claim in one of the two sources above; do not use outside knowledge \
about this business.
"""


def _format_prior_bullets(bullets: list[str]) -> str:
    if not bullets:
        return "(none yet — this is the first guidance this meeting)"
    return "\n".join(f"- {b}" for b in bullets)


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


# AI notes can run several KB (a real full-agent pull, multiple documents read
# in full). Truncated here — live guidance needs the gist fast, not the whole
# document verbatim; the full text is one click away in the AI Notes box.
_AI_NOTES_EXCERPT_CHARS = 2500


def _format_meeting_prep(session: dict | None) -> str:
    if not session:
        return "(no prep on file for this session)"
    parts: list[str] = []
    brief = session.get("brief") or {}
    if brief.get("background"):
        parts.append(f"Brief background: {brief['background']}")
    if brief.get("must_ask_questions"):
        parts.append("Brief must-ask questions: " + "; ".join(brief["must_ask_questions"]))
    if brief.get("key_facts"):
        parts.append("Brief key facts: " + "; ".join(brief["key_facts"]))
    ai_notes = session.get("ai_notes")
    if ai_notes:
        excerpt = ai_notes[:_AI_NOTES_EXCERPT_CHARS]
        if len(ai_notes) > _AI_NOTES_EXCERPT_CHARS:
            excerpt += " …(truncated — see the AI Meeting Notes box for the full version)"
        parts.append("AI meeting notes: " + excerpt)
    return "\n\n".join(parts) if parts else "(no prep on file for this session)"


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
    session_id: str | None = None,
    top_k: int = 6,
    max_sensitivity: str = "confidential",
) -> dict:
    """Ground one live meeting moment and return short bullet-point guidance.
    Read-only, private — fast on purpose, so it stays usable mid-conversation.

    Up to three sources feed the answer, fetched CONCURRENTLY (not one-after-
    the-other) so the slowest of them — not the sum — sets the latency:
      1. This meeting's own prep (session_id -> Postgres, brief + AI notes) —
         a single indexed row lookup, effectively free next to a vector search.
      2. The guidance already given earlier this meeting (session_id ->
         Postgres) — without this the model has no memory of its own past
         answers and will keep re-deriving the same obvious point turn after
         turn; seen for real in production (the same question asked 11 turns
         in a row). Also cheap next to the vector search.
      3. The wider pgvector semantic search over everything else indexed for
         this business (the existing, slower path: an embedding call + a
         vector query).

    `entity_id` is an entities.slug or uuid (NOT a businesses.slug) — same
    convention as hermes.search.semantic_search. Callers holding a business
    slug (e.g. "nh") must resolve it first via hermes.businesses.to_entity_slug.
    """
    query = (latest_text or notes_so_far[-500:] or "").strip()

    def _search() -> list[dict]:
        if not query:
            return []
        try:
            return semantic_search(entity_id, query, max_sensitivity=max_sensitivity, top_k=top_k)
        except Exception:
            return []  # degrade to "no records" rather than failing the turn

    def _prep() -> dict | None:
        if not session_id:
            return None
        try:
            from .db.meetings import session_context  # local import: avoid a cycle
            return session_context(session_id)
        except Exception:
            return None

    def _history() -> list[str]:
        if not session_id:
            return []
        try:
            from .db.meetings import prior_bullets  # local import: avoid a cycle
            return prior_bullets(session_id)
        except Exception:
            return []

    tasks: dict[str, object] = {}
    if query:
        tasks["hits"] = _search
    if session_id:
        tasks["prep"] = _prep
        tasks["history"] = _history

    results: dict[str, object] = {"hits": [], "prep": None, "history": []}
    if tasks:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {name: pool.submit(fn) for name, fn in tasks.items()}
            for name, fut in futures.items():
                results[name] = fut.result()

    hits: list[dict] = results["hits"]
    session_data: dict | None = results["prep"]
    prior = results["history"]

    system = _SYSTEM_TEMPLATE.format(
        business_name=business_name,
        prior_guidance=_format_prior_bullets(prior),
        meeting_prep=_format_meeting_prep(session_data),
        context=_format_context(hits),
    )
    user_message = (
        f"What was just said: {latest_text}\n\n"
        f"Meeting so far (most recent last):\n{notes_so_far[-1500:]}"
        if notes_so_far else f"What was just said: {latest_text}"
    )

    # Background policy applies: NVIDIA-only, with fallthrough (see llm_json).
    text = call_text(system=system, user_message=user_message, max_tokens=400,
                     model=Config.GUIDANCE_MODEL)
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

    brief = call_claude_json(
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

    summary = call_claude_json(
        system=system, user_message=user_message, max_tokens=900, fallback_key="summary",
    )
    summary.setdefault("summary", "")
    summary.setdefault("decisions", [])
    summary.setdefault("action_items", [])
    summary.setdefault("open_questions", [])
    summary.setdefault("follow_up_email_draft", "")
    return summary


# ─────────────────────────────────────────────────────────────────────────
# COMPREHENSIVE GUIDANCE SYSTEM
# Pulls from ALL available data sources for founder-grade meeting copilot:
# - Business strategy (goals, roadmap, priorities)
# - Team structure & dependencies (from floormap/org data)
# - Historical decisions and outcomes
# - Risk register and blockers
# - Team capacity and commitments
# ─────────────────────────────────────────────────────────────────────────

_ENHANCED_GUIDANCE_SYSTEM = """You are Gandi's elite meeting copilot — a sharp, well-informed colleague \
sitting invisibly next to him during this meeting.

Your job: read what was just said in this meeting and synthesize guidance from:
1. What this meeting is ABOUT (context below)
2. What was already SAID in this meeting (to avoid repetition)
3. Real business DATA grounding every point (emails, decisions, risks, strategy)

Guide Gandi like you understand:
- His Q3 goals and how this decision serves or conflicts with them
- What teams this decision cascades to and what might break
- Whether this contradicts something decided last month
- What could go wrong and who needs to know
- What question was actually never answered in this conversation
- Whether they even have the bandwidth/budget for this

Output format: ONLY bullet points (0-4 bullets, often 0-2). Each bullet is:
- A specific risk or conflict grounded in your data
- A concrete next step they haven't thought of yet
- A must-ask question that would derail this if unanswered
- A factual constraint (timeline, budget, dependency, person) they seem to be ignoring

RULES (follow exactly):
- Check prior_guidance below FIRST — never repeat a point already made this meeting
- Every claim references something in "Meeting Context" or "Business Data" below
- If it's not grounded, don't say it — this is Gandi's meeting, not generic advice
- Tone: direct colleague, not a summarizer. You're pointing out what matters NOW

PRIOR GUIDANCE (already said this meeting — do not repeat):
{prior_guidance}

MEETING CONTEXT (what's being discussed + attendees + prep):
{meeting_context}

BUSINESS DATA (strategy, risks, team, history):
{business_data}

RETRIEVED RECORDS (emails, files, decisions from this business's indexed history):
{context}
"""


def _fetch_business_data(entity_id: str, max_sensitivity: str = "confidential") -> dict:
    """Fetch strategic, organizational, and risk data for this business.

    Returns a dict with keys like:
    - q3_goals: current strategic priorities
    - roadmap: known upcoming projects/milestones
    - team_structure: org/team data (from floormap if available)
    - known_risks: identified technical/business risks
    - blockers: current issues/escalations
    - capacity: team utilization, budget constraints
    - recent_decisions: decisions made in past 30 days
    """
    # Placeholder structure — fills in from real sources:
    # - Postgres: strategic goals/roadmap
    # - Semantic search: past decisions, identified risks
    # - Floormap/org data: team structure and dependencies
    # - Calendar/email patterns: team capacity and utilization

    # TODO: query Postgres for business.goals, business.roadmap, business.risks
    # TODO: semantic_search for "decision", "blocker", "risk" in this entity
    # TODO: fetch floormap/org structure if available
    # TODO: aggregate team capacity signals from calendar/meetings
    #
    # None of those sources is wired yet, so this returns NOTHING rather than
    # descriptive stand-ins. It used to return strings like
    # "(database lookup: current quarterly goals)", which reached the prompt as
    # if they were real records — and the model, asked how a moment "conflicts
    # with his Q3 goals", invented Q3 targets to answer. An empty dict renders
    # as "(business data unavailable)", which the prompt handles honestly.
    return {}


def get_guidance_comprehensive(
    *,
    entity_id: str,
    business_name: str,
    latest_text: str,
    notes_so_far: str = "",
    session_id: str | None = None,
    attendees: list[dict] | None = None,
    meeting_title: str = "",
    top_k: int = 8,
    max_sensitivity: str = "confidential",
) -> dict:
    """Enhanced guidance pulling from all business data sources.

    Like get_guidance() but enriched with strategic context, team data,
    risk awareness, and business constraints. Same call signature as
    get_guidance for backwards compatibility, but returns deeper guidance.
    """
    query = (latest_text or notes_so_far[-500:] or "").strip()

    def _search() -> list[dict]:
        if not query:
            return []
        try:
            return semantic_search(entity_id, query, max_sensitivity=max_sensitivity, top_k=top_k)
        except Exception:
            return []

    def _prep() -> dict | None:
        if not session_id:
            return None
        try:
            from .db.meetings import session_context  # local import: avoid a cycle
            return session_context(session_id)
        except Exception:
            return None

    def _history() -> list[str]:
        if not session_id:
            return []
        try:
            from .db.meetings import prior_bullets  # local import: avoid a cycle
            return prior_bullets(session_id)
        except Exception:
            return []

    def _business() -> dict:
        return _fetch_business_data(entity_id, max_sensitivity=max_sensitivity)

    tasks: dict[str, object] = {}
    if query:
        tasks["hits"] = _search
    if session_id:
        tasks["prep"] = _prep
        tasks["history"] = _history
    tasks["business"] = _business

    results: dict[str, object] = {"hits": [], "prep": None, "history": [], "business": {}}
    if tasks:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {name: pool.submit(fn) for name, fn in tasks.items()}
            for name, fut in futures.items():
                results[name] = fut.result()

    hits: list[dict] = results["hits"]
    session_data: dict | None = results["prep"]
    prior = results["history"]
    business_data = results["business"]

    # Format meeting context (attendees, title, prep notes)
    meeting_ctx_parts = []
    if meeting_title:
        meeting_ctx_parts.append(f"Meeting: {meeting_title}")
    if attendees:
        names = [a.get("name") or a.get("email") or "" for a in attendees if a.get("name") or a.get("email")]
        if names:
            meeting_ctx_parts.append(f"Attendees: {', '.join(names)}")
    prep_note = _format_meeting_prep(session_data)
    if prep_note and prep_note != "(no prep on file for this session)":
        meeting_ctx_parts.append(f"Prep: {prep_note}")
    meeting_context = "\n".join(meeting_ctx_parts) if meeting_ctx_parts else "(meeting context unavailable)"

    # Format business data
    business_context_lines = []
    for key, value in business_data.items():
        if value and not str(value).startswith("("):  # never pass a "(lookup: …)" stand-in off as data
            business_context_lines.append(f"- {key.replace('_', ' ').title()}: {value}")
    business_context = "\n".join(business_context_lines) if business_context_lines else "(business data unavailable)"

    system = _ENHANCED_GUIDANCE_SYSTEM.format(
        prior_guidance=_format_prior_bullets(prior),
        meeting_context=meeting_context,
        business_data=business_context,
        context=_format_context(hits),
    )
    user_message = (
        f"What was just said: {latest_text}\n\n"
        f"Meeting so far (most recent last):\n{notes_so_far[-1500:]}"
        if notes_so_far else f"What was just said: {latest_text}"
    )

    text = call_text(system=system, user_message=user_message, max_tokens=400,
                     model=Config.GUIDANCE_MODEL)
    bullets = _parse_bullets(text)

    return {
        "bullets": bullets or ([text.strip()] if text.strip() else []),
        "sources": _sources_of(hits),
    }


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
