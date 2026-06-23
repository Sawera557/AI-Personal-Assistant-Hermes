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
    "need to confirm a write action, ask in one short spoken question."
)


def _system_prompt(voice: bool = False) -> str:
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
        "- For any write or destructive action (sending email, creating/editing calendar "
        "events, modifying QuickBooks data, deleting Drive files, etc.), first clearly "
        "summarize exactly what you will do, then STOP and ask the user to confirm. Only "
        "perform the write after they explicitly approve.\n"
        "- When you use web search, cite the sources.\n"
        "- Lead with the answer; be concise and practical.\n"
        f"Today's date is {today}."
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


def run_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
) -> dict[str, Any]:
    """Run one user turn through the agent loop and return a structured result.

    `voice=True` adds spoken-call style guidance so replies are short and natural
    when read aloud; the agent's tools and abilities are otherwise identical.
    """
    client = _anthropic()
    tools = build_tools()

    if isinstance(user_content, str):
        user_content = [{"type": "text", "text": user_content}]

    messages = store.get(session_id)
    messages.append({"role": "user", "content": user_content})

    tool_trace: list[dict[str, Any]] = []
    reply = ""
    thinking_summary = ""
    stop_reason = None

    for _ in range(Config.MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=Config.MODEL,
            max_tokens=Config.MAX_TOKENS,
            system=_system_prompt(voice),
            tools=tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": Config.EFFORT},
            messages=messages,
        )
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
                content_str, is_error = dispatch(name, tool_input)
                tool_trace.append(
                    {
                        "tool": name,
                        "input": tool_input,
                        "ok": not is_error,
                    }
                )
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

    return {
        "reply": reply or "(no response)",
        "thinking": thinking_summary or None,
        "tool_calls": tool_trace,
        "stop_reason": stop_reason,
    }
