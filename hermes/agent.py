"""The Hermes agent: a tool-use loop over the Maton tools, on Claude, OpenAI or NVIDIA.

Which model runs the loop is a global, switchable setting (see hermes.models)
rather than hardcoded — but everything the agent can DO (Maton/WATI tools,
business scoping, streaming, conversation memory) is identical either way.

Conversation history is always stored in Anthropic's content-block shape
(see `_dump`), regardless of which provider generated it. That keeps the rest
of the app (storage, transcript replay, tool dispatch) provider-agnostic, and
means switching models mid-conversation doesn't lose history — `_to_openai_messages`
below just translates that shared format into OpenAI's wire format per call.

There are only two wire protocols here, not three: NVIDIA NIM speaks the
OpenAI chat-completions API, so `_openai_turn` serves both and differs only in
which client it uses and which per-provider knobs it sends (see _PROVIDERS).
"""
from __future__ import annotations

import datetime
import json
from typing import Any

import anthropic
import openai

from . import context_mode, credits, guardrails, health, models
from .attachments import ATTACHMENT_TEXT_PREFIX
from .config import Config
from .memory import store
from .memory_retrieve import build_memory_block
from .tools import LOCAL_TOOLS, build_tools, dispatch, tool_status

_anthropic_client: anthropic.Anthropic | None = None
_openai_client: openai.OpenAI | None = None
#: NIM clients keyed by API key — models can be provisioned under different keys.
_nvidia_clients: dict[str, openai.OpenAI] = {}


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


def _nvidia(model_id: str) -> openai.OpenAI:
    """The OpenAI SDK pointed at NVIDIA NIM — same protocol, different host and
    key. Kept as its own client (not an OPENAI_BASE_URL override) so switching
    the chat model to a NIM model doesn't also redirect embeddings and voice.

    Cached per key, not globally: build.nvidia.com hands out a key per model
    page, so two NIM models may authenticate differently.
    """
    key = Config.nvidia_key_for(model_id)
    if not key:
        raise RuntimeError(
            f"No NVIDIA API key configured for {model_id!r} — set NVIDIA_API_KEY (or "
            "that model's own key) in .env, or switch the model back to a "
            "Claude/OpenAI option in Settings."
        )
    if key not in _nvidia_clients:
        _nvidia_clients[key] = openai.OpenAI(
            api_key=key,
            base_url=Config.NVIDIA_BASE_URL,
            timeout=Config.NVIDIA_TIMEOUT,
            max_retries=1,
        )
    return _nvidia_clients[key]


#: Per-model NIM recipes, taken from each model's own card. NIM models agree on
#: the wire protocol and on almost nothing else, so each entry is data rather
#: than a branch:
#:
#:   `params`          top-level request params (temperature, top_p, penalties).
#:   `extra`           top-level body params the OpenAI SDK doesn't model, sent
#:                     via extra_body — e.g. Kimi's `reasoning_effort`.
#:   `thinking_kwargs` what to put in chat_template_kwargs to switch reasoning
#:                     ON. The key name is NOT standard: Nemotron/Gemma use
#:                     `enable_thinking`, DeepSeek uses `thinking` and wants a
#:                     `reasoning_effort` alongside it. Boolean values here are
#:                     flipped to False on a continuation call (see below);
#:                     non-boolean ones ride along unchanged. A model with no
#:                     entry is never sent the key at all — gpt-oss and Kimi
#:                     reason unconditionally and reject the toggle.
#:   `reasoning_budget` also cap the thinking phase. Only Nemotron documents it.
#:
#: Deliberately absent: `seed`. NVIDIA's generated snippets ship `seed: 0`,
#: which pins the model to one deterministic answer per input — fine for a demo,
#: wrong for a conversation, where asking twice should be able to rephrase.
_NVIDIA_TUNING: dict[str, dict[str, Any]] = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": {
        "params": {"temperature": 1.0, "top_p": 0.95},
        "thinking_kwargs": {"enable_thinking": True},
        "reasoning_budget": True,
    },
    "openai/gpt-oss-20b": {
        "params": {"temperature": 1.0, "top_p": 1.0},
    },
    "google/gemma-4-31b-it": {
        "params": {
            "temperature": 0.5, "top_p": 1.0,
            "frequency_penalty": 0.0, "presence_penalty": 0.0,
        },
        "thinking_kwargs": {"enable_thinking": True},
    },
    "google/diffusiongemma-26b-a4b-it": {
        "params": {"temperature": 1.0, "top_p": 0.95},
        "thinking_kwargs": {"enable_thinking": True},
    },
    "moonshotai/kimi-k3": {
        "params": {"temperature": 1.0},
        "extra": {"reasoning_effort": "max"},
    },
    "deepseek-ai/deepseek-v4-flash-0731": {
        "params": {"temperature": 1.0, "top_p": 0.95},
        "thinking_kwargs": {"thinking": True, "reasoning_effort": "high"},
    },
}


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


