"""The Hermes agent: a tool-use loop over the Maton tools, on Claude or OpenAI.

Which model runs the loop is a global, switchable setting (see hermes.models)
rather than hardcoded — but everything the agent can DO (Maton/WATI tools,
business scoping, streaming, conversation memory) is identical either way.

Conversation history is always stored in Anthropic's content-block shape
(see `_dump`), regardless of which provider generated it. That keeps the rest
of the app (storage, transcript replay, tool dispatch) provider-agnostic, and
means switching models mid-conversation doesn't lose history — `_to_openai_messages`
below just translates that shared format into OpenAI's wire format per call.
"""
from __future__ import annotations

import datetime
import json
from typing import Any

import anthropic
import openai

from . import models
from .attachments import ATTACHMENT_TEXT_PREFIX
from .config import Config
from .memory import store
from .memory_retrieve import build_memory_block
from .tools import LOCAL_TOOLS, build_tools, dispatch, tool_status

_anthropic_client: anthropic.Anthropic | None = None
_openai_client: openai.OpenAI | None = None


def _anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _openai() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=Config.OPENAI_API_KEY, base_url=Config.OPENAI_BASE_URL)
    return _openai_client


# Extra guidance for voice mode — the reply is spoken aloud on a phone-style call.
_VOICE_STYLE = (
    "\n\nVOICE CALL MODE: You are on a one-to-one, real-time voice call with the "
    "user, like a phone call. Your reply will be spoken aloud, so answer short, "
    "simple, and natural — the way you'd actually talk to someone. Keep it to a "
    "sentence or two unless they ask for detail. Do NOT use markdown, headings, "
    "bullet lists, tables, code blocks, emoji, or long quotes or URLs (they sound "
    "wrong read out). Use plain spoken sentences. Lead with the answer, and if you "
    "need to confirm a write action, ask in one short spoken question. "
    "Always respond in English, even if the user's words are unclear or sound like "
    "another language."
)


def _attachments_block(attachments: list[dict[str, Any]] | None) -> str:
    """Render the 'files shared in this conversation' ledger so the model can
    read any of them on any later turn via the read_document tool. Without this,
    the S3 pointer for a shared WhatsApp doc is invisible after its own turn."""
    if not attachments:
        return ""
    lines = [
        "\n\nFiles shared in this conversation (read any with the `read_document` "
        "tool, passing the exact s3_uri):",
    ]
    for a in attachments:
        name = a.get("filename") or "file"
        uri = a.get("s3_uri") or ""
        lines.append(f"  - {name}  [{uri}]")
    return "\n".join(lines)


