"""Provider credit exhaustion: detect it, remember it, stay off it.

A provider that has run out of credit fails EVERY call until someone tops it
up, so retrying it is pure noise — one top-level turn can otherwise produce a
stack trace per background job (memory extraction, per-business sync, guidance)
for a condition that has nothing to do with the code. Three behaviours belong
together, so they live here:

  * recognise the error across Anthropic / OpenAI / NVIDIA, whose wire shapes
    for "you are out of money" have nothing in common;
  * remember it briefly, so the second through nth caller short-circuits
    instead of making the same doomed request;
  * say it in plain language a user can act on.

Remembered state is deliberately in-process and time-limited, never persisted:
the condition is fixed by topping up somewhere else entirely, and the app must
notice that on its own without anyone restarting it or clearing a flag.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("hermes.credits")

#: How long to treat a provider as out of credit before trying it again. Short
#: enough that a top-up is picked up without a restart, long enough that a busy
#: minute doesn't re-hammer a dead API once per background job.
TTL_SECONDS = 900

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "nvidia": "NVIDIA",
}

#: Substrings that mean "out of credit / over quota" rather than a transient
#: rate limit. Matched case-insensitively against the exception text. Kept
#: explicit because the generic 429 these often arrive as ALSO covers ordinary
#: throttling, which is retryable and must NOT disable the provider.
_CREDIT_PHRASES = (
    "credit balance is too low",        # Anthropic
    "insufficient_quota",               # OpenAI
    "exceeded your current quota",      # OpenAI
    "billing",                          # several
    "payment required",                 # 402, several
    "out of credits",
    "insufficient credits",
    "insufficient balance",
    "quota exceeded",
)

_lock = threading.Lock()
_exhausted: dict[str, float] = {}  # provider -> unix time it was noticed


class CreditsExhausted(RuntimeError):
    """Raised in place of a provider call we already know will fail for money."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.provider = provider_of_scope(scope)
        super().__init__(message(scope))


def scope_for(provider: str, model_id: str | None = None) -> str:
    """The billing scope to track exhaustion against.

    Credit belongs to an ACCOUNT, not to a provider name. Anthropic and OpenAI
    bill one account across every model, so the provider is the right unit.
    NVIDIA NIM is different: build.nvidia.com issues a key per model page, so
    each model can have its own balance — tracking those together would let one
    dry key disable all six.
    """
    if provider == "nvidia" and model_id:
        return f"nvidia:{model_id}"
    return provider


def provider_of_scope(scope: str) -> str:
    """The provider a scope belongs to, for user-facing wording."""
    return scope.split(":", 1)[0]


def is_credit_error(exc: BaseException) -> bool:
    """True if `exc` means the account is out of credit, as opposed to any of
    the other reasons a provider returns 4xx."""
    text = str(exc).lower()
    if any(p in text for p in _CREDIT_PHRASES):
        return True
    # A bare 402 means exactly this even with no recognisable message.
    return getattr(exc, "status_code", None) == 402


def message(scope: str) -> str:
    """The user-facing line. Deliberately says which provider and what to do —
    "something went wrong" would leave the user with no next step. Accepts a
    provider or a full scope; the model suffix is internal bookkeeping."""
    provider = provider_of_scope(scope)
    label = PROVIDER_LABELS.get(provider, provider)
    return (
        f"API credits are out — the {label} account has no remaining balance, "
        "so this model can't answer right now. Add credit to that account, or "
        "pick a different model under Settings › Model."
    )


def note_failure(scope: str, exc: BaseException) -> bool:
    """Record `exc` if it's a credit failure. Returns True if it was one, so
    callers can branch without re-running the check. `scope` comes from
    scope_for() — a provider, or a provider+model for per-key NIM billing."""
    if not is_credit_error(exc):
        return False
    with _lock:
        first = scope not in _exhausted
        _exhausted[scope] = time.time()
    if first:
        # Once per TTL, not once per call — this is the line that replaces a
        # stack trace per background job.
        log.warning(
            "%s is out of API credit; skipping calls to it for the next %d minutes.",
            scope if ":" in scope else PROVIDER_LABELS.get(scope, scope),
            TTL_SECONDS // 60,
        )
    return True


def exhausted(scope: str) -> bool:
    """True while `scope` is known to be out of credit. Expires by itself so a
    top-up is picked up without a restart."""
    with _lock:
        seen = _exhausted.get(scope)
        if seen is None:
            return False
        if time.time() - seen >= TTL_SECONDS:
            del _exhausted[scope]
            return False
        return True


def check(scope: str) -> None:
    """Raise instead of making a call we already know is doomed."""
    if exhausted(scope):
        raise CreditsExhausted(scope)


def clear(scope: str | None = None) -> None:
    """Forget the exhausted state — for tests, and for an explicit retry.
    Clearing a provider also clears every per-model scope beneath it."""
    with _lock:
        if scope is None:
            _exhausted.clear()
            return
        _exhausted.pop(scope, None)
        for key in [k for k in _exhausted if k.startswith(f"{scope}:")]:
            del _exhausted[key]
