"""The Hermes agent: a Claude tool-use loop over the Maton tools."""
from __future__ import annotations

import datetime
from typing import Any

import anthropic

from .config import Config
from .memory import store
from .tools import LOCAL_TOOLS, build_tools, dispatch

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _client


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


def _system_prompt(voice: bool = False, active_business: str | None = None) -> str:
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
        "- For any write or destructive action (sending email, sending a WhatsApp/WATI "
        "message or file, assigning a WATI operator, creating/editing calendar events, "
        "modifying QuickBooks data, deleting Drive files, etc.), first clearly summarize "
        "exactly what you will do, then STOP and ask the user to confirm. Only perform "
        "the write after they explicitly approve.\n"
        "- Never substitute one business's data or connected account for another's. If a "
        "business isn't connected yet, tell the user plainly instead of guessing or using "
        "a different business's account.\n"
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


def stream_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
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
    client = _anthropic()
    tools = build_tools(active_business)

    if isinstance(user_content, str):
        user_content = [{"type": "text", "text": user_content}]

    messages = store.get(session_id)
    messages.append({"role": "user", "content": user_content})

    tool_trace: list[dict[str, Any]] = []
    reply = ""
    thinking_summary = ""
    stop_reason = None

    for _ in range(Config.MAX_TOOL_ITERATIONS):
        with client.messages.stream(
            model=Config.MODEL,
            max_tokens=Config.MAX_TOKENS,
            system=_system_prompt(voice, active_business),
            tools=tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": Config.EFFORT},
            messages=messages,
        ) as stream:
            for delta in stream.text_stream:
                yield {"type": "text", "text": delta}
            resp = stream.get_final_message()

        stop_reason = resp.stop_reason
        assistant_content = _dump(resp.content)
        messages.append({"role": "assistant", "content": assistant_content})

        if t := _thinking_of(assistant_content):
            thinking_summary = t

        if stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.get("type") != "tool_use" or block.get("name") not in LOCAL_TOOLS:
                    continue
                name = block["name"]
                tool_input = block.get("input", {}) or {}
                yield {"type": "tool_call", "tool": name, "input": tool_input}
                content_str, is_error, media = dispatch(name, tool_input, active_business)
                trace_entry = {
                    "tool": name,
                    "input": tool_input,
                    "ok": not is_error,
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
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
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
    for event in stream_turn(session_id, user_content, voice, active_business):
        if event["type"] == "done":
            result = event["result"]
    return result