def _system_base(active_business: str | None = None) -> str:
    """The STABLE part of the system prompt — static operating guidelines, the
    connected-app catalog, and the (per-conversation-constant) business scope.

    Deliberately excludes anything that changes turn-to-turn (today's date, the
    MEMORY block, EARLIER CONVERSATIONS / RETRIEVED CONTEXT, the attachments
    ledger) so this text can be a byte-stable prompt caching prefix. See
    `_system_blocks`."""
    base = (
        "You are Hermes, a capable AI executive assistant. You help the user get "
        "real work done across their connected business apps, the web, and their "
        "documents.\n\n"
        "Connected apps are reachable through the Maton gateway tools. Known app slugs:\n"
        f"{Config.app_catalog()}\n\n"
        "Operating guidelines:\n"
        "- Use `maton_request` to read from and act on these apps via their native APIs.\n"
        "- If you're unsure whether an app is connected, call `maton_list_connections`. "
        "If it isn't connected, call `maton_create_connection` and give the user the "
        "returned OAuth link to authorize it.\n"
        "- Always discover record IDs with read-only GET calls before performing a write.\n"
        "- To email a Zoom meeting invite, use `zoom_send_invite` (not maton_request) — it "
        "pulls the real join link straight from Zoom so it's never mistyped, and it "
        "automatically CCs the user's own email too. Use `send_email` for a plain email with "
        "no attachment.\n"
        "- To email a file — whether the user gave you one or asked you to generate it (e.g. "
        "'send Amal the renewal letter as a PDF') — use `email_with_attachment`, never "
        "hand-build attachments via maton_request. It runs a fixed two-step flow: first call "
        "it WITHOUT confirm to generate the file (PDF/DOCX/XLSX or text — you write the "
        "content), attach it, and hold the email as a Gmail draft; relay the returned preview "
        "and ask the user to approve; only after they explicitly approve, call it again with "
        "confirm=true and the returned draft_id to send exactly what they reviewed. Files over "
        "~25MB are auto-shared via a Drive link instead of attached.\n"
        "- When asked what meetings/events are coming up, call BOTH `calendar_list_upcoming` "
        "and `zoom_list_meetings` (not maton_request) and merge the results into one answer — "
        "a meeting may only exist in one of the two (e.g. a Zoom call with no calendar invite, "
        "or a calendar event with no Zoom link). Only skip one if the user specifically asks "
        "about just Calendar or just Zoom.\n"
        "- For any write or destructive action (sending email, sending a WhatsApp/WATI "
        "message or file, assigning a WATI operator, creating/editing calendar events, "
        "modifying QuickBooks data, deleting Drive files, etc.), first clearly summarize "
        "exactly what you will do, then STOP and ask the user to confirm. Only perform "
        "the write after they explicitly approve.\n"
        "- Never substitute one business's data or connected account for another's. If a "
        "business isn't connected yet, tell the user plainly instead of guessing or using "
        "a different business's account.\n"
        "- Before saying you lack context on a business's specific rules, background, or "
        "policies, call `search_business_knowledge` — that's where manually-added business "
        "context (uploaded notes/files) lives, separate from the MEMORY section below.\n"
        "- When asked to generate a document, report, note, CSV, or any other file the user "
        "can download, write the full content yourself and call `create_file` to save it and "
        "get a real download link — never invent a URL, and always share the returned link "
        "back to the user in your reply.\n"
        "- When you use web search, cite the sources.\n"
        "- If a tool result is marked truncated (a `_truncation` key or a <<TRUNCATED>> "
        "marker), you have not seen the omitted part. Never describe, quote, count, or "
        "answer questions about content past the marker or beyond a listed cut — say the "
        "result was truncated and fetch the rest before answering.\n"
        "- GROUNDING & ATTRIBUTION (non-negotiable — the user demands zero fabrication and "
        "zero assumptions). You may be given several context blocks below; attribute each "
        "correctly and never blur them:\n"
        "    - BUSINESS CONTEXT PACK = standing configured facts about the business. Refer to "
        "these as standing context, NEVER as something the user just told you.\n"
        "    - MEMORY = durable saved facts; may be stale — confirm with a tool before stating "
        "as a current fact.\n"
        "    - EARLIER CONVERSATIONS = what was actually said in past chats. ONLY this block "
        "(or the user's current message) lets you say 'you told me', 'you said', 'we "
        "discussed', or 'as you mentioned' — and cite its date when you do.\n"
        "    - RETRIEVED CONTEXT / `search_business_knowledge` = documents retrieved this turn; "
        "refer to them as retrieved/from the document, never as memory or as something the "
        "user said.\n"
        "    - Live tool results = current truth.\n"
        "  Never present a context-pack or retrieved-document fact as something the user said "
        "or as a memory of your conversation. Do NOT state any specific — a name, address, "
        "amount, date, ID, or count — unless it appears in one of these blocks or a tool result "
        "THIS turn. If you have only part of a detail, give exactly that part and stop; never "
        "complete or fill in the rest from assumption, even if you are fairly confident. When "
        "you use a recalled or retrieved fact, say briefly where it came from so the user can "
        "tell recall from inference.\n"
        "- Lead with the answer; be concise and practical."
    )
    if active_business == "all":
        base += (
            "\n\nBusiness scope: the user has 'All Businesses' selected. Every Maton "
            "tool call must include a `business` argument naming which connected "
            "business's account to use — pick the one relevant to what's being asked, "
            "or ask the user to clarify if it's ambiguous."
        )
    elif active_business:
        base += (
            f"\n\nBusiness scope: this conversation is locked to business '{active_business}'. "
            "All Maton tool calls automatically use that business's connected account. "
            "Stay within this business: do not pull in, enumerate, or report another business's "
            "data, tenants, or accounts unless the user explicitly asks for a cross-business "
            "view. If a memory or retrieved snippet from a DIFFERENT business surfaces, say it "
            f"belongs to that other business — never fold it into '{active_business}'."
        )
    return base


