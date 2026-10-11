"""What the previous assistant reply left behind, read in the same query as the
history (TURN_COST_PLAN step 0.6).

Both conversation stores answer `get_with_state(key) -> (messages, TurnState)`.
`messages` is exactly what `get(key)` returns today; `TurnState` is the small
summary of the conversation's tail that the Phase 1 router needs to decide
whether a turn may run on the cheap CHAT tier:

  * was the read itself healthy (`ok`)? A failed read must never look like a
    brand-new chat. Postgres `get()` returns `[]` on a DB error (B23), which is
    indistinguishable from "no rows" — `ok=False` is the explicit flag the
    router's rule 1 ("the state read failed → FULL") needs;
  * what did the last assistant row say, call, stop on, and how was it routed;
  * the trailing run of CHAT-routed exchanges (the only history CHAT may see —
    hard invariant 3: no FULL reply ever sits under the LITE_CHAT prompt);
  * how many user turns the whole conversation has (B5 will use this for
    `turn_index`; today it is read but not consumed).

Everything here is pure and shared by BOTH backends — the Postgres store maps
its SELECT rows, the in-memory store maps its transcript entries, and both go
through `state_from_rows` / `pending_from`. That is what keeps the two from
drifting: a pending-workflow rule added here is live on both, and
tests/test_turn_state.py asserts they return identical states for the same
transcript.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .config import int_env

# The pending-workflow kinds `pending_from` can report. Values double as the
# router's reason codes (`pending:guardrail`, …), so they are short, stable
# category names — never anything derived from the message itself.
PENDING_GUARDRAIL = "guardrail"  # a Guardrail #1 confirmation is waiting on the user
PENDING_DRAFT = "draft"          # email_with_attachment held a Gmail draft, not yet sent
PENDING_OAUTH = "oauth"          # maton_create_connection handed out an OAuth link

# Route tiers whose exchanges may be replayed under the LITE_CHAT prompt. Only
# CHAT for now (step 1.2: "only the trailing run of CHAT-routed exchanges").
# LIGHT (Phase 3) decides separately whether its replies join this run.
_CHAT_TIERS: frozenset[str] = frozenset({"chat"})

#: The per-turn telemetry fields of a `done` result (TURN_COST_PLAN step 0.5;
#: produced in hermes/agent.py) that BOTH stores persist with the assistant
#: row — Postgres in message metadata and ai_runs metadata, the in-memory store
#: in its transcript. Named once, here, because this is the one pure module
#: both backends import: a field added to the result but to only one store
#: would make the backends disagree about what a replayed turn cost. Not part
#: of TurnState — nothing routes on them; they are read back only for display
#: and for the offline cost reports.
TELEMETRY_FIELDS: tuple[str, ...] = ("calls", "breakdown", "reasoning_tokens", "cached_tokens")


def history_limit() -> int:
    """How many trailing user/assistant rows a turn may replay
    (HERMES_HISTORY_MESSAGES, default 40, never below 2).

    Shared by both stores so the window the state is derived from is the same
    one on Postgres and in memory — otherwise a long run of CHAT exchanges
    would yield a longer `trailing_chat_exchanges` on one backend than the
    other."""
    try:
        return max(2, int_env("HERMES_HISTORY_MESSAGES", 40))
    except ValueError:
        return 40


@dataclass(frozen=True)
class TurnState:
    """The previous reply's persisted state, as seen by the next turn.

    Defaults describe a brand-new conversation whose read succeeded: nothing
    said, nothing called, nothing pending. A failed read is `TurnState(ok=False)`
    — every consumer must treat that as "unknown" and fail to FULL, never as
    "new chat" (TURN_COST_PLAN hard invariant 9)."""

    ok: bool = True
    # Text of the last assistant row in the replay window; None when there is
    # no assistant row yet.
    last_assistant_text: str | None = None
    # That row's persisted tool trace (`metadata.tool_calls` on Postgres, the
    # transcript entry's `tool_calls` in memory), in dispatch order.
    last_tool_calls: tuple[dict[str, Any], ...] = ()
    # That row's stop_reason ("end_turn", "max_tokens", "credits_exhausted", …).
    last_stop_reason: str | None = None
    # That row's route record as persisted — the step 1.2 dict
    # {mode, tier, final, escalated, reason_codes}, or None for every row
    # written before routing exists (rule 5 treats "no route" as FULL).
    last_route: Any = None
    # The trailing run of CHAT-routed exchanges as (user_text, assistant_text)
    # pairs, oldest first. Empty on a new chat and after any non-CHAT reply.
    # Uncapped here; the CHAT turn applies its own 2 × 600-char budget.
    trailing_chat_exchanges: tuple[tuple[str, str], ...] = ()
    # Count of user rows in the WHOLE conversation (not just the window).
    user_turns: int = 0
    # Actions that row held for the user's confirmation (B6): each
    # {"tool", "input", "approval_token", "summary", "carried"}, as persisted
    # in `metadata.pending`.
    # Read back only to hand an explicit "yes" its token (hermes/agent.py);
    # never shown to the user.
    last_pending: tuple[dict[str, Any], ...] = ()

    @property
    def pending(self) -> str | None:
        """The open workflow the last assistant row left behind, if any —
        see `pending_from`."""
        return pending_from(self.last_tool_calls)


FAILED = TurnState(ok=False)


def route_tier(route: Any) -> str | None:
    """The tier a persisted route record says the reply was FINALLY produced
    on, lowercased: "chat", "light", "full" — or None for an unrouted row.

    Accepts the step 1.2 dict (an escalated CHAT turn has `final="full"`, so
    `final` wins over the tier the router first picked) or a bare tier string.
    Anything else reads as unrouted, which every rule treats as FULL."""
    if isinstance(route, dict):
        tier = route.get("final") or route.get("tier")
    else:
        tier = route
    return tier.strip().lower() if isinstance(tier, str) and tier.strip() else None


def _tool_calls_of(value: Any) -> list[dict[str, Any]]:
    """Normalise a persisted tool trace to a list of dicts.

    Accepts the list itself, a whole metadata dict carrying `tool_calls`, or a
    JSON string of either (in case a driver hands jsonb back undecoded).
    Anything unreadable is an empty trace — never an exception."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("tool_calls")
    if not isinstance(value, (list, tuple)):
        return []
    return [tc for tc in value if isinstance(tc, dict)]


