"""Apps access on/off: what a turn can DO, not what it knows.

The companion to hermes.context_mode. This switch controls whether the tool
schemas are sent at all — every Maton app tool (Gmail, Drive, Calendar, Zoom,
Jira, WhatsApp/WATI, QuickBooks…), plus web search, document reads and file
creation. It is the single largest line in a turn's token bill: 10.4–13.9k of
the ~16k input tokens a bare "hi" costs, depending on business scope
(docs/architecture/TURN_COST_PLAN.md §1).

Off, Hermes keeps its business knowledge (subject to hermes.context_mode) but
cannot act: no email, no app reads or writes, no citing documents it would have
had to fetch. The trade is explicit rather than hidden — the system prompt in
that mode tells the model plainly that it has no tools and that apps access is
switched off, because a model told to "call send_email" with no such tool
available narrates fake tool calls instead of answering. Any tool blocks in the
replayed history are sent as text on those calls (a request that declares no
tools can't carry them).

What each combination actually does (TURN_COST_PLAN step 0.3; interactive chat):

    context  apps   result
    on       on     full agent (default)
    on       off    ADVISOR: knows the business, cannot touch anything — the
                    blocks are sent under caveat headers ("nothing here was
                    checked live"), no tool guidance at all
    off      off    PLAIN: plain chat, ~130 tokens for a greeting
    off      on     depends on HERMES_SPLIT_TOGGLES:
                      off (default) — the legacy coupling: Context off strips
                        the tools too, plain prompt (the request is unchanged
                        from before the split);
                      on — tools on, retrieval blocks off, and the
                        standing-rules core (the owner's Hard rules + Security
                        and always-inject memories) sent in their place

Unattended callers (owner decision §8 Q2) don't run at all while this switch
is off — they were written for their tools, and run without them they would
"succeed" with an answer that never looked anything up. So the switch doubles
as an emergency stop for anything that runs without a person watching:
  * Scheduled tasks: hermes.scheduler records each fire as a 'skipped' run,
    OFF_REASON, and delivers nothing. They ignore the Business context switch
    either way (interactive=False always carries full context).
  * AI meeting notes (meeting_copilot.generate_ai_notes): return a skipped
    result without calling the agent, and the endpoint refuses (409) without
    touching the meeting's saved notes. Before the 0.3 split this switch did
    nothing, so notes always had Gmail/Drive — a tool-less ADVISOR reply saved
    over good notes would be a silent regression, not "today's behaviour".
    Unlike the scheduler, notes still run as an INTERACTIVE turn, so Context
    off still strips their tools under the legacy coupling: making them
    interactive=False / autonomy="readonly" (B19) changes their default
    payload and is the open half of §8 Q2.
Both read the switch ONCE and hand that reading to the agent (run_turn's
`apps_on`), so a flip mid-call can't make the run and the agent disagree.

Storage is hermes.settings_flag (Postgres `hermes_settings`, else a
process-global). Fails OPEN to full app access.
"""
from __future__ import annotations

from .settings_flag import Flag

_SETTING_KEY = "apps_enabled"

# Full app access unless someone deliberately turns it off. Fails open for the
# same reason context does: a settings hiccup must not silently turn Hermes
# into a toy that can't reach the user's apps.
_flag = Flag(_SETTING_KEY, default=True, label="apps access")

#: Why an unattended caller didn't run: the scheduler's skipped-run reason
#: (hermes.scheduler.APPS_OFF_REASON carries the same words) and meeting
#: notes' `skipped` value. One wording, so the run history and the meeting
#: page say the same thing.
OFF_REASON = "Apps access is off"


def enabled() -> bool:
    """True when turns should carry the tool schemas and can reach the apps."""
    return _flag.enabled()


def set_enabled(value: bool) -> bool:
    """Switch apps access on/off platform-wide. Returns the new value."""
    return _flag.set_enabled(value)
