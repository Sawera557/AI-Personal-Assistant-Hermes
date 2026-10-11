"""Deterministic per-turn tier router — TURN_COST_PLAN §2 ("Router rules, in
order") and step 1.1.

Before anything expensive is built, `decide()` says how much an interactive
chat turn needs:

  * CHAT  — the WHOLE message is a greeting or closer ("Hi!", "thanks",
            "hiii", "good morning"), nothing is pending and the previous reply
            is not an open FULL reply. Runs (step 1.2) on a ~93-token prompt
            with no tools, no business data and no dispatch code.
  * LIGHT — Phase 3 only: a general-knowledge question on a published 9-pattern
            allow-list. The rule exists here but is OFF unless
            HERMES_ROUTER_LIGHT=strict AND the model is in
            HERMES_ROUTER_LIGHT_MODELS.
  * FULL  — everything else, and every case of doubt: today's path.

Why a closed allow-list and not "short messages are cheap": a short-message
rule leaked 6 of 24 business phrases in measurement ("AFK?", "rent?", "GIF
balance"). CHAT matches only messages made ENTIRELY of greeting/closer phrases
plus six filler words, so anything with a name, a number, a verb or a question
about the user's world falls through to FULL by construction.

Why previous-reply aware: approvals are often given only in prose — "Confirm
and I'll delete A, B, C." leaves no pending metadata — so any reply to an open
FULL turn goes FULL however friendly it sounds ("thanks 👍" there is an
approval). The cost of a wrong FULL is one normal turn; the cost of a wrong
CHAT is an approval answered by a prompt that has no tools and no guardrail.
Every rule below is written so that doubt resolves to FULL.

Properties the rest of the app relies on:

  * Pure: no I/O, no model call, no DB. It reads the environment only through
    hermes.config (mode flags, HERMES_APPS, extra terms), so a test can steer
    it with monkeypatch.setenv. ~10–100 µs per decision.
  * Fail-open means fail to FULL (hard invariant 9): `decide()` wraps the rules
    in try/except, and an unknown flag value, a failed state read, a missing
    state or a non-string message all give FULL.
  * Opt-in by caller (hard invariant 6): `route` defaults to "full". Only the
    two chat endpoints pass route="auto" (step 1.3); the scheduler, meeting
    notes, scripts and tests keep today's path untouched.
  * Shadow-safe: in HERMES_ROUTER_MODE=shadow `decide()` returns the WOULD-BE
    tier; the caller logs it and still runs FULL. Deciding is identical in
    shadow and on, so a shadow week measures exactly what "on" would do.
  * Privacy: decisions are logged as category codes only (`business`, `app`,
    `pending:guardrail`, `open:offer`, …), a word count and a hashed session
    id — never the message text and never the matched substring. The
    prototype's `proper_noun:Nairobi` style reasons are deliberately gone.
    Every code comes from the closed set REASON_CODES, and the log record maps
    anything outside it to "unknown". `decide()` itself does no logging (it
    stays pure); `decide_and_log()` is the routed caller's entry point and
    emits exactly one JSON line per decision while the router is not off.

`_LOW_EFFORT_HINTS` in hermes/agent.py is NOT reused as vocabulary here: it
holds yes, no, ok, list, show me and status — exactly the replies that need
tools. The router's word lists are its own, below.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .businesses import BUSINESS_DISPLAY_NAMES, BUSINESS_TO_ENTITY_SLUG
from .config import APP_LABELS, Config
from .turn_state import TurnState, pending_from, route_tier

log = logging.getLogger("hermes.turn_router")

CHAT = "chat"
LIGHT = "light"
FULL = "full"
TIERS: tuple[str, ...] = (CHAT, LIGHT, FULL)

# Tiers whose replies are "closed" by construction: they had no tools and no
# business data, so a reply after them is not answering an open FULL turn.
_LITE_TIERS: frozenset[str] = frozenset({CHAT, LIGHT})

# The reply stream_turn persists when a turn produced no text at all
# (hermes/agent.py: `reply or "(no response)"`). A nudge after it ("hello?")
# must go FULL — the user is asking for the answer they never got.
NO_RESPONSE = "(no response)"

# Session-key prefix the scheduler gives unattended task runs
# (hermes/scheduler.py: f"__scheduled__:{task_id}"). Never routed.
SCHEDULED_SESSION_PREFIX = "__scheduled__:"

# CHAT is for greetings and closers, which are short. Anything longer is not a
# whole-message greeting, whatever words it contains (§2 rule 6).
_CHAT_MAX_WORDS = 8

# Above this many characters the text rules cannot change the outcome (CHAT
# needs ≤ 8 words; every LIGHT pattern is far shorter), so a pasted document is
# not scanned word by word — it is FULL with the state codes attached.
_MAX_SCAN_CHARS = 2000


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RouterInput:
    """Everything one routing decision may look at.

    `text` is the user's TYPED text only — the caller must not include inlined
    text-file attachments (those set `has_attachments`, which bypasses routing
    altogether). `state` is the TurnState from `store.get_with_state()`.

    `mode` / `light_mode` default to the environment (Config.router_mode() /
    Config.router_light()); pass them explicitly to pin a decision in a test or
    in a shadow comparison. `model` is the model the turn would run on — only
    consulted by the Phase 3 LIGHT rule."""

    text: str
    route: str = "full"
    mode: str | None = None
    interactive: bool = True
    autonomy: str | None = None
    allowed_apps: Sequence[str] | None = None
    session_key: str | None = None
    has_attachments: bool = False
    voice: bool = False
    state: TurnState | None = None
    model: str | None = None
    light_mode: str | None = None


@dataclass(frozen=True)
class Decision:
    """The router's answer.

    `tier` — "chat", "light" or "full" (in shadow mode: the WOULD-BE tier).
    `reason_codes` — category codes only, safe to log and to persist in
        `metadata.route`; never message text.
    `pending_kind` — the open workflow on the previous assistant row
        ("guardrail", "draft", "oauth", see turn_state.pending_from), reported
        whenever the router got far enough to read it, whichever rule decided.
    `affirmation` — the message contains an approval/negation word or an
        approval emoji. Informational: B6's PENDING-ACTION injection must keep
        its own, narrower approval set (TURN_COST_PLAN §7 B6)."""

    tier: str
    reason_codes: tuple[str, ...]
    pending_kind: str | None = None
    affirmation: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form, e.g. for the `route` record of a `done` result."""
        return {
            "tier": self.tier,
            "reason_codes": list(self.reason_codes),
            "pending_kind": self.pending_kind,
            "affirmation": self.affirmation,
        }


