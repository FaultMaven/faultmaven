"""Whether a user turn confirms or declines a proposed stage transition, read from the same gate-token matcher stage_gates.py uses."""

from typing import Optional

from .stage_gates import (
    _contains_gate_token,
    _matches_gate_token,
)

# Bare tokens that carry little intent on their own: #723's Note 1 list. Kept
# apart from the explicit set so a terminal transition confirmed by one of
# these alone is countable (#1748). Matching behaviour is the union of both.
_WEAK_CONFIRM_TOKENS = ("ok", "okay", "sure", "sounds good", "looks good", "lgtm")

_EXPLICIT_CONFIRM_TOKENS = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "correct",
    "confirmed",
    "confirm",
    "approve",
    "approved",
    "absolutely",
    "go ahead",
    "go for it",
    "do it",
    "please do",
    "proceed",
    "mark as resolved",
    "mark it as resolved",
    "resolve it",
    "close it",
    "that's right",
    "that's correct",
)


def confirmation_token_class(user_message: str) -> Optional[str]:
    """Which class of typed confirmation ``user_message`` is, or None for none.

    The typed-confirmation matcher itself (not DECIDE clicks): None means the
    gate does not read the message as consent, and ``_user_confirms_transition``
    is exactly "this is not None".

    DECIDE suggestion clicks now carry intent metadata and route
    through IntentType.CONFIRMATION deterministically. This matcher
    is a safety net for users who type instead of clicking.

    Uses a 100-char length guard: short messages are direct responses
    to the confirmation prompt; longer messages likely contain context
    that should go through normal LLM processing.

    A match here executes a TERMINAL transition, so it must be a BARE
    confirmation: tokens match on word boundaries ("yesterday…" is not
    "yes"), and a message carrying a question or a contrastive
    continuation ("ok but what is the root cause?") is substantive
    input, not consent — it falls to the pending-gate escape lane
    instead (INV-26: the gate never consumes substantive input). The
    substance test is the shared ``is_substantive_reply`` predicate —
    the same one that guards classifier-minted confirmation intents at
    the IntentResolver adoption site (#721), so the two confirm lanes
    cannot drift apart.

    Whether the message confirms is decided by its OPENING token, over both
    sets. Which class it is reads the WHOLE message (#1748): ``"explicit_token"``
    when an explicit token appears anywhere in it — "ok, go ahead" and
    "sure, close it" open on a weak token and still say what they mean — and
    ``"weak_token"`` only when none does.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message:
        return None
    if is_substantive_reply(user_message):
        return None
    msg = user_message.strip().lower()
    if not _matches_gate_token(msg, _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS):
        return None
    if _contains_gate_token(msg, _EXPLICIT_CONFIRM_TOKENS):
        return "explicit_token"
    return "weak_token"


def _user_confirms_transition(user_message: str) -> bool:
    """Fallback check for typed confirmations (not DECIDE clicks).

    The verdict of :func:`confirmation_token_class`, which carries the rules.
    """
    return confirmation_token_class(user_message) is not None


def _user_declines_transition(user_message: str) -> bool:
    """Check if user message declines a pending transition.

    Tokens match on word boundaries — "note db latency spiked" must not
    read as "no", nor "stopped the pod" as "stop" (the old bare
    ``startswith`` swallowed such evidence-bearing messages with a
    canned acknowledgment).
    """
    if not user_message:
        return False
    msg = user_message.strip().lower()
    decline_patterns = [
        "no",
        "nope",
        "not yet",
        "wait",
        "cancel",
        "don't",
        "not ready",
        "hold on",
        "stop",
    ]
    return _matches_gate_token(msg, decline_patterns)
