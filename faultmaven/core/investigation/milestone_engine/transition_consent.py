"""Whether a user turn confirms or declines a proposed terminal transition.

Every ``pending_transition`` is a terminal proposal (RESOLVED or CLOSED), and a
terminal state has no outgoing edge. So the gate reads consent narrowly (#1783,
ruling (a), 2026-09-29): a proposal executes on its click, or on a typed reply
that is, as a whole, one consent token for the proposal's target. Any other
typed reply is re-asked, and a re-ask never records a refusal.
"""

from typing import Literal, Optional

from faultmaven.modules.case.contracts import TerminalConfirmedVia

from .stage_gates import _matches_gate_token

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

#: Tokens that name one target, and so consent only to a proposal for it: a
#: bare "close it" is not consent to a pending RESOLVED, nor "resolve it" to a
#: pending CLOSE (#1783). Every other token consents to either target.
_TARGET_SCOPED_TOKENS: dict[str, str] = {
    "close it": "closed",
    "resolve it": "resolved",
    "mark as resolved": "resolved",
    "mark it as resolved": "resolved",
}

#: The positive decorations a bare consent may carry around its token: Unicode
#: emoji, Slack's wire shortcodes for them, and emoticons. A closed list, so
#: anything else (a negative emoji, ``:(``, ``:thinking_face:``, a letter or a
#: symbol) is left in place and the reply is not bare. Tried longest first, and
#: in a fixed order, so ``:-)`` is removed whole rather than as ``:)``.
_POSITIVE_DECORATIONS: tuple[str, ...] = tuple(
    sorted(
        {
            # emoji
            "\U0001f44d",  # 👍
            "\U0001f44c",  # 👌
            "✅",  # ✅
            "✔",  # ✔
            "☑",  # ☑
            "\U0001f64f",  # 🙏
            "\U0001f642",  # 🙂
            "\U0001f60a",  # 😊
            "\U0001f600",  # 😀
            "\U0001f603",  # 😃
            "\U0001f604",  # 😄
            "\U0001f601",  # 😁
            "\U0001f389",  # 🎉
            "\U0001f4af",  # 💯
            "\U0001f64c",  # 🙌
            "\U0001f44f",  # 👏
            # Slack shortcodes
            ":+1:",
            ":thumbsup:",
            ":ok_hand:",
            ":white_check_mark:",
            ":heavy_check_mark:",
            ":ballot_box_with_check:",
            ":pray:",
            ":slightly_smiling_face:",
            ":smile:",
            ":smiley:",
            ":blush:",
            ":grinning:",
            ":tada:",
            ":100:",
            ":raised_hands:",
            ":clap:",
            ":skin-tone-2:",
            ":skin-tone-3:",
            ":skin-tone-4:",
            ":skin-tone-5:",
            ":skin-tone-6:",
            # emoticons
            ":)",
            ":-)",
            "=)",
            ":]",
            "(y)",
        },
        key=lambda decoration: (-len(decoration), decoration),
    )
)

#: Code points that only modify the emoji before them: the emoji presentation
#: selector and the five skin tones. Removed outright, so ``✔️`` and ``👍🏽``
#: read as ``✔`` and ``👍``.
_EMOJI_MODIFIERS = frozenset({"️", *(chr(c) for c in range(0x1F3FB, 0x1F400))})

PendingGateVerdict = Literal["confirm", "decline", "reask", "not_an_answer"]


def _normalize_reply(user_message: str) -> str:
    """``user_message`` stripped and lowercased, with curly single quotes made
    straight, so ``that’s right`` (mobile and macOS autocorrect) reads as
    ``that's right`` and ``don’t`` as ``don't``. Every matcher here reads it."""
    return user_message.strip().lower().replace("\u2019", "'").replace("\u2018", "'")