# ---------------------------------------------------------------------------
# Vocabulary — rule 6 (CHAT allow-list)
# ---------------------------------------------------------------------------
# Every entry is in NORMALISED form (see _normal_forms): lowercase, no
# apostrophes ("whats"), no punctuation. A message is CHAT only when it can be
# split ENTIRELY into these phrases plus _FILLERS.
_GREETINGS: frozenset[str] = frozenset({
    "hi", "hii", "hey", "heya", "hiya", "hello", "helo", "hallo", "howdy", "yo",
    "greetings",
    "good morning", "good afternoon", "good evening", "good day",
    "morning", "afternoon", "evening", "gm",
    "salaam", "salam", "salaam alaikum", "salam alaikum", "salaam aleikum",
    "assalamu alaikum", "assalamualaikum", "as salaam alaikum", "asalaam alaikum",
    "habari", "habari yako", "habari za asubuhi", "jambo", "hujambo", "mambo",
    "how are you", "how are u", "how r u", "how are you doing", "how are you today",
    "how have you been", "hows your day", "hows your day going",
    "how is your day", "how is your day going",
    "whats up", "wassup", "sup",
})

_CLOSERS: frozenset[str] = frozenset({
    "thanks", "thank you", "thank u", "thankyou", "thx", "thnx", "thanx", "tnx",
    "ty", "tysm", "tyvm", "cheers", "many thanks", "thanks a lot", "thanks a ton",
    "thanks a bunch", "thanks a million", "much appreciated", "appreciated",
    "bye", "bye bye", "byebye", "goodbye", "good bye", "bye for now",
    "see you", "see ya", "see you later", "see you soon", "talk later",
    "talk soon", "ttyl", "good night", "goodnight", "night", "nite", "gn",
    "take care", "have a good day", "have a nice day", "have a great day",
    "have a good night", "asante", "asante sana", "shukran", "shukriya", "ciao",
})

# Acknowledgement small talk. CHAT only on a new chat or right after a CHAT
# reply: after any FULL reply "great"/"perfect"/"cool" can be an approval.
_ACKS: frozenset[str] = frozenset({
    "great", "perfect", "cool", "nice", "awesome", "amazing", "wonderful",
    "excellent", "lovely", "brilliant", "fantastic", "nice one", "good job",
    "great job", "well done", "lol", "haha", "hahaha", "hehe", "lmao", "wow",
})

# Words that may pad a greeting without changing it ("hey there", "thanks so
# much", "hello hermes"). Deliberately exactly the §2 list: "again", "all" and
# "too" — fillers in the prototype — are not, because "thanks again" / "hello
# again" refer back to something.
_FILLERS: frozenset[str] = frozenset({"hermes", "there", "buddy", "so", "very", "much"})

_CHAT_PHRASES: dict[str, str] = {
    **{p: "greeting" for p in _GREETINGS},
    **{p: "closer" for p in _CLOSERS},
    **{p: "ack" for p in _ACKS},
}
_MAX_PHRASE_WORDS = max(len(p.split()) for p in _CHAT_PHRASES)


# ---------------------------------------------------------------------------
# Vocabulary — rules 2 and 4 (approval-ish → FULL, always)
# ---------------------------------------------------------------------------
# Rule 2: approval emoji, checked on the RAW text before normalising strips
# them. Base code points only, so skin-tone modifiers and the U+FE0F variation
# selector ("✔️") still match. U+2713 (✓) is added to the §2 set: it is the
# plain check mark people type when ✔ is not to hand.
_APPROVAL_EMOJI: frozenset[str] = frozenset({
    "\U0001F44D",  # 👍 thumbs up
    "\U0001F44C",  # 👌 OK hand
    "✅",      # check mark button
    "✔",      # heavy check mark
    "☑",      # ballot box with check
    "✓",      # check mark
    "\U0001F197",  # 🆗 OK button
    "\U0001F4AF",  # 💯 hundred points
    "\U0001F64F",  # 🙏 folded hands
    "\U0001F91D",  # 🤝 handshake
})

# Rule 4 — affirmations and negations ANYWHERE in the message, not only as the
# whole message or its prefix (the prototype's rule). "ok thanks" is FULL.
_AFFIRM_WORDS: frozenset[str] = frozenset({
    # affirmations (§2 list + spelling variants)
    "yes", "yeah", "yep", "yup", "ya", "yea", "yah", "yas", "y",
    "ok", "okay", "okey", "okie", "k", "kk", "sure", "fine", "alright", "alrighty",
    "proceed", "confirm", "confirmed", "approve", "approved", "send", "sent",
    "done", "go", "agreed", "agree", "absolutely", "definitely", "correct",
    "right", "please", "pls", "plz", "lgtm",
    # negations
    "no", "nope", "nah", "n", "not", "dont", "never", "nevermind",
    "cancel", "cancelled", "canceled", "stop", "wait", "hold",
})
_AFFIRM_PHRASES: tuple[str, ...] = (
    "go ahead", "do it", "go for it", "not now", "not yet", "sounds good",
    "looks good", "all right", "hold on", "ship it", "send it", "yes please",
    "please do", "of course",
)

# Rule 4 — ordinals ("the second one", "#2", "option 3", a bare "2").
_ORDINAL_WORDS: frozenset[str] = frozenset({
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "last", "next", "previous", "former", "latter", "both",
})
_ORDINAL_NUM = re.compile(r"\b\d+(?:st|nd|rd|th)\b")
_THE_X_ONE = re.compile(r"\bthe(?:\s+\w+){0,3}\s+ones?\b")
_OPTION_NUM = re.compile(r"\b(?:option|number|num|item|choice|no)\s*\d+\b")
_BARE_NUMBER = re.compile(r"^[\d\s]+$")
_HASH_NUM_RAW = re.compile(r"#\s*\d")

# Rule 4 — anaphora: the message points back at something.
_ANAPHORA_WORDS: frozenset[str] = frozenset({
    "it", "its", "itll", "that", "thats", "thatll", "this", "these", "those",
    "them", "they", "theyre", "same", "again", "also",
})


