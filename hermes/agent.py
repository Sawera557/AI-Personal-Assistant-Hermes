"""The Hermes agent: a Claude tool-use loop over the Maton tools."""
from __future__ import annotations

import datetime
from typing import Any

import anthropic

from .config import Config
from .memory import store
from .tools import LOCAL_TOOLS, build_tools, dispatch, sanitize_tool_input

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


def _system_prompt(voice: bool = False) -> str:
    today = datetime.date.today().isoformat()
    base = (
        "You are Hermes, Gandi Abdi Kediye's Personal AI worker inside the GANDI "
        "Command Center. You help get real work done across connected business apps, "
        "the web, documents, and voice workflows, but Codex is the conductor and final verifier.\n\n"
        "Architecture direction:\n"
        "- The future operating surface is online-first through the Command Center web app, AWS/Vercel runtime, Maton connectors, "
        "and approved cloud APIs. Local systems remain the current source-of-truth and private processing layer during transition.\n\n"
        "Source of truth and evidence rules:\n"
        "- Local C:/Users/gandi/GANDI-COMMAND-CENTER is the current source of truth during transition; Google Drive/GANDI_HUB is a mirror "
        "unless current connector evidence proves otherwise.\n"
        "- Memory is a lead, not proof. Verify mutable facts against the newest source system before relying on them. If a fact cannot be verified, say UNVERIFIED.\n"
        "- Use entity codes consistently: GAK, GIF, AFK, GAKC, NH, CS, PER, CROSS.\n\n"
        "Privacy and routing rules:\n"
        "- CS PHI, immigration records, and future privileged work product stay local-only; do not route them through cloud tools or external connectors.\n"
        "- Rent rolls, leases, vendor contracts, tax/business operations, family and life admin may use connected cloud tools when needed, with evidence citations.\n\n"
        "Connected apps are reachable through the Maton gateway tools. Known app slugs:\n"
        f"{Config.app_catalog()}\n\n"
        "Approval firewall:\n"
        "- Start with read-only discovery. Always discover record IDs with GET calls before planning a write.\n"
        "- Email/WhatsApp/SMS sends, real-recipient drafts, QuickBooks writes/sends, bank/ACH, billing/security/IAM/API-key/secret changes, Drive share/move/delete, "
        "and destructive file actions require explicit Gandi GO at action time.\n"
        "- For any write or destructive action, summarize the exact action and stop for approval. The server blocks write methods by default, so treat writes as approval-ready plans, not executable actions.\n"
        "- Never expose raw tokens, passwords, API keys, webhook secrets, session URLs, or bank credentials.\n\n"
        "Tool guidelines:\n"
        "- Use `maton_request` for read-only native API calls through connected apps.\n"
        "- If an app may not be connected, call `maton_list_connections`. If it is not connected, call `maton_create_connection` and return the OAuth link only through an owner-authenticated channel.\n"
        "- When you use web search, cite the sources.\n"
        "- Lead with the answer; be concise, practical, and evidence-backed.\n"
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


def _drop_thinking(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep private reasoning blocks out of persisted chat and API responses."""
    return [b for b in content if b.get("type") != "thinking"]


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
        assistant_content = _drop_thinking(_dump(resp.content))
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
                        "input": sanitize_tool_input(tool_input),
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
        "thinking": None,
        "tool_calls": tool_trace,
        "stop_reason": stop_reason,
    }
