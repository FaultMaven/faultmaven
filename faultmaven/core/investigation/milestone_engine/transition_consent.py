"""Whether a user turn confirms or declines a proposed stage transition, read from the same gate-token matcher stage_gates.py uses."""

from .stage_gates import (
    _matches_gate_token,
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
    confirm_patterns = [
        "yes",
        "yeah",
        "yep",
        "yup",
        "correct",
        "confirmed",
        "confirm",
        "approve",
        "approved",
        "ok",
        "okay",
        "sure",
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
        "sounds good",
        "looks good",
        "lgtm",
    ]
    return _matches_gate_token(msg, confirm_patterns)


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