# ---------------------------------------------------------------------------
# Vocabulary — rule 5 (open previous reply)
# ---------------------------------------------------------------------------
# The §2 confirm/offer pattern VERBATIM — unanchored, exactly as published, so
# it also fires inside longer words ("confirmation", "reconfirm", "unconfirmed",
# "proceeding"). A false hit only costs a FULL turn; a narrower pattern could
# put a prose-only approval ("Confirm and I'll delete A, B, C.") under CHAT.
_CONFIRM_OFFER_SPEC = re.compile(
    r"confirm|approve|go ahead|proceed|shall i|should i|do you want|would you like|"
    r"want me to|which (one|business|task|account|file|option)|reply (yes|with)|"
    r"let me know|ok to|are you sure",
    re.IGNORECASE,
)
# Cost-only additions on top of the spec: forms it misses ("approval",
# "approving"), first-person-plural offers, permission questions and explicit
# "say yes" prompts. Whole-word, since "can i"/"may i" would otherwise fire
# inside unrelated words.
_CONFIRM_OFFER_EXTRA = re.compile(
    r"\b(?:approv\w*|shall we|should we|may i|can i|okay to|"
    r"is that (?:ok|okay|alright|right|correct|fine)|say the word|(?:say|type) yes)\b",
    re.IGNORECASE,
)
_OFFER_TAIL_CHARS = 400
# A "?" this close to the end still means the reply asked something, even if a
# short sentence follows it ("Want the PDF too? I can attach it."). Cost-only
# widening of §2's "ends in ?".
_QUESTION_TAIL_CHARS = 100


# ---------------------------------------------------------------------------
# Vocabulary — FULL signals (rule 7's exclusions; also rule 8's reason codes)
# ---------------------------------------------------------------------------
# Generic words from the business names, kept as domain nouns rather than
# business names so "how is the farm doing" reads as `domain`.
_BUSINESS_GENERIC: frozenset[str] = frozenset({
    "farm", "farms", "property", "properties", "commodities", "personal", "family",
    "services",
})
# Spellings owners actually type that are not derivable from the constants.
_BUSINESS_ALIASES: tuple[str, ...] = (
    "afrofarm", "afrofarms", "afro farms", "comfortpca", "comfort services",
    "personal and family",
)

# Static app words on top of the dynamic Config.apps() / APP_LABELS vocabulary
# (a newly added app is picked up from there automatically).
_APP_WORDS: tuple[str, ...] = (
    "gmail", "email", "emails", "e mail", "mail", "inbox", "drive", "doc", "docs",
    "document", "documents", "sheet", "sheets", "spreadsheet", "calendar",
    "meeting", "meetings", "event", "events", "zoom", "jira", "ticket", "tickets",
    "issue", "issues", "whatsapp", "wati", "quickbooks", "qb", "invoice",
    "invoices", "notion", "fathom", "contacts", "contact", "form", "forms", "pdf",
    "file", "files", "attachment", "attachments", "folder", "link", "recording",
    "transcript", "app", "apps",
)
# Slug parts too generic to stand alone as app words ("pdf-co" → "co").
_APP_PART_STOP: frozenset[str] = frozenset({"co"})

_DOMAIN_WORDS: tuple[str, ...] = (
    "rent", "rents", "tenant", "tenants", "lease", "leases", "room", "rooms",
    "unit", "units", "booking", "bookings", "bill", "bills", "payment", "payments",
    "paid", "arrears", "revenue", "income", "expense", "expenses", "profit",
    "cash", "bank", "loan", "salary", "payroll", "staff", "employee", "employees",
    "order", "orders", "harvest", "crop", "crops", "livestock", "inventory",
    "stock", "sales", "customer", "customers", "client", "clients", "supplier",
    "vendor", "contract", "agreement", "deal", "report", "reports", "task",
    "tasks", "project", "projects", "deadline", "budget", "tax", "vat", "kra",
    "account", "accounts", "statement", "receipt", "guardrail", "guardrails",
    "approval", "reminder", "briefing", "note", "notes", "memo", "dashboard",
    "artefact", "artifact", "kpi", "metrics", "letter", "renewal", "credits",
    "usage", "model", "business", "businesses", "company", "portfolio",
    "occupancy", "guest", "guests", "pending", "outstanding", "urgent",
    *_BUSINESS_GENERIC,
)
_ACTION_WORDS: tuple[str, ...] = (
    "send", "sent", "text", "message", "msg", "reply", "forward", "schedule",
    "reschedule", "remind", "create", "make", "generate", "build", "prepare",
    "draft", "write", "update", "edit", "change", "modify", "rename", "move",
    "delete", "remove", "cancel", "assign", "pay", "book", "connect",
    "disconnect", "authorize", "approve", "confirm", "pin", "check", "find",
    "search", "look", "lookup", "fetch", "pull", "get", "show", "list", "open",
    "read", "summarize", "summarise", "summary", "analyze", "analyse", "analysis",
    "review", "track", "monitor", "upload", "download", "share", "save",
    "export", "import", "call", "ring", "invite", "add", "set", "turn", "enable",
    "disable", "run", "start", "stop", "pause", "resume", "attach", "notify",
    "post", "publish", "help", "say", "said", "told", "tell", "mention",
    "mentioned", "wrote", "asked", "called", "emailed", "texted", "replied",
    "fixed", "done", "ready", "sorted", "resolved", "working", "broken",
    "finished", "arrived", "delivered", "signed", "compare", "evaluate",
    "assess", "forecast", "reconcile", "debug", "diagnose", "recommend", "plan",
    "strategy",
)
_PERSONAL_WORDS: tuple[str, ...] = (
    "i", "im", "ive", "id", "ill", "me", "my", "mine", "myself", "we", "us",
    "our", "ours", "ourselves", "your", "yours", "their", "theirs", "his", "her",
    "hers", "the user",
)
_LIVE_WORDS: tuple[str, ...] = (
    "latest", "news", "current", "currently", "recent", "recently", "now",
    "weather", "score", "rate", "rates", "exchange rate", "update", "updates",
    "upcoming", "due", "overdue", "today", "tonight", "tomorrow", "yesterday",
    "this week", "next week", "last week",
)
_MEMORY_WORDS: tuple[str, ...] = (
    "remember", "forget", "recall", "prefer", "preference", "preferences",
    "from now on", "call me", "keep in mind", "note that", "my name",
)
# §2 rule 7's explicit list: quote, price, cost, how much, how many, left,
# remaining, total, explain, delay, status, balance, owe (+ inflections).
_COST_WORDS: tuple[str, ...] = (
    "quote", "quotes", "quotation", "price", "prices", "pricing", "cost", "costs",
    "costing", "how much", "how many", "left", "remaining", "total", "totals",
    "explain", "explanation", "delay", "delays", "delayed", "status", "balance",
    "balances", "owe", "owes", "owed", "owing",
)
_DATE_WORDS: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "weekday", "weekdays",
    "weekend",
)
# Phrases that don't make a question personal or an action ("tell me a joke",
# "how do you say …"); removed before the pronoun / action scan.
_NEUTRAL_PHRASES: tuple[str, ...] = (
    "tell me", "give me", "how do you say", "can you", "could you", "would you",
    "will you",
)