def _system_volatile(
    memory_block: str = "",
    attachments: list[dict[str, Any]] | None = None,
    context_pack: str = "",
    voice: bool = False,
    retrieved_context: str = "",
    conversation_block: str = "",
) -> str:
    """The VOLATILE part of the system prompt — everything that changes turn to
    turn (today's date, the BUSINESS CONTEXT PACK, the per-turn MEMORY block,
    EARLIER CONVERSATIONS, RETRIEVED CONTEXT, the growing attachments ledger) or
    per-request (voice style). Kept AFTER the cached prefix so it never
    invalidates the cache."""
    today = datetime.date.today().isoformat()
    parts = [f"Today's date is {today}."]
    if context_pack:
        parts.append(
            "BUSINESS CONTEXT PACK (authoritative standing facts and rules — "
            "trust these over memory or retrieval; if a tool result contradicts "
            "them, say so explicitly):\n" + context_pack
        )
    if memory_block:
        parts.append(
            "MEMORY — saved facts from PAST conversations (preferences, business "
            "rules, standing decisions). Preferences and standing decisions are "
            "reliable. But anything here that can change over time — contact numbers, "
            "balances, message contents, current status, who said what — may be STALE: "
            "treat it as background, NOT as a live retrieval, and confirm it with a tool "
            "call before stating it as a current fact.\n" + memory_block
        )
    if conversation_block:
        parts.append(conversation_block)
    if retrieved_context:
        parts.append(retrieved_context)
    text = "\n\n".join(parts)
    attach = _attachments_block(attachments)
    if attach:
        text += attach
    if voice:
        text += _VOICE_STYLE
    return text


def _system_prompt(
    voice: bool = False,
    active_business: str | None = None,
    memory_block: str = "",
    attachments: list[dict[str, Any]] | None = None,
    context_pack: str = "",
    retrieved_context: str = "",
    conversation_block: str = "",
) -> str:
    """Plain string form of the full system prompt (stable base + volatile tail).

    Kept for the OpenAI path and callers/tests that want one string. The Anthropic
    path uses `_system_blocks` instead so it can place a cache breakpoint between
    the two parts."""
    volatile = _system_volatile(
        memory_block, attachments, context_pack, voice, retrieved_context, conversation_block
    )
    return _system_base(active_business) + "\n\n" + volatile


def _system_blocks(
    voice: bool = False,
    active_business: str | None = None,
    memory_block: str = "",
    attachments: list[dict[str, Any]] | None = None,
    context_pack: str = "",
    retrieved_context: str = "",
    conversation_block: str = "",
    cache: bool | None = None,
) -> list[dict[str, Any]]:
    """Structured system prompt for the Anthropic Messages API: a stable text
    block (optionally marked with `cache_control`) followed by a volatile text
    block. The breakpoint sits at the end of the stable block, so the tool schema
    + static guidelines are reused from cache while today's date / MEMORY / EARLIER
    CONVERSATIONS / RETRIEVED CONTEXT / attachments (which change every turn) stay
    after it and never invalidate it."""
    if cache is None:
        cache = Config.PROMPT_CACHE
    stable: dict[str, Any] = {"type": "text", "text": _system_base(active_business)}
    if cache:
        stable["cache_control"] = {"type": "ephemeral"}
    blocks: list[dict[str, Any]] = [stable]
    volatile = _system_volatile(
        memory_block, attachments, context_pack, voice, retrieved_context, conversation_block
    )
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


def _dump(content: Any) -> list[dict[str, Any]]:
    """Convert SDK content blocks to plain dicts for storage and replay."""
    out = []
    for block in content:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        else:
            out.append(block)
    return out


def _text_of(content: list[dict[str, Any]]) -> str:
    parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _thinking_of(content: list[dict[str, Any]]) -> str:
    parts = [b.get("thinking", "") for b in content if b.get("type") == "thinking"]
    return "\n".join(p for p in parts if p).strip()


