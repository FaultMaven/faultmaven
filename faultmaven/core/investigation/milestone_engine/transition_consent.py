"""Whether a user turn confirms or declines a proposed terminal transition.

Every ``pending_transition`` is a terminal proposal (RESOLVED or CLOSED), and a
terminal state has no outgoing edge. So the gate reads consent narrowly (#1783,
ruling (a), 2026-09-29): a proposal executes on its click, or on a typed reply
that is, as a whole, one consent token for the proposal's target. Any other
typed reply is re-asked, and a re-ask never records a refusal.

A click is consent only to the offer it names (#1812, ruling (a)): every
confirmation card carries its offer's key (``terminal_offer_key``,
``gate1_offer_key``), and a click whose key is not the standing offer's
executes nothing. A typed decline is as bare as a typed consent (#1813), and
Gate 1 reads a typed consent through the same bare test (#1794).
"""

import hashlib
import logging
from typing import Any, Literal, Optional

from faultmaven.modules.case.contracts import TerminalConfirmedVia

from .stage_gates import _matches_gate_token

logger = logging.getLogger(__name__)

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
#: selector and the five skin tones. Each is replaced by a space, like a
#: decoration and never by nothing, so one inside a word splits it (``o🏽k`` is
#: not ``ok``), while after an emoji (``✔️``, ``👍🏽``) the space is harmless.
_EMOJI_MODIFIERS = frozenset({"\ufe0f", *(chr(c) for c in range(0x1F3FB, 0x1F400))})

#: The typed decline tokens. A typed reply declines only when the WHOLE of it
#: is one of them (#1813, the bare-decline mirror of ``confirmation_token_class``).
_DECLINE_TOKENS = (
    "no",
    "nope",
    "not yet",
    "wait",
    "cancel",
    "don't",
    "not ready",
    "hold on",
    "stop",
)

PendingGateVerdict = Literal["confirm", "decline", "reask", "not_an_answer"]

#: Why a click was refused (#1812): it named no offer, or not the standing one.
OfferRefusal = Literal["stale", "untargeted"]

#: What a typed confirmation must look like, said wherever a confirmation is
#: asked for (#1814, ruling (b)): the terminal re-ask and Gate 1's
#: presentation. No quote marks, because the grammar rejects a quoted token.
TYPED_CONFIRMATION_LINE = "To confirm, click **Yes** or reply with the single word yes."

#: The prefix that marks a Gate 1 offer key, so it can never equal a terminal
#: offer's key (a ``proposed_at`` timestamp).
_GATE1_KEY_PREFIX = "gate1:"


def terminal_offer_key(pending: Optional[dict[str, Any]]) -> Optional[str]:
    """The key of the terminal offer ``pending`` presents, or None when none stands.

    ``proposed_at``: ``propose_transition`` is the only writer of a fresh
    offer, and stamps it on every proposal. Every step that changes what is
    offered re-proposes rather than editing the standing dict: the INV-37
    CLOSE→RESOLVE pivot, and a ``needs_info`` RESOLVED offer becoming ready. So
    a withdrawn or superseded offer's key never comes back. (The fields set in
    place, ``justifying_signature`` and a first-pass ``needs_info``, are set
    right after the proposal they belong to.)
    """
    return (pending or {}).get("proposed_at") or None


def gate1_offer_key(statement: Optional[str]) -> str:
    """The key of the Gate 1 offer that presents ``statement``.

    The offer IS the wording shown, so a revision is a new offer: a digest of
    the stripped statement. It is 22 characters, well clear of Slack's
    button-value limit. A statement revised to identical text keeps its key.
    """
    digest = hashlib.sha256((statement or "").strip().encode()).hexdigest()[:16]
    return f"{_GATE1_KEY_PREFIX}{digest}"


def offer_intent_fields(
    proposal_id: Optional[str], *, case: Any, gate: str
) -> dict[str, str]:
    """The intent fields that name the offer a confirmation card presents:
    ``{"proposal_id": proposal_id}``, spliced into both cards of a pair.

    Every builder is reached with its offer standing. If one ever is not, the
    pair ships with no key and this logs at ERROR: a click on it is refused as
    untargeted, which is the safe direction, never a 500 on a turn.
    """
    if not proposal_id:
        logger.error(
            "confirmation_pair_without_offer",
            extra={"case_id": getattr(case, "case_id", None), "gate": gate},
        )
        return {}
    return {"proposal_id": proposal_id}


def offer_click_refusal(
    intent_data: Optional[dict[str, Any]], standing_key: Optional[str]
) -> Optional[OfferRefusal]:
    """Why a click may not answer the offer whose key is ``standing_key``, or
    None when it names that offer.

    ``untargeted``: the click names no offer (a card rendered before keys
    shipped, or a hand-built intent). ``stale``: it names another one. With no
    offer standing (``standing_key`` None) every click is refused.
    """
    clicked = (intent_data or {}).get("proposal_id")
    if not clicked:
        return "untargeted"
    if clicked != standing_key:
        return "stale"
    return None