def pending_from(meta_or_tool_calls: Any) -> str | None:
    """The open workflow recorded on ONE assistant row, or None.

    Accepts that row's metadata dict or just its `tool_calls` list. The kinds
    (router rule 3, "hard pending → FULL"):

      * PENDING_GUARDRAIL — a tool call was held by Guardrail #1 and the trace
        carries `awaiting_confirmation: true` (hermes/agent.py stream_turn);
      * PENDING_DRAFT — `email_with_attachment` ran WITHOUT `confirm`, i.e. the
        prepare stage that saves a Gmail draft and waits for a "send it"
        (hermes/email_attachments.py);
      * PENDING_OAUTH — `maton_create_connection` handed the user an OAuth link
        to finish in the browser.

    Deliberately conservative: a draft or OAuth call that FAILED still counts,
    because a wrong "pending" only costs a FULL turn while a missed one could
    put an approval under a prompt with no tools. First match wins, in the
    order above, so the reported kind is stable."""
    calls = _tool_calls_of(meta_or_tool_calls)
    if any(tc.get("awaiting_confirmation") is True for tc in calls):
        return PENDING_GUARDRAIL
    for tc in calls:
        if tc.get("tool") == "email_with_attachment":
            tool_input = tc.get("input") if isinstance(tc.get("input"), dict) else {}
            if not tool_input.get("confirm"):
                return PENDING_DRAFT
    if any(tc.get("tool") == "maton_create_connection" for tc in calls):
        return PENDING_OAUTH
    return None


def pending_of(value: Any) -> tuple[dict[str, Any], ...]:
    """Normalise a persisted `pending` list (B6) to well-formed records.

    Accepts the list itself or a JSON string of it. A record must name a tool
    and carry a token; anything else is dropped — an unreadable record must
    read as "nothing held", never as an approval."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in value:
        if (
            isinstance(item, dict)
            and isinstance(item.get("tool"), str)
            and isinstance(item.get("approval_token"), str)
            and item.get("approval_token")
        ):
            out.append({
                "tool": item["tool"],
                "input": item.get("input") if isinstance(item.get("input"), dict) else {},
                "approval_token": item["approval_token"],
                "summary": item.get("summary") if isinstance(item.get("summary"), str) else "",
                "carried": item.get("carried") if isinstance(item.get("carried"), int) else 0,
            })
    return tuple(out)


def state_from_rows(
    rows: Iterable[tuple[Any, ...]], user_turns: int,
) -> TurnState:
    """Build a TurnState from the replay window, oldest row first.

    Each row is `(role, text, tool_calls, stop_reason, route[, pending])` — the
    shape both stores map their own records onto; `pending` (B6) is optional so
    a caller that has none can keep passing five. `user_turns` is the
    whole-conversation user count (the window alone undercounts in long chats;
    that is B5).

    The window is NOT trimmed to start on a user row first (get() does that for
    the model's benefit); the CHAT run is paired from the END, so a leading
    orphan assistant row can never join it."""
    window = [
        (row[0], row[1] or "", _tool_calls_of(row[2]), row[3], row[4],
         row[5] if len(row) > 5 else None)
        for row in rows
        if row[0] in ("user", "assistant")
    ]

    last = next((r for r in reversed(window) if r[0] == "assistant"), None)

    # Walk back over complete (user, assistant) pairs while the assistant half
    # was produced on a CHAT tier. Stops at the first FULL/unrouted reply, so
    # CHAT history can never include a FULL reply (hard invariant 3).
    exchanges: list[tuple[str, str]] = []
    i = len(window) - 1
    while (
        i >= 1
        and window[i][0] == "assistant"
        and window[i - 1][0] == "user"
        and route_tier(window[i][4]) in _CHAT_TIERS
    ):
        exchanges.append((window[i - 1][1], window[i][1]))
        i -= 2
    exchanges.reverse()

    return TurnState(
        ok=True,
        last_assistant_text=last[1] if last else None,
        last_tool_calls=tuple(last[2]) if last else (),
        last_stop_reason=last[3] if last else None,
        last_route=last[4] if last else None,
        trailing_chat_exchanges=tuple(exchanges),
        user_turns=int(user_turns or 0),
        last_pending=pending_of(last[5]) if last else (),
    )
