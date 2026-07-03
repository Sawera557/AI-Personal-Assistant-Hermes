"""Meeting copilot — private, real-time guidance grounded in what's already
been read into the knowledge base (Pinecone + Postgres), never guessed.

This is deliberately NOT the tool-use agent (hermes.agent.run_turn). It never
calls Maton, never sends anything, never speaks in the meeting — it only reads
what's already been indexed by the ingestion pipeline and writes short
bullet-point guidance to a private screen only the owner sees. Fast on
purpose: one semantic search + one plain Claude call, no tool loop, no
extended thinking — the retrieval already happened ahead of time (autopilot
ingestion), so the live moment only has to check what's already on the shelf.
"""
from __future__ import annotations

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
        "sources": [
            {
                "title": h.get("title"),
                "source_system": h.get("source_system"),
                "score": h.get("score"),
                "download_url": h.get("download_url"),
            }
            for h in hits
        ],
    }