def _system_prompt(
    voice: bool = False,
    active_business: str | None = None,
    memory_block: str = "",
    attachments: list[dict[str, Any]] | None = None,
    context_pack: str = "",
    retrieved_context: str = "",
    conversation_block: str = "",
    tools_available: bool = True,
) -> str:
    today = datetime.date.today().isoformat()

    if not tools_available:
        # Plain-chat mode (hermes.context_mode off): no tools are sent, so the
        # several thousand tokens of tool instructions below are not just waste
        # — they are actively wrong. Told to "call send_email" with no such tool
        # available, the model narrates tool calls instead of answering. This
        # prompt describes what Hermes can actually do in this mode, and is
        # ~100 tokens instead of ~1,600.
        plain = (
            "You are Hermes, a capable AI assistant. Answer directly and "
            "concisely from your own knowledge.\n\n"
            "You currently have NO tools and NO access to the user's apps, "
            "documents, saved memory or business data — business context is "
            "switched off for speed and cost. Never claim to have looked "
            "something up, never invent specifics about their business, and "
            "never describe or narrate a tool call. If a request genuinely "
            "needs their data or apps, say plainly that business context is "
            "turned off and they can re-enable it in Settings.\n"
            f"Today's date is {today}."
        )
        return plain + _VOICE_STYLE if voice else plain

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
        "the write after they explicitly approve. A safety guardrail enforces this: the "
        "first time you call such a tool it returns `status:\"awaiting_confirmation\"` with "
        "a `summary` and an `approval_token` INSTEAD of running. When that happens, show the "
        "user the summary and ask them to confirm; once they approve, call the SAME tool "
        "again with the SAME arguments plus `approval_token` set to the returned value. Never "
        "invent an approval_token, and never change the arguments between the two calls — any "
        "change needs a fresh confirmation. (If the guardrail is off, tools run directly; you "
        "should still confirm first as above unless the user has told you not to.)\n"
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
            "All Maton tool calls automatically use that business's connected account. "
            "Stay within this business: do not pull in, enumerate, or report another business's "
            "data, tenants, or accounts unless the user explicitly asks for a cross-business "
            "view. If a memory or retrieved snippet from a DIFFERENT business surfaces, say it "
            f"belongs to that other business — never fold it into '{active_business}'."
        )
    if context_pack:
        base += (
            "\n\nBUSINESS CONTEXT PACK (authoritative standing facts and rules — "
            "trust these over memory or retrieval; if a tool result contradicts "
            "them, say so explicitly):\n" + context_pack
        )
    if memory_block:
        base += (
            "\n\nMEMORY — saved facts from PAST conversations (preferences, business "
            "rules, standing decisions). Preferences and standing decisions are "
            "reliable. But anything here that can change over time — contact numbers, "
            "balances, message contents, current status, who said what — may be STALE: "
            "treat it as background, NOT as a live retrieval, and confirm it with a tool "
            "call before stating it as a current fact.\n" + memory_block
        )
    if conversation_block:
        base += f"\n\n{conversation_block}"
    if retrieved_context:
        base += f"\n\n{retrieved_context}"
    base += _attachments_block(attachments)
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