def _strip_thinking(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop thinking blocks — required before prefilling a partial assistant turn
    for continuation, since extended thinking forbids prefilled assistant turns."""
    return [b for b in content if b.get("type") != "thinking"]


def _merge_continuation(prev: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
    """Fold a continuation's content into the truncated assistant message in place
    so the resumed reply reads as one message. Trailing text of `prev` is joined
    with the continuation's text; any non-text continuation blocks are appended."""
    for block in new:
        if block.get("type") == "text":
            last_text = next((b for b in reversed(prev) if b.get("type") == "text"), None)
            if last_text is not None:
                last_text["text"] = f"{last_text.get('text', '')}{block.get('text', '')}"
            else:
                prev.append(block)
        else:
            prev.append(block)


# ---- OpenAI translation --------------------------------------------------
# Everything below exists so the stored conversation (Anthropic content-block
# shape) and the tool definitions (Anthropic input_schema shape) can also
# drive an OpenAI chat-completions call. Nothing else in the file needs to
# know which provider is active.

def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic tool defs -> OpenAI function-calling defs.

    Anthropic's server-side tools (web_search/web_fetch) have no input_schema
    and no OpenAI equivalent here, so they're dropped — web browsing simply
    isn't available while an OpenAI model is active.
    """
    out = []
    for t in tools:
        schema = t.get("input_schema")
        if not schema:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": schema,
                },
            }
        )
    return out


def _openai_user_block(block: dict[str, Any]) -> dict[str, Any] | None:
    btype = block.get("type")
    if btype == "text":
        return {"type": "text", "text": block.get("text", "")}
    if btype == "image":
        source = block.get("source", {})
        if source.get("type") == "base64":
            url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
            return {"type": "image_url", "image_url": {"url": url}}
        return None
    if btype == "document":
        # OpenAI's chat-completions API has no direct PDF input; degrade to a
        # text note rather than silently dropping the attachment.
        title = block.get("title", "file")
        return {
            "type": "text",
            "text": (
                f"[Attached document '{title}' — this model can't read PDF content "
                "directly. Ask the user to paste the relevant text, or switch to a "
                "Claude model to read it.]"
            ),
        }
    return None


def _to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the stored Anthropic-block message history into OpenAI's
    chat-completions message format for one API call."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "user":
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            if tool_results:
                for tr in tool_results:
                    c = tr.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": c if isinstance(c, str) else json.dumps(c),
                        }
                    )
                # Real file content (e.g. drive_read_file's document/image block)
                # rides alongside tool_result blocks in the same stored turn — the
                # "tool" role can't carry it, so it's translated into a follow-up
                # user message instead of being silently dropped.
                extra = [b for b in content if b.get("type") != "tool_result"]
                parts = [b for b in (_openai_user_block(b) for b in extra) if b]
                if parts:
                    out.append({"role": "user", "content": parts})
                continue
            parts = [b for b in (_openai_user_block(b) for b in content) if b]
            out.append({"role": "user", "content": parts or ""})
            continue

        # assistant
        text = _text_of(content)
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_uses:
            msg["tool_calls"] = [
                {
                    "id": tu["id"],
                    "type": "function",
                    "function": {"name": tu["name"], "arguments": json.dumps(tu.get("input") or {})},
                }
                for tu in tool_uses
            ]
        out.append(msg)
    return out


# Normalize OpenAI's finish_reason vocabulary onto Anthropic's stop_reason
# vocabulary, so the shared tool-dispatch loop in stream_turn() only has to
# handle one set of values.
_OPENAI_STOP_MAP = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


#: Valid per-request reasoning-effort levels the frontend may pass. Anything
#: else (None, "", a typo) falls back to the platform default Config.EFFORT.
_VALID_EFFORTS = frozenset({"low", "medium", "high"})


def normalize_effort(effort: str | None) -> str:
    """Coerce a caller-supplied effort to a valid level, else Config.EFFORT."""
    if isinstance(effort, str) and effort.strip().lower() in _VALID_EFFORTS:
        return effort.strip().lower()
    return Config.EFFORT


#: Words that signal a turn wants deeper reasoning — bumps auto-effort to "high".
_HIGH_EFFORT_HINTS = (
    "analyz", "compare", "comparison", "evaluate", "assess", "strateg", "plan",
    "forecast", "project", "model", "reconcile", "debug", "diagnos", "root cause",
    "design", "architect", "trade-off", "tradeoff", "pros and cons", "step by step",
    "comprehensive", "in depth", "in-depth", "why ", "explain how", "optimi",
    "recommend", "draft a", "write a", "summarize", "reason", "calculate", "research",
)
#: Phrases that signal a quick, shallow turn — pulls auto-effort down to "low".
#: Matched at word boundaries (whole message or hint followed by a space), so
#: "hi" won't fire on "history" nor "yes" on "yesterday".
_LOW_EFFORT_HINTS = (
    "hi", "hello", "hey", "thanks", "thank you", "yes", "no", "ok", "okay",
    "what time", "when is", "list", "show me", "status", "is", "are", "how many",
)


def _is_attachment_block(block: dict[str, Any]) -> bool:
    """True if a content block is a file the user attached — an image/document
    block, or a text block that inlines a file upload (ATTACHMENT_TEXT_PREFIX)."""
    if block.get("type") != "text":
        return True
    text = block.get("text")
    return isinstance(text, str) and text.startswith(ATTACHMENT_TEXT_PREFIX)


def _starts_with_hint(text: str, hint: str) -> bool:
    """Word-boundary prefix match: the whole text equals the hint, or the hint is
    followed by a space. Avoids 'hi' matching 'history', 'is' matching 'island'."""
    return text == hint or text.startswith(hint + " ")


def _auto_effort(user_content: list[dict[str, Any]] | None, has_attachments: bool) -> str:
    """Pick a reasoning-effort level from the turn's own signals, when the caller
    didn't set one explicitly. Heuristic and deliberately conservative — it only
    moves off the platform default for clearly-simple or clearly-hard turns.

    Signals: attachments (files/images to reason over → at least medium),
    message length, and depth-hint keywords. Returns a valid effort level."""
    text = _text_of(user_content or []).lower()
    words = len(text.split())

    # Attachments almost always mean "reason over this content" — never trivial.
    if has_attachments:
        return "high" if (words > 40 or any(h in text for h in _HIGH_EFFORT_HINTS)) else "medium"

    if any(hint in text for hint in _HIGH_EFFORT_HINTS) or words > 80:
        return "high"
    # Short and hint-free (a greeting, a one-line lookup) — keep it fast/cheap.
    if words <= 12 and (words == 0 or any(_starts_with_hint(text, h) for h in _LOW_EFFORT_HINTS)):
        return "low"
    return Config.EFFORT


def resolve_effort(
    effort: str | None,
    user_content: list[dict[str, Any]] | None = None,
    has_attachments: bool = False,
) -> str:
    """Resolve the effort for a turn: an explicit low/medium/high from the caller
    wins; "auto" (or None / anything unrecognized) defers to _auto_effort()."""
    if isinstance(effort, str) and effort.strip().lower() in _VALID_EFFORTS:
        return effort.strip().lower()
    return _auto_effort(user_content, has_attachments)


def _cache_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of the tools array with a `cache_control` breakpoint on the
    last tool, so the whole (byte-stable) tool schema is cached. Shallow-copies
    the last entry rather than mutating `build_tools()`'s output in place — the
    same list is reused across the turn's tool round-trips and by the OpenAI path.
    Server-side tool blocks (web_search/web_fetch) are left untagged; the last
    entry is whatever `build_tools` put there and is a normal tool definition."""
    if not tools or not Config.PROMPT_CACHE:
        return tools
    tagged = list(tools)
    last = dict(tagged[-1])
    last["cache_control"] = {"type": "ephemeral"}
    tagged[-1] = last
    return tagged


def _anthropic_turn(
    model_id: str,
    tools: list[dict[str, Any]],
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    effort: str | None = None,
    max_tokens: int | None = None,
    continuation: bool = False,
):
    """Stream one Claude API call. Yields text deltas AND, for models that
    support it, live thinking deltas ({"type": "thinking", "text": "<delta>"})
    as Claude's real reasoning is generated — not a synthetic status string.
    Ends with a final {"type": "_final", "content": [...], "stop_reason": "..."}
    event.

    Uses the SDK's raw event iterator (`for event in stream`) instead of the
    `.text_stream` convenience helper, since that helper only surfaces text
    deltas and silently drops thinking deltas — the two can't both be
    consumed from the same stream (`.text_stream` is itself a generator over
    the same underlying iterator).
    """
    # Haiku rejects adaptive thinking outright ("adaptive thinking is not
    # supported on this model") — only the more capable tiers support it, so
    # only those models ever produce a live "thinking" event.
    # `continuation` means we're resuming a reply that the token cap cut off, by
    # prefilling the partial assistant turn. Extended thinking forbids prefilling
    # an assistant turn, so continuation calls run with thinking (and effort) off;
    # the caller strips the partial's thinking blocks to match.
    extra: dict[str, Any] = {}
    if "haiku" not in model_id.lower() and not continuation:
        extra["thinking"] = {"type": "adaptive", "display": "summarized"}
        extra["output_config"] = {"effort": normalize_effort(effort)}

    with _anthropic().messages.stream(
        model=model_id,
        max_tokens=max_tokens or Config.MAX_TOKENS,
        system=system,
        tools=_cache_tools(tools),
        messages=messages,
        **extra,
    ) as stream:
        for event in stream:
            if event.type == "text":
                yield {"type": "text", "text": event.text}
            elif event.type == "thinking":
                yield {"type": "thinking", "text": event.thinking}
        resp = stream.get_final_message()
    u = getattr(resp, "usage", None)
    usage = {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    yield {
        "type": "_final",
        "content": _dump(resp.content),
        "stop_reason": resp.stop_reason,
        "usage": usage,
    }


def _openai_turn(
    model_id: str,
    tools: list[dict[str, Any]],
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    continuation: bool = False,
):
    """Stream one OpenAI chat-completions call. Same event shape as
    `_anthropic_turn`, with the assistant content normalized into Anthropic's
    content-block shape and the finish_reason normalized via _OPENAI_STOP_MAP.

    `continuation` means this call resumes a reply the token cap cut off; its text
    is kept raw (not stripped) so the leading boundary whitespace survives the
    merge into the partial and words aren't glued together."""
    openai_tools = _to_openai_tools(tools)
    stream = _openai().chat.completions.create(
        model=model_id,
        max_completion_tokens=max_tokens or Config.MAX_TOKENS,
        messages=_to_openai_messages(system, messages),
        tools=openai_tools or openai.NOT_GIVEN,
        stream=True,
        stream_options={"include_usage": True},
    )

    text_parts: list[str] = []
    calls: dict[int, dict[str, str | None]] = {}
    finish_reason: str | None = None
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    for chunk in stream:
        # The usage chunk (emitted last when include_usage is set) has no choices.
        if getattr(chunk, "usage", None):
            usage["input_tokens"] = getattr(chunk.usage, "prompt_tokens", 0) or 0
            usage["output_tokens"] = getattr(chunk.usage, "completion_tokens", 0) or 0
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if delta and delta.content:
            text_parts.append(delta.content)
            yield {"type": "text", "text": delta.content}
        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                slot = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = (slot["name"] or "") + tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] = (slot["arguments"] or "") + tc.function.arguments
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    content: list[dict[str, Any]] = []
    text = "".join(text_parts)
    if not continuation:
        text = text.strip()
    if text:
        content.append({"type": "text", "text": text})
    for idx in sorted(calls):
        call = calls[idx]
        try:
            tool_input = json.loads(call["arguments"] or "{}")
        except ValueError:
            tool_input = {}
        content.append(
            {
                "type": "tool_use",
                "id": call["id"] or f"call_{idx}",
                "name": call["name"] or "",
                "input": tool_input,
            }
        )

    yield {
        "type": "_final",
        "content": content,
        "stop_reason": _OPENAI_STOP_MAP.get(finish_reason, "end_turn"),
        "usage": usage,
    }


def stream_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
    effort: str | None = None,
):
    """Generator version of the agent loop — yields incremental events instead
    of blocking until the whole turn (every tool round-trip included) is done.

    `run_turn()` below drains this exact generator and returns one final dict,
    so it's a strict behavioral superset: nothing that already calls
    `run_turn()` (voice mode, `meeting_copilot.generate_ai_notes`) needs to
    change. Only the streaming /api/chat/stream endpoint consumes the
    incremental events directly, so the typed chat UI can render text as it's
    generated instead of waiting for the full response.

    Yields:
      {"type": "text", "text": "<delta>"}                      — assistant text, as generated
      {"type": "tool_call", "tool": "<name>", "input": {...}}   — about to call a tool
      {"type": "done", "result": {...}}                         — final result, same shape run_turn() returns

    Same parameters and business-scoping rules as run_turn() (see below).
    """
    model_id = models.active_model()
    provider = models.provider_of(model_id)
    tools = build_tools(active_business, allowed_apps=allowed_apps)

    if isinstance(user_content, str):
        user_content = [{"type": "text", "text": user_content}]

    # Resolve reasoning effort once for the whole turn: an explicit low/medium/high
    # from the frontend wins; "auto" (or nothing) is chosen from the turn's signals.
    # Attachments count whether they're image/document blocks OR text-file uploads
    # inlined as text blocks (marked by ATTACHMENT_TEXT_PREFIX) — the latter would
    # otherwise be indistinguishable from the user's own typed text.
    has_attachments = any(_is_attachment_block(b) for b in user_content)
    effort = resolve_effort(effort, user_content, has_attachments)

    messages = store.get(session_id)
    messages.append({"role": "user", "content": user_content})

    # Fetched once per turn (not per tool round-trip, not cached across turns —
    # next-turn recall of what was just extracted is the point of the feature).
    try:
        memory_block = build_memory_block(
            active_business=active_business, latest_user_text=_text_of(user_content),
        )
    except Exception:  # noqa: BLE001 — a memory-retrieval failure must never break the turn
        memory_block = ""
    # B2: always-present per-business context pack (fail-open to "").
    try:
        from .context_packs import packs_for
        context_pack = packs_for(active_business)
    except Exception:  # noqa: BLE001 — a pack failure must never break the turn
        context_pack = ""
    # Always-on RAG: relevant snippets from this business's knowledge base
    # (Context-tab files via pgvector + the autopilot corpus via Pinecone),
    # federated in one embedding. Empty on small-talk / "All Businesses" / when
    # the feature flag is off — see hermes/rag.py. Fail-open to "".
    try:
        from .rag import build_context_block
        retrieved_context = build_context_block(
            active_business=active_business, latest_user_text=_text_of(user_content),
        )
    except Exception:  # noqa: BLE001 — a retrieval failure must never break the turn
        retrieved_context = ""
    # Phase 2: recall of PAST conversations (verbatim turns + summaries), owner-
    # scoped, provenance-first. Empty on small-talk / when the flag is off — see
    # hermes/conversation_retrieve.py. Fail-open to "".
    try:
        from .conversation_retrieve import build_history_block
        from .db.conversation_memory import owner_user_id
        conversation_block = build_history_block(
            user_id=owner_user_id(), latest_user_text=_text_of(user_content),
            active_business=active_business,
        )
    except Exception:  # noqa: BLE001 — a recall failure must never break the turn
        conversation_block = ""
    # Files shared earlier in this chat, so the model can read them on any turn
    # (see _attachments_block). Tolerate stores without the method / a failure.
    try:
        attachments = store.attachments(session_id)
    except Exception:  # noqa: BLE001
        attachments = []

    tool_trace: list[dict[str, Any]] = []
    reply = ""
    thinking_summary = ""
    stop_reason = None
    # Token usage summed across every provider call this turn (one per tool
    # round-trip, plus any continuation calls). Cost is computed once at the end.
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    # Per-turn output budget that auto-grows (monotonically, across the whole turn)
    # when a reply is cut off by the token cap.
    turn_max_tokens = Config.MAX_TOKENS
    for _ in range(Config.MAX_TOOL_ITERATIONS):
        # OpenAI needs one system string; Anthropic gets structured blocks so the
        # stable prefix (tools + static guidelines) can carry a cache breakpoint.
        # Build both once per outer iteration and reuse across any in-place
        # continuation calls below.
        system_prompt = _system_prompt(
            voice, active_business, memory_block, attachments, context_pack,
            retrieved_context, conversation_block,
        )
        system_blocks = _system_blocks(
            voice, active_business, memory_block, attachments, context_pack,
            retrieved_context, conversation_block,
        )

        # Produce ONE complete assistant turn, resuming in place if the token cap
        # cuts the reply off. These continuation calls stay inside this inner loop
        # and never consume the outer tool-iteration budget — otherwise a reply
        # truncated on the final allowed pass would be dropped with no output.
        continuing = False
        while True:
            turn = (
                _anthropic_turn(
                    model_id, tools, system_blocks, messages, effort, turn_max_tokens, continuing
                )
                if provider == "anthropic"
                else _openai_turn(model_id, tools, system_prompt, messages, turn_max_tokens, continuing)
            )

            assistant_content: list[dict[str, Any]] = []
            for event in turn:
                if event["type"] == "_final":
                    assistant_content = event["content"]
                    stop_reason = event["stop_reason"]
                    # Accumulate here, inside the continuation loop, so a reply
                    # resumed after a token-cap cutoff bills every provider call
                    # it took — not just the last one.
                    for k, v in (event.get("usage") or {}).items():
                        usage_totals[k] = usage_totals.get(k, 0) + (v or 0)
                else:
                    yield event

            if continuing:
                # Resuming a cut-off reply: fold the continuation into the truncated
                # assistant message rather than appending a second assistant turn.
                _merge_continuation(messages[-1]["content"], assistant_content)
            else:
                messages.append({"role": "assistant", "content": assistant_content})

            if t := _thinking_of(assistant_content):
                thinking_summary = t

            # Reply cut off purely by the token cap (no tool call to make): grow the
            # budget and resume the same assistant message instead of returning a
            # truncated answer. Strip the partial's thinking blocks first so the
            # prefilled continuation call is valid under extended thinking. Bounded
            # because turn_max_tokens strictly doubles toward the ceiling.
            if (
                stop_reason == "max_tokens"
                and turn_max_tokens < Config.MAX_TOKENS_CEILING
                and not any(b.get("type") == "tool_use" for b in assistant_content)
            ):
                messages[-1]["content"] = _strip_thinking(messages[-1]["content"])
                turn_max_tokens = min(Config.MAX_TOKENS_CEILING, turn_max_tokens * 2)
                continuing = True
                continue
            break

        if stop_reason == "tool_use":
            tool_results = []
            extra_blocks: list[dict[str, Any]] = []
            for block in assistant_content:
                if block.get("type") != "tool_use" or block.get("name") not in LOCAL_TOOLS:
                    continue
                name = block["name"]
                tool_input = block.get("input", {}) or {}
                label = tool_status(name, tool_input)
                yield {"type": "tool_call", "tool": name, "input": tool_input, "label": label}
                content_str, is_error, media, blocks = dispatch(name, tool_input, active_business)
                trace_entry = {
                    "tool": name,
                    "input": tool_input,
                    "ok": not is_error,
                    "label": label,
                }
                if media:
                    trace_entry["media"] = media
                # Capture files this run produced (create_file) so the UI can offer
                # them as downloads later. We store the s3_uri (stable) rather than
                # the presigned download_url (which expires); callers re-presign it.
                if not is_error and name == "create_file":
                    try:
                        _fr = json.loads(content_str)
                        if isinstance(_fr, dict) and _fr.get("s3_uri"):
                            trace_entry["file"] = {
                                "filename": _fr.get("filename"),
                                "s3_uri": _fr.get("s3_uri"),
                                "content_type": _fr.get("content_type"),
                                "size_bytes": _fr.get("size_bytes"),
                            }
                    except (ValueError, TypeError):
                        pass
                tool_trace.append(trace_entry)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": content_str,
                        "is_error": is_error,
                    }
                )
                if blocks:
                    extra_blocks.extend(blocks)
            if tool_results:
                # Real file content (e.g. drive_read_file) rides alongside the tool
                # results in the same user turn, so Claude can read it natively on
                # the very next turn instead of only seeing a text tool result.
                messages.append({"role": "user", "content": [*tool_results, *extra_blocks]})
                continue
            # tool_use stop but only server-side tools — let the model finish.
            break

        if stop_reason == "pause_turn":
            # Server tool (web search/fetch) hit its iteration cap; resume.
            continue

        if stop_reason == "refusal":
            reply = (
                "I'm sorry, but I can't help with that request."
                if not _text_of(assistant_content)
                else _text_of(assistant_content)
            )
            break

        # end_turn / max_tokens / stop_sequence. Read the stored assistant message
        # (not just this chunk) so a reply resumed after a token-cap cutoff — or
        # one that stayed truncated at the ceiling — returns its full text.
        reply = _text_of(messages[-1]["content"])
        break

    # Loop exhausted its tool-iteration budget without producing a final reply
    # (e.g. every pass made a tool call): fall back to the last assistant text
    # already streamed rather than emitting "(no response)".
    if not reply:
        for message in reversed(messages):
            if message.get("role") == "assistant":
                reply = _text_of(message["content"])
                break

    store.set(session_id, messages)

    cost_usd = models.price_of(
        model_id,
        usage_totals["input_tokens"],
        usage_totals["output_tokens"],
        usage_totals["cache_read_input_tokens"],
    )

    yield {
        "type": "done",
        "result": {
            "reply": reply or "(no response)",
            "thinking": thinking_summary or None,
            "tool_calls": tool_trace,
            "stop_reason": stop_reason,
            "usage": usage_totals,
            "cost_usd": cost_usd,
            "model_used": model_id,
        },
    }


def run_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Run one user turn through the agent loop and return a structured result.

    `voice=True` adds spoken-call style guidance so replies are short and natural
    when read aloud; the agent's tools and abilities are otherwise identical.

    `active_business` scopes which Maton key(s) tool calls use this turn: a real
    business slug locks every call to that business's key; "all" lets Claude pick
    per call via the tools' extra `business` argument; None preserves legacy
    behavior (the plain default key, no business awareness).

    Blocking/one-shot — internally just drains stream_turn() and returns its
    final event. Use stream_turn() directly for incremental output.
    """
    result: dict[str, Any] = {}
    for event in stream_turn(session_id, user_content, voice, active_business, allowed_apps, effort):
        if event["type"] == "done":
            result = event["result"]
    return result
