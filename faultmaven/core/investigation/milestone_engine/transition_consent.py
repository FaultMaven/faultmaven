"""Whether a user turn confirms or declines a proposed stage transition, read from the same gate-token matcher stage_gates.py uses."""

import re
from typing import Optional

from faultmaven.modules.case.contracts import TerminalConfirmedVia

from .stage_gates import (
    _gate_token_match,
    _matches_gate_token,
)

#: A Slack emoji as it arrives on the wire (``:+1:``, ``:white_check_mark:``).
#: Removed before the bare test so a Slack reply labels as the same reply typed
#: with the Unicode emoji does (#1748). Applied to the lowercased message.
_SLACK_EMOJI_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")

#: Curly apostrophes, as mobile keyboards and macOS autocorrect type them.
#: Every token here is spelled with a straight one ("that's right", "don't"),
#: so a reply is read with its curly ones straightened (#1783).
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})

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

#: Politeness a consent may carry without ceasing to be one ("yes please",
#: "ok thanks"). Never consent on its own: a reply needs a token as well.
_CONSENT_FILLERS = ("please", "thanks", "thank you")

#: A refusal or a deferral, anywhere in a reply (#1783). Any of these vetoes
#: consent and reads as a decline. Bare "no" is deliberately absent: "no
#: problem" and "no worries" would read as refusals, and the whole-reply
#: grammar already refuses "ok no" (which the gate then re-asks once).
_REFUSAL_PHRASES = (
    "don't",
    "do not",
    "not yet",
    "not now",
    "later",
    "wait",
    "hold on",
    "hold off",
    "cancel",
    "stop",
    "never mind",
    "nevermind",
)

_REFUSAL = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(p) for p in _REFUSAL_PHRASES) + r")(?!\w)"
)


def _normalize_reply(user_message: str) -> str:
    """``user_message`` as every matcher here reads it: stripped, lowercased,
    curly apostrophes straightened."""
    return user_message.strip().lower().translate(_APOSTROPHES)


def _reply_refuses(user_message: str) -> bool:
    """Whether ``user_message`` refuses or defers anywhere in it (#1783).

    Word-bounded, so "stopped the pod" is not "stop" and "waiting" is not
    "wait". It vetoes the typed matcher below and a confirmation intent the
    resolver minted from the same text, and it makes the reply a decline. A
    CLICKED confirmation is never vetoed by the text it carries.
    """
    if not user_message:
        return False
    return _REFUSAL.search(_normalize_reply(user_message)) is not None


def confirmation_token_class(user_message: str) -> Optional[TerminalConfirmedVia]:
    """Which class of typed confirmation ``user_message`` is, or None for none.

    The typed-confirmation matcher itself (not DECIDE clicks): None means the
    gate does not read the message as consent, and anything else means it
    does. Its callers take consent from ``is not None``; there is no second
    predicate to drift from this one.

    DECIDE suggestion clicks now carry intent metadata and route
    through IntentType.CONFIRMATION deterministically. This matcher
    is a safety net for users who type instead of clicking.

    Uses a 100-char length guard: short messages are direct responses
    to the confirmation prompt; longer messages likely contain context
    that should go through normal LLM processing.

    A match here executes a TERMINAL transition, so it must be a BARE
    confirmation, and the substance test comes first: a message carrying a
    question or a contrastive continuation ("ok but what is the root
    cause?") is substantive input, not consent — it falls to the
    pending-gate escape lane instead (INV-26: the gate never consumes
    substantive input). The substance test is the shared
    ``is_substantive_reply`` predicate — the same one that guards
    classifier-minted confirmation intents at the IntentResolver adoption
    site (#721), so the two confirm lanes cannot drift apart.

    **Consent is the whole reply (#1783).** The reply confirms only when all
    of it, once curly apostrophes are straightened and Slack emoji shortcodes
    (``:+1:``, ``:white_check_mark:``) removed, is confirmation tokens (the
    explicit and weak sets), the fillers ``please`` / ``thanks`` / ``thank
    you``, and characters that are not letters or digits — with at least one
    token. It is a closed grammar: nothing is parsed for negation, so a word
    it does not know ("ok no", "okay i'll confirm with the team", "sure —
    tomorrow") withholds consent, and the gate re-asks. A refusal or deferral
    anywhere (``_reply_refuses``: "ok, don't close it yet", "sure, do it
    later") vetoes consent as well. A missed consent costs one re-ask; a false
    one closes a case irreversibly. The grammar walks the reply with the
    gate's own matcher (``_gate_token_match``, longest match, word-bounded)
    at each word, so tokens match on word boundaries ("yesterday…" is not
    "yes").

    The class is read from the FIRST token of a consenting reply (#1748). Its
    set says explicit or weak; what follows it says bare or prefixed. BARE
    means no letter or digit anywhere after that token: punctuation, Unicode
    emoji, Slack shortcodes and emoticons made of punctuation keep a reply
    bare ("ok!", "ok 👍", "ok :+1:", "ok =)", "yes :)"). PREFIXED means a
    consent of more than one word — another token or a filler ("ok go ahead",
    "yes please close it", "ok thanks"):

    * ``"explicit_token"`` / ``"explicit_prefixed"`` — the first token is
      explicit, alone or with more;
    * ``"weak_token"`` / ``"weak_prefixed"`` — the first token is weak, alone
      (#723's "bare weak token") or with more.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message:
        return None
    if is_substantive_reply(user_message):
        return None
    msg = _SLACK_EMOJI_SHORTCODE.sub(" ", _normalize_reply(user_message))
    tokens = _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS
    first: Optional[tuple[str, int]] = None
    pos = 0
    while pos < len(msg):
        if not msg[pos].isalnum():
            pos += 1
            continue
        match = _gate_token_match(msg[pos:], tokens + _CONSENT_FILLERS)
        if match is None:
            return None  # a word the consent grammar does not know
        word, length = match
        if first is None and word in tokens:
            first = (word, pos + length)
        pos += length
    if first is None or _reply_refuses(msg):
        return None
    token, end = first
    bare = not any(c.isalnum() for c in msg[end:])
    if token in _EXPLICIT_CONFIRM_TOKENS:
        return "explicit_token" if bare else "explicit_prefixed"
    return "weak_token" if bare else "weak_prefixed"


def _user_declines_transition(user_message: str) -> bool:
    """Check if user message declines a pending transition.

    Opens with a decline token, or refuses or defers anywhere in it
    (``_reply_refuses``, #1783: "ok, don't close it yet" is a decline, not a
    reply the gate cannot read).

    Tokens match on word boundaries — "note db latency spiked" must not
    read as "no", nor "stopped the pod" as "stop" (the old bare
    ``startswith`` swallowed such evidence-bearing messages with a
    canned acknowledgment).
    """
    if not user_message:
        return False
    msg = _normalize_reply(user_message)
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
    return _matches_gate_token(msg, decline_patterns) or _reply_refuses(msg)