def _undecorated(user_message: str) -> str:
    """``user_message`` normalised, with every emoji modifier and every listed
    positive decoration replaced by a space, and whitespace collapsed.

    A space, never nothing: one inside a word splits it and cannot reassemble a
    token (``clo(y)se it``, ``o👍k``, ``o🏽k``). The one treatment every
    grammar here reads: ``confirmation_token_class``, ``_consent_prefix`` and
    ``_user_declines_transition``.
    """
    text = "".join(
        " " if c in _EMOJI_MODIFIERS else c for c in _normalize_reply(user_message)
    )
    for decoration in _POSITIVE_DECORATIONS:
        text = text.replace(decoration, " ")
    return " ".join(text.split())


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
    token (#1783, ruling (a)). Exactly, once ``_normalize_reply`` has stripped
    and lowercased it and made curly single quotes straight, a BARE reply is
    the token's words, with:

    * any whitespace, any listed positive decoration
      (``_POSITIVE_DECORATIONS``) and any emoji modifier
      (``_EMOJI_MODIFIERS``) before, between or after them;
    * and only ``.``, ``!`` and ``,`` trailing, after the last word.

    So ``yes``, ``ok!``, ``lgtm 👍``, ``👍🏽 ok``, ``✔️ yes``, ``ok :+1:``,
    ``yes :-)`` and ``go 👍 ahead`` are consent. Anything more is not, whatever
    it says: ``ok go ahead``, ``ok, don't close it yet``, ``ok 👎``, ``❌ close
    it``, ``ok?``. Such a reply is re-asked, never executed
    (``pending_gate_verdict``).

    A decoration or a modifier is replaced by a space, never by nothing, so
    one inside a word splits the word and cannot reassemble a token
    (``clo(y)se it``, ``o👍k``, ``o🏽k``, ``clo️se it``). A target-scoped token
    (``_TARGET_SCOPED_TOKENS``) consents only to its own target.

    The shared substance screen runs first: ``is_substantive_reply`` is the
    predicate the IntentResolver adoption guard applies to minted intents
    (#721), so the two confirm lanes cannot drift apart (INV-26).
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message or is_substantive_reply(user_message):
        return None
    text = _undecorated(user_message).rstrip(".!, ")
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


def gate1_bare_consent(user_message: str) -> bool:
    """Whether ``user_message`` is, as a whole, one consent token to Gate 1
    (the problem-statement confirmation) (#1794, ruling (a)).

    ``confirmation_token_class`` with no target, so the target-scoped tokens
    ("close it", "mark as resolved") are not consent here. Gate 1 commits only
    on its click or on a turn whose typed text passes this: the LLM's
    ``user_confirmed_investigation`` and a resolver-minted confirmation count
    only when it holds. ``Yes, that's correct. Let's investigate.`` (the
    card's own payload) is not bare, and a click is never screened by its text.
    """
    return confirmation_token_class(user_message, None) is not None


def _consent_prefix(user_message: str) -> bool:
    """Whether ``user_message`` OPENS with a consent token on a word boundary,
    after the same treatment ``confirmation_token_class`` gives it (#1808).

    Modifiers and positive decorations become spaces first, so ``👍 go ahead
    and close it…`` opens with ``go ahead``. Says nothing about length or what
    follows: ``ok but we need to wait for the weekend soak first`` opens with
    consent. One grammar, read by ``opens_with_consent_token`` and by the
    engine's escape lane, which records no refusal for a reply that opens with
    consent.
    """
    if not user_message:
        return False
    return _matches_gate_token(
        _undecorated(user_message),
        _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS,
    )


def opens_with_consent_token(user_message: str) -> bool:
    """Whether ``user_message`` is consent-SHAPED: not substantive, and opening
    with a consent token (``_consent_prefix``).

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
    return _consent_prefix(user_message)


def _user_declines_transition(user_message: str) -> bool:
    """Whether ``user_message`` is a BARE typed decline (#1813, ruling (a)).

    The mirror of ``confirmation_token_class``: not substantive, and the
    whole reply is one decline token (``_DECLINE_TOKENS``), with any
    whitespace, listed positive decoration and emoji modifier around it and
    only ``.``, ``!`` and ``,`` trailing. So ``no``, ``Not yet.``, ``nope!``
    and ``no 👍`` decline, and ``no problem, go ahead``, ``no worries``, ``no,
    not yet``, ``nope 👎`` and ``no?`` do not. Nor, as before, does a word
    that only shares a token's prefix (``note db latency spiked``, ``stopped
    the pod``). A multi-token refusal is re-asked each time it is sent (a
    re-ask has no cap); the Not-yet click declines in one step.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        is_substantive_reply,
    )

    if not user_message or is_substantive_reply(user_message):
        return False
    return _undecorated(user_message).rstrip(".!, ") in _DECLINE_TOKENS


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
    * ``decline`` — an explicit decline: the Not-yet click, a BARE typed
      decline token (``_user_declines_transition``), or a minted decline on
      text that is neither consent-shaped nor a question;
    * ``reask`` — show the proposal's buttons again, and record nothing: a
      consent-shaped reply that is not bare, a reply whose text and minted
      intent disagree, or a minted confirmation on text that is not bare;
    * ``not_an_answer`` — the reply answers neither way, a minted decline on
      text carrying ``?`` included (#1813): the escape lane's question rule
      then withdraws the proposal and records nothing. The caller re-asks a
      short one and sends a substantive one down the escape lane.

    First match wins, so a minted intent never overrides the typed text. A
    click's offer key is checked by the engine before this runs (#1812).
    """
    if intent_value is not None and not typed:
        return ("confirm", "intent") if intent_value else ("decline", None)
    bare = confirmation_token_class(user_message, to_state)
    if bare is not None:
        return ("reask", None) if intent_value is False else ("confirm", bare)
    # A minted decline on a question is not a refusal (#1813): once, here,
    # ahead of both decline returns below, so "no?" (not bare) lands here too.
    if typed and intent_value is False and "?" in (user_message or ""):
        return "not_an_answer", None
    if opens_with_consent_token(user_message):
        return "reask", None
    if _user_declines_transition(user_message):
        return ("reask", None) if intent_value is True else ("decline", None)
    if intent_value is True:
        return "reask", None
    if intent_value is False:
        return "decline", None
    return "not_an_answer", None
