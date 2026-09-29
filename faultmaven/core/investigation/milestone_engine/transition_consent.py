"""Whether a user turn confirms or declines a proposed stage transition, read from the same gate-token matcher stage_gates.py uses."""

from typing import Optional

from .stage_gates import (
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


def _user_confirms_transition(user_message: str) -> bool:
    """Fallback check for typed confirmations (not DECIDE clicks).

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
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message:
        return False
    if is_substantive_reply(user_message):
        return False
    msg = user_message.strip().lower()
    return _matches_gate_token(msg, list(_EXPLICIT_CONFIRM_TOKENS)) or (
        _matches_gate_token(msg, list(_WEAK_CONFIRM_TOKENS))
    )


def confirmation_token_class(user_message: str) -> Optional[str]:
    """Which class of typed token confirmed, or None when nothing did.

    Returns None exactly when ``_user_confirms_transition`` returns False.
    Otherwise ``"explicit_token"`` if any explicit token matches, and
    ``"weak_token"`` when only a weak one does, so "yes ok" is explicit.
    Same matcher and same substance screen as ``_user_confirms_transition``;
    this only reports which set matched (#1748, the observable behind #723).
    """
    if not _user_confirms_transition(user_message):
        return None
    msg = user_message.strip().lower()
    if _matches_gate_token(msg, list(_EXPLICIT_CONFIRM_TOKENS)):
        return "explicit_token"
    return "weak_token"


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