def confirmation_token_class(
    user_message: str, to_state: Optional[str]
) -> Optional[TerminalConfirmedVia]:
    """Which class of BARE typed consent ``user_message`` is to a proposal for
    ``to_state``, or None when it is not one.

    ``"explicit_token"`` or ``"weak_token"`` (#723's bare weak token), and
    nothing else: a reply is consent only when the WHOLE of it is one consent
    token (#1783, ruling (a)). Around the token it may carry only trailing
    ``.``, ``!`` and ``,``, whitespace, and the positive decorations
    (``_POSITIVE_DECORATIONS``): ``yes``, ``ok!``, ``lgtm 👍``, ``ok :+1:``,
    ``yes :-)``. Anything more is not consent, whatever it says: ``ok go
    ahead``, ``ok, don't close it yet``, ``ok 👎``, ``❌ close it``, ``ok?``.
    Such a reply is re-asked, never executed (``pending_gate_verdict``).

    A decoration is replaced by a space, never by nothing, so a decoration
    inside a word cannot reassemble a token (``clo(y)se it``, ``o👍k``). A
    target-scoped token (``_TARGET_SCOPED_TOKENS``) consents only to its own
    target.

    The shared substance screen runs first: ``is_substantive_reply`` is the
    predicate the IntentResolver adoption guard applies to minted intents
    (#721), so the two confirm lanes cannot drift apart (INV-26).
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message or is_substantive_reply(user_message):
        return None
    text = "".join(
        c for c in _normalize_reply(user_message) if c not in _EMOJI_MODIFIERS
    )
    for decoration in _POSITIVE_DECORATIONS:
        text = text.replace(decoration, " ")
    text = " ".join(text.split()).rstrip(".!, ")
    via: TerminalConfirmedVia
    if text in _EXPLICIT_CONFIRM_TOKENS:
        via = "explicit_token"
    elif text in _WEAK_CONFIRM_TOKENS:
        via = "weak_token"
    else:
        return None
    if _TARGET_SCOPED_TOKENS.get(text, to_state) != to_state:
        return None
    return via


def opens_with_consent_token(user_message: str) -> bool:
    """Whether ``user_message`` is consent-SHAPED: not substantive, and opening
    with a consent token on a word boundary.

    This is the rule the gate used to execute on, and it is NOT consent: a
    reply that opens with a token may go on to refuse ("ok, don't close it
    yet"). It marks the replies the gate answers itself with a re-ask rather
    than sending them down the escape lane, so the set of replies the gate
    consumes did not move when consent narrowed (#1783).
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message or is_substantive_reply(user_message):
        return False
    return _matches_gate_token(
        _normalize_reply(user_message),
        _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS,
    )


def _user_declines_transition(user_message: str) -> bool:
    """Check if user message declines a pending transition.

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
    return _matches_gate_token(msg, decline_patterns)


def pending_gate_verdict(
    user_message: str,
    to_state: Optional[str],
    *,
    intent_value: Optional[bool],
    typed: bool,
) -> tuple[PendingGateVerdict, Optional[TerminalConfirmedVia]]:
    """The pending-transition gate's answer to one user turn (#1783, ruling (a)).

    ``intent_value`` is the answer an intent carries: True for a
    ``confirmation`` with ``value=True`` or a ``status_transition`` to the
    pending target, False for a ``confirmation`` with ``value=False``, None
    when the turn carries neither. ``typed`` is True when the service MINTED
    that intent from typed text, so it is not a click.

    Returns the verdict and, for ``confirm``, how the user confirmed:

    * ``confirm`` — execute. Only a click (``"intent"``), or a bare consent
      token (``confirmation_token_class``) that no minted decline contradicts;
    * ``decline`` — an explicit decline: the Not-yet click, a typed decline
      token, or a minted decline on text that is not consent-shaped;
    * ``reask`` — show the proposal's buttons again, and record nothing: a
      consent-shaped reply that is not bare, a reply whose text and minted
      intent disagree, or a minted confirmation on text that is not bare;
    * ``not_an_answer`` — the reply answers neither way. The caller re-asks a
      short one and sends a substantive one down the escape lane.

    First match wins, so a minted intent never overrides the typed text.
    """
    if intent_value is not None and not typed:
        return ("confirm", "intent") if intent_value else ("decline", None)
    bare = confirmation_token_class(user_message, to_state)
    if bare is not None:
        return ("reask", None) if intent_value is False else ("confirm", bare)
    if opens_with_consent_token(user_message):
        return "reask", None
    if _user_declines_transition(user_message):
        return ("reask", None) if intent_value is True else ("decline", None)
    if intent_value is True:
        return "reask", None
    if intent_value is False:
        return "decline", None
    return "not_an_answer", None
