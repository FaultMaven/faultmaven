"""Whether a user turn confirms or declines a proposed stage transition, read from the same gate-token matcher stage_gates.py uses."""

import re
from typing import Optional

from faultmaven.core.investigation.terminal_transitions import (
    is_substantive_reply,
    normalize_reply,
)
from faultmaven.modules.case.contracts import TerminalConfirmedVia

from .stage_gates import (
    CLOSE_CONFIRMATION_PAYLOAD,
    _gate_token_match,
    _matches_gate_token,
)
from .terminal_replies import RESOLVE_CONFIRMATION_PAYLOAD

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
#: "ok thx"). Never consent on its own: a reply must OPEN with a token.
_CONSENT_FILLERS = ("please", "pls", "plz", "thanks", "thx", "ty", "thank you")

#: The closed vocabulary a consent may continue with after its opening token
#: (#1783): "yes, resolved", "yep, all good", "looks good to me", "yes
#: everything is back to normal". Deliberately without any negation ("not",
#: "no", "still", "again"), so "yes, the issue is not resolved" is not consent.
_AFFIRMATIVE_WORDS = frozenset(
    (
        "it is it's its resolved fixed solved done works worked working all good"
        " great fine perfect now the issue problem case this that close closed"
        " mark as go ahead and to me we are we're i think everything looks seems"
        " back normal too also indeed definitely of course sir mate team"
    ).split()
)

#: The words of every consent token and filler: all a refusal phrase may follow
#: for the reply to be a CERTAIN decline ("yes, don't close it yet").
_TOKEN_WORDS = frozenset(
    word
    for phrase in _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS + _CONSENT_FILLERS
    for word in phrase.split()
)

#: Every word a consent may contain.
_CONSENT_VOCABULARY = _TOKEN_WORDS | _AFFIRMATIVE_WORDS

#: Emoji, Slack shortcodes and emoticons that keep a consent a consent. They are
#: removed before the character and word checks, so "ok 👍", "ok :+1:" and
#: "ok (y)" are the bare token. Matched on the normalised (lowercased) text.
_POSITIVE_EMOJI = (
    "👍",
    "👌",
    "✅",
    "✔",
    "☑",
    "🙏",
    "🙂",
    "😊",
    "😀",
    "😃",
    "🎉",
    "💯",
    "🚀",
    "✨",
    "🙌",
    ":+1:",
    ":thumbsup:",
    ":ok_hand:",
    ":white_check_mark:",
    ":heavy_check_mark:",
    ":ballot_box_with_check:",
    ":pray:",
    ":slightly_smiling_face:",
    ":smile:",
    ":tada:",
    ":100:",
    ":rocket:",
    ":sparkles:",
    ":raised_hands:",
    ":)",
    ":-)",
    "=)",
    ":]",
    ":d",
    ":-d",
    "(y)",
    "<3",
)

#: Emoji, Slack shortcodes and emoticons that refuse or defer (#1783). Any of
#: them anywhere is a refusal signal and a certain decline: "ok 👎", "👎 ok",
#: "ok :wait:".
_NEGATIVE_EMOJI = (
    "👎",
    "❌",
    "🚫",
    "✋",
    "🛑",
    "⛔",
    "🙅",
    "✗",
    "✖",
    "⏳",
    "🙄",
    ":-1:",
    ":thumbsdown:",
    ":x:",
    ":no_entry:",
    ":no_entry_sign:",
    ":raised_hand:",
    ":stop_sign:",
    ":octagonal_sign:",
    ":no_good:",
    ":stop:",
    ":wait:",
    ":later:",
    ":(",
    ">:(",
    ":-/",
    ":-(",
)

#: The only characters besides letters and digits a consent may contain, once
#: its positive emoji are removed: separators, quotes, the emoji variation
#: selector and the skin-tone modifiers. A closed set, so "!ok", "~~ok~~",
#: "[ ] close it" and "ok ⏳" are not consent (#1783).
_CONSENT_SEPARATORS = (
    frozenset(" .,!;-—–\"'") | {"️"} | {chr(c) for c in range(0x1F3FB, 0x1F400)}
)