def _anthropic_turn(
    model_id: str,
    tools: list[dict[str, Any]],
    system: str,
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
    credits.check("anthropic")
    extra: dict[str, Any] = {}
    if "haiku" not in model_id.lower() and not continuation:
        extra["thinking"] = {"type": "adaptive", "display": "summarized"}
        extra["output_config"] = {"effort": normalize_effort(effort)}

    with _anthropic().messages.stream(
        model=model_id,
        max_tokens=max_tokens or Config.MAX_TOKENS,
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
    provider: str = "openai",
):
    """Stream one OpenAI-protocol chat-completions call. Same event shape as
    `_anthropic_turn`, with the assistant content normalized into Anthropic's
    content-block shape and the finish_reason normalized via _OPENAI_STOP_MAP.

    Serves both OpenAI and NVIDIA NIM (`provider="nvidia"`), which speak the
    same wire protocol; only the client and a few per-provider knobs differ.

    `continuation` means this call resumes a reply the token cap cut off; its text
    is kept raw (not stripped) so the leading boundary whitespace survives the
    merge into the partial and words aren't glued together."""
    credits.check(credits.scope_for(provider, model_id))
    openai_tools = _to_openai_tools(tools)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": _to_openai_messages(system, messages),
        "tools": openai_tools or openai.NOT_GIVEN,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # Output length is the RESPONSE's business, not ours: no cap is sent, so the
    # model stops when it has finished answering rather than when it hits a
    # number we picked. Verified against NIM — an uncapped request finishes with
    # finish_reason="stop", it does not fall back to some small server default.
    # Config.OUTPUT_CAP is an opt-in cost guard, unset by default. (Anthropic is
    # the exception: max_tokens is a required field there, so _anthropic_turn
    # still sends one and grows it on truncation.)
    cap = max_tokens or Config.OUTPUT_CAP

    if provider == "nvidia":
        client = _nvidia(model_id)
        tuning = _NVIDIA_TUNING.get(model_id) or {
            "params": {
                "temperature": Config.NVIDIA_TEMPERATURE,
                "top_p": Config.NVIDIA_TOP_P,
            },
        }
        kwargs.update(tuning.get("params") or {})
        if cap:
            kwargs["max_tokens"] = cap  # NIM uses the classic name
        extra: dict[str, Any] = dict(tuning.get("extra") or {})
        if thinking_kwargs := tuning.get("thinking_kwargs"):
            # Reasoning is switched OFF on a continuation call for the same
            # reason it is on Claude: we're resuming a prefilled partial
            # assistant turn, and reasoning restarted mid-sentence just burns
            # tokens before the text we actually want. Only the BOOLEANS flip —
            # a companion value like DeepSeek's reasoning_effort is not a toggle
            # and stays as configured.
            thinking = not continuation
            extra["chat_template_kwargs"] = {
                k: (v and thinking) if isinstance(v, bool) else v
                for k, v in thinking_kwargs.items()
            }
            if tuning.get("reasoning_budget"):
                # NIM bills reasoning against max_tokens. Uncapped (the default)
                # the configured budget stands on its own; but whenever a cap IS
                # in force it must stay strictly below it — a budget larger than
                # the cap lets the model spend the whole allowance reasoning and
                # return that reasoning as the answer, truncated at max_tokens.
                budget = Config.NVIDIA_REASONING_BUDGET
                if cap:
                    budget = min(budget, max(512, cap * 3 // 4))
                extra["reasoning_budget"] = budget if thinking else 0
        if extra:
            kwargs["extra_body"] = extra
    else:
        client = _openai()
        if cap:
            kwargs["max_completion_tokens"] = cap

    stream = client.chat.completions.create(**kwargs)

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
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
        # Reasoning models stream their reasoning on a separate field, ahead of
        # the answer. Surface it as the very same live "thinking" event Claude's
        # extended thinking produces, so the UI renders both without knowing the
        # provider. Two field names in the wild — Nemotron/gpt-oss/Kimi/DeepSeek
        # use `reasoning_content`, DiffusionGemma uses `reasoning` — and reading
        # only one silently drops the other's thinking. getattr: plain OpenAI
        # deltas have neither.
        reasoning = None
        if delta:
            reasoning = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
        if reasoning:
            reasoning_parts.append(reasoning)
            yield {"type": "thinking", "text": reasoning}
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
    # Reasoning is returned OUT OF BAND, not as a content block. Anthropic
    # requires any thinking block replayed in an assistant turn to carry the
    # signature it was issued with; one synthesized here would have none, so
    # storing it in the shared history would 400 the next Claude call the moment
    # someone switches provider mid-conversation — exactly the case the shared
    # history format exists to support.
    reasoning_text = "".join(reasoning_parts).strip()
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

    # A stream that ended without ever reporting a finish_reason did NOT finish
    # cleanly — NIM drops the connection when a reasoning phase exhausts the
    # budget. Calling that "end_turn" strands the caller with an empty reply and
    # a silent $0 turn; reporting "max_tokens" instead lets stream_turn grow the
    # budget and resume, which is the recovery path that already exists.
    stop_reason = (
        "max_tokens" if finish_reason is None else _OPENAI_STOP_MAP.get(finish_reason, "end_turn")
    )

    yield {
        "type": "_final",
        "content": content,
        "thinking": reasoning_text,
        "stop_reason": stop_reason,
        "usage": usage,
    }


def _credits_result(provider: str, model_id: str, tool_trace: list[dict[str, Any]]):
    """The `done` event for a turn that couldn't run because the provider is out
    of credit. Shaped exactly like a normal result — a zero-cost turn that says
    what happened — so every caller (chat, stream, voice, scheduler) renders it
    as an ordinary reply rather than having to learn a new error path."""
    return {
        "type": "done",
        "result": {
            "reply": credits.message(provider),
            "thinking": None,
            "tool_calls": tool_trace,
            "stop_reason": "credits_exhausted",
            "usage": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            "cost_usd": 0.0,
            "model_used": model_id,
        },
    }


def stream_turn(
    session_id: str,
    user_content: str | list[dict[str, Any]],
    voice: bool = False,
    active_business: str | None = None,
    allowed_apps: list[str] | None = None,
    effort: str | None = None,
    interactive: bool = True,
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
    # Plain-chat mode: no tool schemas (~11k tokens of the ~20k a trivial
    # message costs) and no retrieval blocks below. See hermes.context_mode.
    rich_context = context_mode.enabled()
    tools = build_tools(active_business, allowed_apps=allowed_apps) if rich_context else []

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

    # Conversational turn counter for Guardrail #1. Derived from the stored
    # history rather than a process-local counter so it survives a restart and
    # agrees across workers — on a serverless deploy the approval and the
    # request that issued it are usually different processes entirely.
    turn_index = sum(1 for m in messages if m.get("role") == "user")

    # Every block below is fail-open: none of them may break a chat turn. They
    # go through health.guarded() so that staying silent is now *recorded* —
    # see hermes/health.py for why (a month of dead memory extraction that
    # nothing in the system was able to notice).
    latest_text = _text_of(user_content)

    def _ctx(signal, fn, default=""):
        """One retrieval block, skipped entirely in plain-chat mode.

        Skipping is NOT recorded as a health signal: it is a deliberate setting,
        not a degradation, and reporting it as one would train people to ignore
        the health page (see hermes/health.py).
        """
        if not rich_context:
            return default
        return health.guarded(signal, fn, default)

    # Fetched once per turn (not per tool round-trip, not cached across turns —
    # next-turn recall of what was just extracted is the point of the feature).
    memory_block = _ctx(
        "memory_block",
        lambda: build_memory_block(
            active_business=active_business, latest_user_text=latest_text,
        ),
    )

    # B2: always-present per-business context pack.
    def _pack():
        from .context_packs import packs_for
        return packs_for(active_business)

    context_pack = _ctx("context_pack", _pack)

    # Always-on RAG: relevant snippets from this business's knowledge base
    # (Context-tab files via pgvector + the autopilot corpus via Pinecone),
    # federated in one embedding. Empty on small-talk / "All Businesses" / when
    # the feature flag is off — see hermes/rag.py.
    def _rag():
        from .rag import build_context_block
        return build_context_block(
            active_business=active_business, latest_user_text=latest_text,
        )

    retrieved_context = _ctx("rag", _rag)

    # Phase 2: recall of PAST conversations (verbatim turns + summaries), owner-
    # scoped, provenance-first. Empty on small-talk / when the flag is off — see
    # hermes/conversation_retrieve.py.
    def _recall():
        from .conversation_retrieve import build_history_block
        from .db.conversation_memory import owner_user_id
        return build_history_block(
            user_id=owner_user_id(), latest_user_text=latest_text,
            active_business=active_business,
        )

    conversation_block = _ctx("conversation_recall", _recall)

    # Files shared earlier in this chat, so the model can read them on any turn
    # (see _attachments_block). Tolerate stores without the method / a failure.
    attachments = _ctx("attachments", lambda: store.attachments(session_id), default=[])

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
        system_prompt = _system_prompt(
            voice, active_business, memory_block, attachments, context_pack,
            retrieved_context, conversation_block, tools_available=rich_context,
        )

        # Produce ONE complete assistant turn, resuming in place if the token cap
        # cuts the reply off. These continuation calls stay inside this inner loop
        # and never consume the outer tool-iteration budget — otherwise a reply
        # truncated on the final allowed pass would be dropped with no output.
        continuing = False
        while True:
            # Two wire protocols, three providers: OpenAI and NVIDIA NIM share
            # _openai_turn and differ only by `provider`.
            #
            # Only Anthropic is handed turn_max_tokens. Its API requires
            # max_tokens, so the reply really can be cut off by our number and
            # the growth loop below matters. The OpenAI-protocol path sends no
            # cap (None) and lets the response decide its own length.
            turn = (
                _anthropic_turn(
                    model_id, tools, system_prompt, messages, effort, turn_max_tokens, continuing
                )
                if provider == "anthropic"
                else _openai_turn(
                    model_id, tools, system_prompt, messages, None, continuing, provider
                )
            )

            assistant_content: list[dict[str, Any]] = []
            turn_thinking = ""
            try:
                for event in turn:
                    if event["type"] == "_final":
                        assistant_content = event["content"]
                        stop_reason = event["stop_reason"]
                        turn_thinking = event.get("thinking") or ""
                        # Accumulate here, inside the continuation loop, so a reply
                        # resumed after a token-cap cutoff bills every provider call
                        # it took — not just the last one.
                        for k, v in (event.get("usage") or {}).items():
                            usage_totals[k] = usage_totals.get(k, 0) + (v or 0)
                    else:
                        yield event
            except credits.CreditsExhausted as exc:
                # Already known to be out of credit — we never even called out.
                yield _credits_result(exc.provider, model_id, tool_trace)
                return
            except Exception as exc:  # noqa: BLE001 — only credit failures are handled here
                if not credits.note_failure(credits.scope_for(provider, model_id), exc):
                    raise
                # An out-of-credit provider is a billing fact, not a bug: answer
                # the user in plain language instead of raising a 500 at them.
                yield _credits_result(provider, model_id, tool_trace)
                return

            if continuing:
                # Resuming a cut-off reply: fold the continuation into the truncated
                # assistant message rather than appending a second assistant turn.
                _merge_continuation(messages[-1]["content"], assistant_content)
            else:
                messages.append({"role": "assistant", "content": assistant_content})

            # Claude carries its reasoning in a signed content block; the
            # OpenAI-protocol path hands it back out of band instead (see
            # _openai_turn) so nothing unsigned ever enters the history.
            if t := (turn_thinking or _thinking_of(assistant_content)):
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

                # Guardrail #1: structurally hold any external send/write until
                # the user has approved this exact action. On the first call the
                # gate returns a summary + approval_token instead of executing;
                # the model relays it, the user approves, and the model re-calls
                # with the token (see hermes.guardrails). Unattended runs
                # (interactive=False) defer to the scheduler's autonomy tiers.
                #
                # `turn` is what makes that structural rather than advisory: a
                # token minted this turn is not honoured until the user has
                # spoken again, so the model cannot satisfy its own confirmation
                # by echoing back a string it was just handed.
                decision = guardrails.check(
                    session_id, name, tool_input, active_business,
                    interactive=interactive, turn=turn_index,
                )
                if not decision.allow:
                    if decision.reason == "awaiting":
                        # The token is valid but was issued this turn. Retrying
                        # cannot succeed, so say so plainly — otherwise the model
                        # burns the whole tool-iteration budget re-trying.
                        next_step = (
                            "STOP. You already asked for confirmation in this turn and the "
                            "user has not answered yet. Do NOT call this tool again now. "
                            "Show the summary, end your reply, and wait for the user to "
                            "respond. Their approval can only take effect on a later turn."
                        )
                    else:
                        next_step = (
                            "Show the user this summary and ask them to confirm. Then END "
                            "your reply and wait — do not call this tool again in this turn. "
                            f"After the user has approved, call {name} again with the SAME "
                            f"arguments plus approval_token=\"{decision.approval_token}\". "
                            "Do NOT change the arguments — any change requires a new "
                            "confirmation."
                        )
                    guard_payload = json.dumps({
                        "status": "awaiting_confirmation",
                        "summary": decision.summary,
                        "approval_token": decision.approval_token,
                        "next_step": next_step,
                    })
                    tool_trace.append({
                        "tool": name,
                        "input": tool_input,
                        "ok": True,
                        "label": label,
                        "awaiting_confirmation": True,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": guard_payload,
                        "is_error": False,
                    })
                    continue

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
    interactive: bool = True,
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
    for event in stream_turn(
        session_id, user_content, voice, active_business, allowed_apps, effort, interactive
    ):
        if event["type"] == "done":
            result = event["result"]
    return result
