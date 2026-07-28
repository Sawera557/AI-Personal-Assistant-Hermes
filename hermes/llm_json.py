"""Shared helper for backend calls that expect a single JSON object back from
Claude — structured extraction/classification, not the tool-use agent loop."""
from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from .config import Config

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _client


def call_claude_json(
    *, system: str, user_message: str, max_tokens: int, fallback_key: str, model: str | None = None,
) -> dict:
    """Call Claude expecting a JSON object back. Tolerates a stray ```json
    fence around the object (models do this despite instructions not to).
    On any parse failure, degrades to {fallback_key: <raw text>} rather than
    raising — a malformed result is still better than a crashed caller.

    The `system` prompt here is a largely-static per-caller instruction (meeting
    brief/summary, memory extraction), so when prompt caching is on we send it as
    a cached text block: repeated calls of the same kind reuse the prefix at ~0.1x
    instead of paying full input price each time. The varying part (user_message)
    stays in the message body, after the cached prefix."""
    system_param: Any = system
    if Config.PROMPT_CACHE and system:
        system_param = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    resp = _anthropic().messages.create(
        model=model or Config.MODEL,
        max_tokens=max_tokens,
        system=system_param,
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