#: A refusal or a deferral: THE refusal list (#1783). Read anywhere in a reply,
#: word-bounded, on the normalised text before anything is removed. Bare "no"
#: is deliberately absent — "no problem" and "no worries" would read as
#: refusals — and a reply that merely contains it is re-asked instead.
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
    "dont",
    "nah",
    "negative",
    "hold up",
    "not so fast",
    "l8r",
    "w8",
)

#: Words that decline only when a reply OPENS with them ("no", "nope, it's fine
#: now"), whatever follows, a question included, as the decline list always
#: read. Not refusal phrases: "yes, no problem" is not a refusal.
_OPENING_ONLY_DECLINES = ("no", "nope", "not ready")

#: What a certain decline may open with: the opening-only words, and the
#: subset of ``_REFUSAL_PHRASES`` that opens a decline even when a question or
#: more text follows.
_DECLINE_OPENERS = _OPENING_ONLY_DECLINES + (
    "not yet",
    "wait",
    "cancel",
    "don't",
    "dont",
    "hold on",
    "stop",
    "nah",
    "negative",
)

_REFUSAL = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(p) for p in _REFUSAL_PHRASES) + r")(?!\w)"
)

#: A word, in any script: "нет" and "不要" are words the vocabulary does not
#: hold, not separators.
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")

#: The engine's own positive card payloads, one source: the builders' constants.
_POSITIVE_CARD_PAYLOADS = frozenset(
    normalize_reply(payload)
    for payload in (RESOLVE_CONFIRMATION_PAYLOAD, CLOSE_CONFIRMATION_PAYLOAD)
)


def _without_positive_emoji(msg: str) -> str:
    """``msg`` with each positive emoji, shortcode and emoticon replaced by a
    space, longest first."""
    for emoji in sorted(_POSITIVE_EMOJI, key=len, reverse=True):
        msg = msg.replace(emoji, " ")
    return msg


def _refusal_signal(msg: str) -> bool:
    """Whether the normalised ``msg`` carries a refusal phrase or a negative
    emoji anywhere (#1783). Read before any emoji or shortcode is removed."""
    return _REFUSAL.search(msg) is not None or any(e in msg for e in _NEGATIVE_EMOJI)


def _opening_token(msg: str) -> Optional[str]:
    """The consent token the normalised ``msg`` opens with, longest first."""
    match = _gate_token_match(msg, _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS)
    return match[0] if match else None