# Raw-text signals, matched BEFORE normalising (they need case, @, :, $, %).
_RAW_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("url", re.compile(r"https?://|www\.|s3://", re.IGNORECASE)),
    ("jira_key", re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")),
    ("phone", re.compile(r"\+?\d[\d\s-]{7,}\d")),
    ("currency", re.compile(
        r"[$£€¥₹₦]|\b(?:kes|ksh|kshs|usd|ugx|aed|eur|gbp|tzs|dollars?|euros?|shillings?)\b"
        r"|\b\d+(?:\.\d+)?\s?(?:k|m|bn)\b",
        re.IGNORECASE,
    )),
    ("percent", re.compile(r"\d\s?%|\bper ?cent\b|\bpercent(?:age)?\b", re.IGNORECASE)),
    ("time", re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)),
    ("date", re.compile(r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b")),
    ("mention", re.compile(r"(?:^|\s)@\w")),
)
_WHO = re.compile(r"\bwho(?:m|se|s|d|ll)?\b")
# "per" is a business slug (Personal & Family) but also an ordinary English
# word ("per night"), so it counts only as an uppercase token or in a slug
# position ("for per", "in per"). A false positive here only costs money.
_PER_UPPER_RAW = re.compile(r"(?<![A-Za-z])PER(?![A-Za-z])")
_PER_SLOT = re.compile(r"\b(?:for|in|on|from|about|under|of|to) per\b")


# ---------------------------------------------------------------------------
# Rule 7 (Phase 3) — the published LIGHT allow-list, TURN_COST_PLAN step 3.2.
# Exactly these nine shapes; anything else is FULL. Matched against
# _light_form(text): lowercase, quotes removed, trailing ?!. stripped, but math
# symbols kept (pattern 1 needs "+", "*", "/").
# ---------------------------------------------------------------------------
_UNIT = (
    r"(?:km|kms|kilomet(?:er|re)s?|miles?|mi|met(?:er|re)s?|m|cm|centimet(?:er|re)s?|mm|"
    r"millimet(?:er|re)s?|inch(?:es)?|in|feet|foot|ft|yards?|yd|kg|kgs|kilograms?|grams?|g|"
    r"pounds?|lbs?|ounces?|oz|stones?|tonnes?|tons?|lit(?:er|re)s?|l|ml|millilit(?:er|re)s?|"
    r"gallons?|gal|pints?|quarts?|cups?|tablespoons?|tbsp|teaspoons?|tsp|"
    r"celsius|fahrenheit|kelvin|degrees(?: celsius| fahrenheit)?|c|f|"
    r"acres?|hectares?|ha|square (?:feet|foot|met(?:er|re)s?|miles?|kilomet(?:er|re)s?)|"
    r"sq ?ft|mph|kph|kmh|km/h|seconds?|minutes?|hours?|days?|weeks?|years?)"
)
_UNIT_NUM = r"(?:\d+(?:\.\d+)?|a|an|one)"
LIGHT_ALLOW_LIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 1. pure arithmetic
    ("arith", re.compile(r"^(what is |whats |calculate |compute )?[\d\s.+\-*/x×÷^()=]+$")),
    # 2. "what is a/an X"
    ("what_is_a", re.compile(r"^(what is|whats|what are) (a|an) [a-z][a-z\s-]{0,40}$")),
    # 3. definitions
    ("define", re.compile(r"^(define|definition of|meaning of) [a-z][a-z\s-]{0,40}$")),
    # 4. a single abstract noun
    ("abstract_noun", re.compile(
        r"^(what is|whats) [a-z-]*(ism|tion|sion|ology|ity|ance|ence|ment|sis|ics)$")),
    # 5. "what does X mean / stand for"
    ("what_does_mean", re.compile(r"^what does [a-z0-9\s-]{1,30} (mean|stand for)$")),
    # 6. "how does a X work"
    ("how_does_work", re.compile(r"^how (do|does) (a |an )?[a-z\s-]{1,40} work$")),
    # 7. translation
    ("translate", re.compile(r"^(translate|how do you say) .{1,80}$")),
    # 8. unit conversion between units on a fixed list (currency stays FULL —
    #    no currency is on the list, and the `currency` signal vetoes it too)
    ("unit_conversion", re.compile(
        rf"^(?:convert |what is |whats )?{_UNIT_NUM} ?{_UNIT} (?:to|into|in) {_UNIT}$")),
    # 9. a joke, fun fact or riddle
    ("joke", re.compile(r"^(tell me|give me) (a |an |another )?(joke|fun fact|riddle)$")),
)
# Patterns whose digits are the point, so "a digit next to a word" is not a
# FULL signal for them (§2 rule 7: "outside pure arithmetic or a whitelisted
# unit conversion").
_DIGITS_ALLOWED: frozenset[str] = frozenset({"arith", "unit_conversion"})


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------
# Apostrophes join ("what's" → "whats", "it's" → "its"); every other
# non-word character (punctuation, emoji, symbols, "_") becomes a space.
_APOSTROPHES = dict.fromkeys(map(ord, "'’‘ʼ`´"), None)
_QUOTES = dict.fromkeys(map(ord, "'’‘ʼ`´\"“”„«»"), None)
_NON_WORD = re.compile(r"[\W_]+")
# Letters (not digits) repeated 3+ times: "hiii", "thanksss", "goooood".
_REPEAT = re.compile(r"([^\W\d_])\1{2,}")


