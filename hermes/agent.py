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


def _system_prompt(voice: bool = False, active_business: str | None = None, memory_block: str = "") -> str:
    today = datetime.date.today().isoformat()
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
        "automatically CCs the user's own email too. Use `send_email` for any other email.\n"
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
        "- Lead with the answer; be concise and practical.\n"
        f"Today's date is {today}."
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
            "All Maton tool calls automatically use that business's connected account."
        )
    if memory_block:
        base += f"\n\nMEMORY (what you already know — preferences, business rules, standing decisions):\n{memory_block}"
    return base + _VOICE_STYLE if voice else base


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


def _anthropic_turn(
    model_id: str,
    tools: list[dict[str, Any]],
    system: str,
    messages: list[dict[str, Any]],
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
    extra: dict[str, Any] = {}
    if "haiku" not in model_id.lower():
        extra["thinking"] = {"type": "adaptive", "display": "summarized"}
        extra["output_config"] = {"effort": Config.EFFORT}

    with _anthropic().messages.stream(
        model=model_id,
        max_tokens=Config.MAX_TOKENS,
        system=system,
        tools=tools,
        messages=messages,
        **extra,
    ) as stream:
        for event in stream:
            if event.type == "text":
                yield {"type": "text", "text": event.text}
            elif event.type == "thinking":
                yield {"type": "thinking", "text": event.thinking}
        resp = stream.get_final_message()
    yield {"type": "_final", "content": _dump(resp.content), "stop_reason": resp.stop_reason}


def _openai_turn(
    model_id: str,
    tools: list[dict[str, Any]],
    system: str,
    messages: list[dict[str, Any]],
):
    """Stream one OpenAI chat-completions call. Same event shape as
    `_anthropic_turn`, with the assistant content normalized into Anthropic's
    content-block shape and the finish_reason normalized via _OPENAI_STOP_MAP."""
    openai_tools = _to_openai_tools(tools)
    stream = _openai().chat.completions.create(
        model=model_id,
        max_completion_tokens=Config.MAX_TOKENS,
        messages=_to_openai_messages(system, messages),
        tools=openai_tools or openai.NOT_GIVEN,
        stream=True,
    )

    text_parts: list[str] = []
    calls: dict[int, dict[str, str | None]] = {}
    finish_reason: str | None = None

    for chunk in stream:
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
    text = "".join(text_parts).strip()
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
    }


def stream_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
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

    tool_trace: list[dict[str, Any]] = []
    reply = ""
    thinking_summary = ""
    stop_reason = None

    for _ in range(Config.MAX_TOOL_ITERATIONS):
        system_prompt = _system_prompt(voice, active_business, memory_block)
        turn = (
            _anthropic_turn(model_id, tools, system_prompt, messages)
            if provider == "anthropic"
            else _openai_turn(model_id, tools, system_prompt, messages)
        )

        assistant_content: list[dict[str, Any]] = []
        for event in turn:
            if event["type"] == "_final":
                assistant_content = event["content"]
                stop_reason = event["stop_reason"]
            else:
                yield event

        messages.append({"role": "assistant", "content": assistant_content})

        if t := _thinking_of(assistant_content):
            thinking_summary = t

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

        # end_turn / max_tokens / stop_sequence
        reply = _text_of(assistant_content)
        break

    store.set(session_id, messages)

    yield {
        "type": "done",
        "result": {
            "reply": reply or "(no response)",
            "thinking": thinking_summary or None,
            "tool_calls": tool_trace,
            "stop_reason": stop_reason,
        },
    }


def run_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
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
    for event in stream_turn(session_id, user_content, voice, active_business, allowed_apps):
        if event["type"] == "done":
            result = event["result"]
    return result