def confirmation_token_class(user_message: str) -> Optional[TerminalConfirmedVia]:
    """Which class of typed confirmation ``user_message`` is, or None for none.

    The typed-confirmation matcher itself (not DECIDE clicks): None means the
    gate does not read the message as consent, and anything else means it
    does. Its callers take consent from ``is not None``; there is no second
    predicate to drift from this one.

    DECIDE suggestion clicks now carry intent metadata and route
    through IntentType.CONFIRMATION deterministically. This matcher
    is a safety net for users who type instead of clicking.

    A match here executes a TERMINAL transition, so the reply must be a BARE
    consent. It is read as ``normalize_reply`` gives it, and it is consent
    only when all of these hold (#1783):

    * it is not substantive (the shared ``is_substantive_reply``: over 100
      characters, a question in any script, or a contrastive " but "), so a
      question is never consent (INV-26: the gate never consumes substantive
      input; the same predicate guards resolver-minted intents, #721);
    * it carries no refusal signal (``_refusal_signal``: a refusal phrase or a
      negative emoji, anywhere);
    * it OPENS with a consent token, the longest match, on a word boundary
      ("yesterday…" is not "yes"). A reply that opens with anything else —
      "❌ close it", "~~ok~~", "thanks, ok" — is not consent;
    * once its positive emoji, shortcodes and emoticons are removed, every
      other character is a letter, a digit or in ``_CONSENT_SEPARATORS``;
    * every word is in the closed vocabulary: the words of the tokens and
      fillers, and ``_AFFIRMATIVE_WORDS`` ("yes, resolved", "looks good to
      me"). A word outside it ("ok no", "ok нет", "sure — tomorrow") withholds
      consent, and the gate re-asks.

    The engine's own positive card payloads are consent too, typed or sent
    without their intent.

    The class names the opening token's set, explicit or weak (#1748), and
    whether the reply says more: ``*_token`` when its words are exactly the
    opening token's (emoji and punctuation aside: "ok 👍", "ok :+1:",
    "yes!"), ``*_prefixed`` when it carries more of the vocabulary ("ok go
    ahead", "yes please close it", a card payload).
    """
    msg = normalize_reply(user_message)
    if not msg or is_substantive_reply(msg) or _refusal_signal(msg):
        return None
    token = _opening_token(msg)
    if msg in _POSITIVE_CARD_PAYLOADS:
        return "weak_prefixed" if token in _WEAK_CONFIRM_TOKENS else "explicit_prefixed"
    if token is None:
        return None
    rest = _without_positive_emoji(msg)
    if any(not c.isalnum() and c not in _CONSENT_SEPARATORS for c in rest):
        return None
    words = _WORD.findall(rest)
    if not all(word in _CONSENT_VOCABULARY for word in words):
        return None
    bare = words == token.split()
    if token in _EXPLICIT_CONFIRM_TOKENS:
        return "explicit_token" if bare else "explicit_prefixed"
    return "weak_token" if bare else "weak_prefixed"


def _user_declines_transition(user_message: str) -> bool:
    """Whether ``user_message`` CERTAINLY declines a pending transition.

    Certain means one of (#1783):

    * it opens with a decline word (``_DECLINE_OPENERS``), whatever follows;
    * it carries a negative emoji (``_NEGATIVE_EMOJI``);
    * it is not substantive, and a refusal phrase appears in it with only the
      words of consent tokens and fillers before it ("ok, don't close it yet",
      "sure, do it later").

    Anything else is not a certain decline, even with a refusal word in it:
    "sure, I don't mind" and "yes, the errors don't come back" are
    ambiguous, and a question ("what happens if I cancel?") is the user
    deciding. Those are re-asked or withdrawn, never recorded as a refusal of
    the offer. A few consents are declined by this rule, and that is
    accepted: "yes, don't wait", "yes, cancel the investigation". A false
    decline withdraws a proposal the user can ask for again; a false consent
    closes a case irreversibly.

    Tokens match on word boundaries — "note db latency spiked" must not
    read as "no", nor "stopped the pod" as "stop" (the old bare
    ``startswith`` swallowed such evidence-bearing messages with a
    canned acknowledgment).
    """
    msg = normalize_reply(user_message)
    if not msg:
        return False
    if _matches_gate_token(msg, _DECLINE_OPENERS):
        return True
    if any(e in msg for e in _NEGATIVE_EMOJI):
        return True
    if is_substantive_reply(msg):
        return False
    refusal = _REFUSAL.search(msg)
    if refusal is None:
        return False
    before = _WORD.findall(_without_positive_emoji(msg[: refusal.start()]))
    return all(word in _TOKEN_WORDS for word in before)


def _minted_confirmation_conflicts(user_message: str) -> bool:
    """Whether a confirmation the resolver MINTED from ``user_message``
    conflicts with the text itself (#1783).

    The resolver's match is an inference, and the text outranks it when the
    text is a certain decline, carries a refusal signal, is substantive, or
    opens with a consent token without being consent ("ok no", "ok after
    lunch"). The gate then re-asks: it neither executes the inference nor
    declines on it. Text wholly outside the vocabulary ("that works", "oui
    non") is the classifier's reading and does not conflict.
    """
    msg = normalize_reply(user_message)
    if (
        _user_declines_transition(msg)
        or _refusal_signal(msg)
        or is_substantive_reply(msg)
    ):
        return True
    return _opening_token(msg) is not None and confirmation_token_class(msg) is None