def _base_norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    return " ".join(_NON_WORD.sub(" ", t).split())


def _normal_forms(text: str) -> tuple[str, str]:
    """The message normalised for word matching, in two spellings.

    NFKC, casefold, apostrophes dropped, punctuation and emoji stripped, and
    letters repeated 3+ times collapsed — once to ONE letter ("hiii" → "hi",
    "yesss" → "yes") and once to TWO ("goooood" → "good", "heyyy" → "heyy").
    Neither collapse is right for every word, so both are kept: CHAT accepts
    the message if EITHER spelling is a whole greeting, and every FULL check
    runs on BOTH, so the union can only ever add FULL."""
    base = _base_norm(text)
    return _REPEAT.sub(r"\1", base), _REPEAT.sub(r"\1\1", base)


def _light_form(text: str) -> str:
    """Normalised for the LIGHT allow-list: like _normal_forms but keeping the
    math symbols pattern 1 needs, and dropping quotes and trailing ?!."""
    t = unicodedata.normalize("NFKC", text).casefold().translate(_QUOTES)
    t = " ".join(t.split())
    return t.rstrip("?!. ").strip()


def word_count(text: Any) -> int:
    """Words in the normalised message — the only measure of the text that the
    decision log carries."""
    if not isinstance(text, str):
        return 0
    return len(_normal_forms(text[:_MAX_SCAN_CHARS])[0].split()) if text else 0


# ---------------------------------------------------------------------------
# Compiled vocabularies
# ---------------------------------------------------------------------------
def _phrase_regex(phrases: Iterable[str], *, possessive: bool = False) -> re.Pattern[str] | None:
    """One alternation over whole-word phrases, longest first, in both
    normalised spellings. None for an empty vocabulary.

    `possessive=True` (the proper-noun vocabularies: business names, app
    names, HERMES_ROUTER_EXTRA_TERMS) also matches the phrase with a trailing
    "s": normalising drops apostrophes, so "GAK's rent" arrives as "gaks rent"
    and "Wanjiku's lease" as "wanjikus lease", and a plain whole-word match
    would miss the name. It is NOT used for the generic word lists — there it
    would turn the pronoun "i" into "is" and flag every "what is …". These
    regexes are only ever FULL signals (CHAT matching is _segment's exact
    phrase lookup), so the looser match can only add FULL."""
    forms: set[str] = set()
    for p in phrases:
        if not isinstance(p, str):
            continue
        for form in _normal_forms(p):
            if form:
                forms.add(form)
    if not forms:
        return None
    alternation = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    if possessive:
        # Only multi-letter names take the "s" ("gaks", "nhs"); a one-letter
        # term would otherwise swallow ordinary words.
        plural = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True) if len(f) > 1)
        if plural:
            return re.compile(rf"(?<!\w)(?:(?:{plural})s|{alternation})(?!\w)")
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


_AFFIRM_RX = _phrase_regex((*_AFFIRM_WORDS, *_AFFIRM_PHRASES))
_ORDINAL_RX = _phrase_regex(_ORDINAL_WORDS)
_ANAPHORA_RX = _phrase_regex(_ANAPHORA_WORDS)
_STATIC_SIGNALS: tuple[tuple[str, re.Pattern[str] | None], ...] = (
    ("domain", _phrase_regex(_DOMAIN_WORDS)),
    ("action", _phrase_regex(_ACTION_WORDS)),
    ("personal", _phrase_regex(_PERSONAL_WORDS)),
    ("live", _phrase_regex(_LIVE_WORDS)),
    ("memory", _phrase_regex(_MEMORY_WORDS)),
    ("cost_word", _phrase_regex(_COST_WORDS)),
    ("date", _phrase_regex(_DATE_WORDS)),
)
_STATIC_APP_RX = _phrase_regex(_APP_WORDS)


def _business_terms() -> set[str]:
    """Business slugs, entity slugs, display names and aliases — everything
    that names one of the owner's businesses. "per" is excluded here and
    handled by _PER_UPPER_RAW / _PER_SLOT (it is an ordinary English word)."""
    terms: set[str] = set()
    for slug, entity in BUSINESS_TO_ENTITY_SLUG.items():
        if slug != "per":
            terms.add(slug)
        terms.add(entity.replace("_", " "))
    for slug, name in BUSINESS_DISPLAY_NAMES.items():
        if slug != "per":
            terms.add(slug.replace("_", " "))
        terms.add(name)
        # Distinctive single words of a display name ("Nour", "Hammam",
        # "Comfort") — the generic ones ("farm", "properties") are domain nouns.
        for word in _base_norm(name).split():
            if word not in _BUSINESS_GENERIC and len(word) > 1:
                terms.add(word)
    terms.update(_BUSINESS_ALIASES)
    terms.discard("per")
    terms.discard("personal")  # entity slug "personal" — a common word; generic
    return terms


def _app_terms(apps: Iterable[str]) -> set[str]:
    """Every enabled app slug, every known app label, and their parts: "google
    mail", "gmail", "zoom admin", "drive", … Built from Config.apps() AND
    APP_LABELS, so a newly enabled app forces FULL with no code change, and a
    disabled app's name still does."""
    terms: set[str] = set()
    for slug in {*apps, *APP_LABELS}:
        if not isinstance(slug, str) or not slug.strip():
            continue
        phrase = _base_norm(slug.replace("-", " "))
        terms.add(phrase)
        terms.update(p for p in phrase.split() if p not in _APP_PART_STOP and len(p) > 1)
        label = APP_LABELS.get(slug)
        if label:
            # The name part only — "Zoom (your own meetings…)" → "zoom".
            terms.add(label.split("(", 1)[0])
    return terms


@functools.lru_cache(maxsize=8)
def _dynamic_vocab(
    apps: tuple[str, ...], extra_terms: tuple[str, ...],
) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None, re.Pattern[str] | None]:
    """(business, app, extra-term) regexes. Cached per (HERMES_APPS,
    HERMES_ROUTER_EXTRA_TERMS) value, so an env change takes effect on the next
    decision while a steady config compiles once."""
    return (
        _phrase_regex(_business_terms(), possessive=True),
        _phrase_regex(_app_terms(apps), possessive=True),
        _phrase_regex(extra_terms, possessive=True),
    )


