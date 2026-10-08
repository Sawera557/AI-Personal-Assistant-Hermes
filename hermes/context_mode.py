"""Business context on/off: what a turn KNOWS, not what it can DO.

Every turn normally ships the agent's whole working knowledge — long-term
memory, the business context pack, retrieved documents, past-conversation
recall. That is right when you are doing work and pure waste when you type
"Hi". An early measurement on this deployment (one business locked, so RAG and
a business pack were included) itemised a trivial message as

    tool schemas .... 11,000   (65%)   <- hermes.apps_access, NOT this switch
    memory block .....1,880
    context pack .....1,873
    base prompt ......1,586
    retrieved RAG ......660
                     ------
                     16,999

(This docstring used to quote ~19,950 for the total; the parts it listed sum to
16,999. docs/architecture/TURN_COST_PLAN.md §1 has the current breakdown: the
tool schemas alone are 10.4–13.9k depending on business scope.)

What turning THIS switch off actually does (TURN_COST_PLAN step 0.3):

  * Interactive chat, HERMES_SPLIT_TOGGLES off (the default — today's
    behaviour): the retrieval blocks are dropped AND the tools go with them.
    The turn answers from a ~115-token plain prompt that says business context
    is off. That is the old single-switch coupling, kept until the flag is
    turned on, so the default request is unchanged.
  * Interactive chat, HERMES_SPLIT_TOGGLES on: only the retrieval blocks are
    dropped. Every tool Apps access allows is still sent, so Hermes looks things
    up instead of having been handed them — and the standing-rules core (the
    global pack's Hard rules and Security sections, plus the owner's and the
    business's always-inject memories) rides along in place of the pack, so
    tools never travel without the owner's hard rules.
  * Unattended runs (the scheduler; interactive=False) ignore this switch and
    always get full context. A scheduled task written against the owner's rules
    and documents must not quietly run without them because chat was made
    cheaper.

The tool schemas have their own switch: hermes.apps_access. Turn both off for
the cheapest possible plain chat.

Storage is hermes.settings_flag (Postgres `hermes_settings`, else a
process-global). Fails OPEN to full context.
"""
from __future__ import annotations

from .settings_flag import Flag

_SETTING_KEY = "context_enabled"

# Full context unless someone deliberately turns it off.
_flag = Flag(_SETTING_KEY, default=True, label="business context")


def enabled() -> bool:
    """True when turns should carry memory, context pack and retrieved docs."""
    return _flag.enabled()


def set_enabled(value: bool) -> bool:
    """Switch business context on/off platform-wide. Returns the new value."""
    return _flag.set_enabled(value)