def _vocab() -> tuple[re.Pattern[str] | None, re.Pattern[str] | None, re.Pattern[str] | None]:
    return _dynamic_vocab(tuple(Config.apps()), tuple(Config.router_extra_terms()))


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------
def _search(rx: re.Pattern[str] | None, text: str) -> bool:
    return bool(rx is not None and rx.search(text))


def _resolve_mode(mode: str | None) -> str:
    """HERMES_ROUTER_MODE for this decision; unknown → "off"."""
    if mode is None:
        return Config.router_mode()
    value = mode.strip().lower() if isinstance(mode, str) else ""
    return value if value in Config.ROUTER_MODES else "off"


def _resolve_light(light_mode: str | None) -> str:
    """HERMES_ROUTER_LIGHT for this decision; unknown → "off"."""
    if light_mode is None:
        return Config.router_light()
    value = light_mode.strip().lower() if isinstance(light_mode, str) else ""
    return value if value in Config.ROUTER_LIGHT_MODES else "off"


def _bypass_codes(inp: RouterInput, mode: str) -> list[str]:
    """Rule 1. Every reason this turn is never routed, in a fixed order."""
    codes: list[str] = []
    if mode == "off":
        codes.append("bypass:mode_off")
    if inp.route != "auto":
        codes.append("bypass:route")
    if inp.interactive is not True:
        codes.append("bypass:unattended")
    if inp.autonomy is not None:
        codes.append("bypass:autonomy")
    # `allowed_apps` is only ever passed by restricted, unattended callers (the
    # scheduler); the chat endpoints pass None. An explicit [] is still a
    # caller that chose to restrict, so it is treated as set.
    if inp.allowed_apps is not None:
        codes.append("bypass:allowed_apps")
    if isinstance(inp.session_key, str) and inp.session_key.startswith(SCHEDULED_SESSION_PREFIX):
        codes.append("bypass:scheduled")
    if inp.has_attachments:
        codes.append("bypass:attachments")
    # A failed state read must never look like a new chat (B23): Postgres get()
    # returns [] on error, and TurnState.ok=False is the explicit flag.
    state = inp.state
    if state is None or getattr(state, "ok", False) is not True:
        codes.append("bypass:state")
    return codes


def _has_previous_reply(state: TurnState) -> bool:
    return (
        state.last_assistant_text is not None
        or bool(state.last_tool_calls)
        or state.last_stop_reason is not None
        or state.last_route is not None
    )


def _reply_tail(text: str) -> str:
    """The last _OFFER_TAIL_CHARS characters of an assistant reply, NFKC-folded
    (a full-width "？" is a "?") and with every whitespace run — newlines, the
    markdown line breaks between "Shall" and "I" — collapsed to one space, so
    the confirm/offer phrases match however the reply was wrapped. Folding can
    only make the rule-5 checks fire more often, never less."""
    tail = unicodedata.normalize("NFKC", text[-(_OFFER_TAIL_CHARS * 2):])
    return " ".join(tail.split())[-_OFFER_TAIL_CHARS:]


def _ends_with_question(tail: str) -> bool:
    """The reply ends in "?" — ignoring trailing whitespace, markdown, closing
    quotes/brackets and emoji ("Shall I send it? 🙂") — or has a "?" in its
    last _QUESTION_TAIL_CHARS characters. `tail` comes from _reply_tail."""
    end = len(tail)
    while end and not (tail[end - 1].isalnum() or tail[end - 1] in "?.!"):
        end -= 1
    trimmed = tail[:end]
    return trimmed.endswith("?") or "?" in trimmed[-_QUESTION_TAIL_CHARS:]


def _is_offer(tail: str) -> bool:
    """The reply's tail asks for a confirmation or offers to act (§2 rule 5's
    pattern, plus the cost-only extras). `tail` comes from _reply_tail."""
    return bool(_CONFIRM_OFFER_SPEC.search(tail) or _CONFIRM_OFFER_EXTRA.search(tail))


def _open_codes(state: TurnState) -> list[str]:
    """Rule 5: is the previous assistant reply still OPEN?

    After a FULL (or unrouted) reply — every row written before routing
    existed — the reply is open when it used any tool, asked or offered
    something, stopped for any reason but end_turn (a missing stop_reason
    included), or produced no text. Any message after an open reply is FULL,
    however friendly: "thanks 👍" after "Confirm and I'll delete A, B, C." is
    an approval.

    After a CHAT/LIGHT reply the question/offer checks are skipped (§2 rule 6:
    a LITE_CHAT reply often ends with "anything I can help with?", and it can
    have offered nothing real). The failure checks still apply there — a tool
    trace, an explicit non-end_turn stop, or an empty / "(no response)" reply —
    which goes beyond §2's letter (rule 5 is written for FULL rows only) but
    can only add FULL: a nudge after a failed reply is never small talk."""
    if not _has_previous_reply(state):
        return []
    lite = route_tier(state.last_route) in _LITE_TIERS
    text = state.last_assistant_text if isinstance(state.last_assistant_text, str) else ""
    stop = state.last_stop_reason
    stop = stop.strip().lower() if isinstance(stop, str) else None

    codes: list[str] = []
    if state.last_tool_calls:
        codes.append("open:tool")
    if (stop is not None and stop != "end_turn") if lite else (stop != "end_turn"):
        codes.append("open:stopped")
    if text.strip() in ("", NO_RESPONSE):
        codes.append("open:failed")
    if not lite:
        tail = _reply_tail(text)
        if _ends_with_question(tail):
            codes.append("open:question")
        if _is_offer(tail):
            codes.append("open:offer")
    return codes


def _approval_codes(forms: tuple[str, str], raw: str) -> list[str]:
    """Rule 4: approval-ish words ANYWHERE → FULL. Category codes only."""
    both = forms[0] if forms[0] == forms[1] else f"{forms[0]} | {forms[1]}"
    codes: list[str] = []
    if _search(_AFFIRM_RX, both):
        codes.append("affirmation")
    if (
        _search(_ORDINAL_RX, both)
        or _ORDINAL_NUM.search(both)
        or _THE_X_ONE.search(both)
        or _OPTION_NUM.search(both)
        or _HASH_NUM_RAW.search(raw)
        or (forms[0] and _BARE_NUMBER.match(forms[0]))
    ):
        codes.append("ordinal")
    if _search(_ANAPHORA_RX, both):
        codes.append("anaphora")
    return codes


def _vocab_codes(raw: str, both: str) -> set[str]:
    """Business / app / extra-term hits — the proper-noun FULL signals that
    also veto CHAT (an operator-listed name forces FULL everywhere)."""
    business_rx, app_rx, extra_rx = _vocab()
    hits: set[str] = set()
    if _search(business_rx, both) or _PER_UPPER_RAW.search(raw) or _PER_SLOT.search(both):
        hits.add("business")
    if _search(app_rx, both) or _search(_STATIC_APP_RX, both):
        hits.add("app")
    if _search(extra_rx, both):
        hits.add("extra_term")
    return hits


def _signal_codes(raw: str, both: str, light_text: str, pattern: str | None) -> set[str]:
    """Every FULL signal of §2 rule 7, as category codes. Used to veto LIGHT and
    to explain a rule-8 FULL in the decision log."""
    hits = _vocab_codes(raw, both)
    for code, rx in _RAW_SIGNALS:
        if rx.search(raw):
            hits.add(code)
    scrubbed = f" {both} "
    for phrase in _NEUTRAL_PHRASES:
        scrubbed = scrubbed.replace(f" {phrase} ", " ")
    for code, rx in _STATIC_SIGNALS:
        if _search(rx, scrubbed):
            hits.add(code)
    if _WHO.search(both):
        hits.add("who")
    # A capitalised word after the first: a name, a place, an acronym.
    for word in raw.split()[1:]:
        letters = "".join(ch for ch in word if ch.isalpha())
        if len(letters) > 1 and letters[0].isupper():
            hits.add("cap_word")
            break
    if pattern not in _DIGITS_ALLOWED and re.search(r"\d", light_text) and re.search(r"[^\W\d_]", light_text):
        hits.add("digit_word")
    return hits


def _segment(text: str) -> tuple[str, ...] | None:
    """Split a normalised message into CHAT phrases (longest match first) and
    fillers. Returns the phrase kinds ("greeting", "closer", "ack"), or None if
    any word is outside the allow-list, the message is empty or too long, or it
    is fillers only."""
    words = text.split()
    if not words or len(words) > _CHAT_MAX_WORDS:
        return None
    kinds: list[str] = []
    i = 0
    while i < len(words):
        for n in range(min(_MAX_PHRASE_WORDS, len(words) - i), 0, -1):
            kind = _CHAT_PHRASES.get(" ".join(words[i:i + n]))
            if kind:
                kinds.append(kind)
                i += n
                break
        else:
            if words[i] in _FILLERS:
                i += 1
                continue
            return None
    return tuple(kinds) or None


def _light_pattern(light_text: str) -> str | None:
    for name, rx in LIGHT_ALLOW_LIST:
        if rx.match(light_text):
            return name
    return None


# ---------------------------------------------------------------------------
# The closed reason-code vocabulary
# ---------------------------------------------------------------------------
# Every code `decide()` can put on a Decision. Built from the same tables the
# rules use, so a new signal or LIGHT pattern is covered automatically. The
# decision log maps anything outside this set to "unknown" (belt and braces:
# no future code built from message text can reach a log line), and
# tests/test_turn_router.py asserts every code a decision carries is in it.
REASON_CODES: frozenset[str] = frozenset({
    # rule 1
    "bypass:mode_off", "bypass:route", "bypass:unattended", "bypass:autonomy",
    "bypass:allowed_apps", "bypass:scheduled", "bypass:attachments", "bypass:state",
    # input guards and fail-open
    "bad_input", "too_long", "router_error", "no_match",
    # rules 2–5
    "approval_emoji", "empty",
    "pending:guardrail", "pending:draft", "pending:oauth",
    "affirmation", "ordinal", "anaphora",
    "open:tool", "open:stopped", "open:failed", "open:question", "open:offer",
    # rule 6
    "greeting", "closer", "ack", "ack_after_full",
    # rule 7
    "light:model", "light:shadow", *(f"light:{name}" for name, _ in LIGHT_ALLOW_LIST),
    # FULL signals (rule 7's exclusions, rule 8's explanation)
    "business", "app", "extra_term", "who", "cap_word", "digit_word",
    *(code for code, _ in _RAW_SIGNALS),
    *(code for code, _ in _STATIC_SIGNALS),
})


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def decide(inp: RouterInput) -> Decision:
    """Route one turn: CHAT, LIGHT or FULL (§2 rules 1–8, first match wins).

    Never raises. Any exception — a malformed state, a bad env value, a bug —
    returns FULL with the single reason code `router_error`; only the exception
    TYPE is logged, never its message (which could quote the user's text)."""
    try:
        return _decide(inp)
    except Exception as exc:  # noqa: BLE001 — fail open to FULL (hard invariant 9)
        try:
            log.warning("turn_router: decide() raised %s; routing FULL", type(exc).__name__)
        except Exception:  # noqa: BLE001 — logging must not break a turn either
            pass
        return Decision(FULL, ("router_error",))


def _decide(inp: RouterInput) -> Decision:
    # Rule 1 — bypass: flag off, caller pinned FULL, unattended, restricted,
    # scheduled, attachments, unreadable state. Nothing about the text is read.
    mode = _resolve_mode(inp.mode)
    bypass = _bypass_codes(inp, mode)
    if bypass:
        return Decision(FULL, tuple(bypass))

    state: TurnState = inp.state  # type: ignore[assignment]  # checked by rule 1
    raw = inp.text
    if not isinstance(raw, str):
        return Decision(FULL, ("bad_input",))
    pending = pending_from(state.last_tool_calls)

    # A pasted document is never small talk; skip the word scans.
    if len(raw) > _MAX_SCAN_CHARS:
        codes = ["too_long", *([f"pending:{pending}"] if pending else []), *_open_codes(state)]
        return Decision(FULL, tuple(codes), pending, False)

    forms = _normal_forms(raw)
    both = forms[0] if forms[0] == forms[1] else f"{forms[0]} | {forms[1]}"
    codes: list[str] = []
    affirmation = False

    # Rule 2 — raw-text checks, before normalising: an approval emoji, or
    # nothing left once punctuation and emoji are stripped ("🙂", "?", "...").
    if any(ch in _APPROVAL_EMOJI for ch in raw):
        codes.append("approval_emoji")
        affirmation = True
    if not forms[0]:
        codes.append("empty")

    # Rule 3 — a workflow is open on the last assistant row (held guardrail
    # confirmation, unsent attachment draft, OAuth link).
    if pending:
        codes.append(f"pending:{pending}")

    # Rule 4 — approval-ish words anywhere: affirmations, negations, ordinals,
    # anaphora. "ok thanks", "thanks for that", "hello again" → FULL.
    approval = _approval_codes(forms, raw)
    if "affirmation" in approval:
        affirmation = True
    codes.extend(approval)

    # Rule 5 — the previous reply is still open.
    codes.extend(_open_codes(state))

    # Rules 1–5 can only ever say FULL, so their codes are collected together:
    # the log then shows every reason ("pending:guardrail" AND "affirmation").
    if codes:
        return Decision(FULL, tuple(codes), pending, affirmation)

    has_prev = _has_previous_reply(state)
    prev_tier = route_tier(state.last_route) if has_prev else None
    vocab_hits = _vocab_codes(raw, both)

    # Rule 6 — the CHAT allow-list: the WHOLE message is greetings/closers (+
    # fillers), at most 8 words, naming no business, app or extra term.
    if not vocab_hits:
        kinds = _segment(forms[0]) or _segment(forms[1])
        if kinds:
            if "ack" in kinds and has_prev and prev_tier != CHAT:
                # "great"/"cool"/"perfect" after a FULL (or LIGHT, or unrouted)
                # reply can be an approval.
                return Decision(FULL, ("ack_after_full",), pending, affirmation)
            return Decision(CHAT, tuple(sorted(set(kinds))), pending, affirmation)

    light_text = _light_form(raw)
    pattern = _light_pattern(light_text)

    # Rule 7 — Phase 3 LIGHT, OFF unless HERMES_ROUTER_LIGHT=strict.
    light_mode = _resolve_light(inp.light_mode)
    if light_mode != "off" and pattern is not None and (prev_tier in _LITE_TIERS or not has_prev):
        signals = _signal_codes(raw, both, light_text, pattern)
        if not signals:
            if inp.model is None or inp.model not in Config.router_light_models():
                return Decision(FULL, ("light:model", f"light:{pattern}"), pending, affirmation)
            if light_mode == "strict":
                return Decision(LIGHT, (f"light:{pattern}",), pending, affirmation)
            return Decision(FULL, ("light:shadow", f"light:{pattern}"), pending, affirmation)
        return Decision(FULL, tuple(sorted(signals)), pending, affirmation)

    # Rule 8 — everything else is FULL. The codes say why it was not CHAT.
    reasons = _signal_codes(raw, both, light_text, pattern)
    if len(forms[0].split()) > _CHAT_MAX_WORDS:
        reasons.add("too_long")
    return Decision(FULL, tuple(sorted(reasons)) or ("no_match",), pending, affirmation)


# ---------------------------------------------------------------------------
# Decision logging — category codes only
# ---------------------------------------------------------------------------
def session_hash(session_key: Any) -> str | None:
    """A short, stable, non-reversible id for a session key, so log lines from
    one conversation can be grouped without the log holding the key itself."""
    if not isinstance(session_key, str) or not session_key:
        return None
    return hashlib.sha256(f"hermes-router:{session_key}".encode("utf-8")).hexdigest()[:12]


def decision_log_record(
    inp: RouterInput, decision: Decision, *, effort: str | None = None,
) -> dict[str, Any]:
    """The one JSON-able record logged per routed turn (§6 "Decision logging").

    Every value comes from a closed vocabulary or is a number/hash: tier,
    reason category codes, pending kind, word count, hashed session id, flag
    modes, route, voice, effort. The message text and any matched substring
    never enter it — tests/test_turn_router.py asserts no message token can be
    found in the serialised line."""
    route = inp.route if inp.route in ("auto", "full") else "other"
    try:
        mode = _resolve_mode(inp.mode)
        light = _resolve_light(inp.light_mode)
    except Exception:  # noqa: BLE001
        mode = light = "unknown"
    pending = decision.pending_kind
    return {
        "event": "turn_router.decision",
        "tier": decision.tier if decision.tier in TIERS else "unknown",
        "reasons": [c if c in REASON_CODES else "unknown" for c in decision.reason_codes],
        "pending": pending if f"pending:{pending}" in REASON_CODES else None,
        "affirmation": bool(decision.affirmation),
        "words": word_count(inp.text),
        "session": session_hash(inp.session_key),
        "mode": mode,
        "light": light,
        "route": route,
        "voice": bool(inp.voice),
        "effort": effort if effort in ("low", "medium", "high") else None,
    }


def log_decision(
    inp: RouterInput, decision: Decision, *, effort: str | None = None,
) -> str | None:
    """Emit the decision as ONE JSON log line on `hermes.turn_router` (INFO)
    and return it. Never raises — a logging failure must not break a turn."""
    try:
        line = json.dumps(
            decision_log_record(inp, decision, effort=effort),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        log.info("%s", line)
        return line
    except Exception:  # noqa: BLE001
        return None


def decide_and_log(inp: RouterInput, *, effort: str | None = None) -> Decision:
    """`decide()` plus exactly ONE decision log line — the entry point a routed
    caller uses (step 1.3 wires it into stream_turn for route="auto").

    Nothing is logged while HERMES_ROUTER_MODE resolves to "off": the router is
    not running then, and a line per chat turn would be noise that says only
    "bypass:mode_off". In shadow and on, every decision is logged, bypasses
    included, so the shadow report sees the whole population of chat turns.
    Never raises: decide() and log_decision() both fail safe on their own."""
    decision = decide(inp)
    try:
        mode = _resolve_mode(inp.mode)
    except Exception:  # noqa: BLE001 — an unreadable mode is "off" (invariant 9)
        mode = "off"
    if mode != "off":
        log_decision(inp, decision, effort=effort)
    return decision
