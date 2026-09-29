"""#1748 - how a terminal transition was confirmed, and who kept talking after.

#723 defers two confirmation-model notes until either is observed. The first (a
terminal transition confirmed by a bare weak token that proved spurious) needs
the confirming channel recorded and the next message counted. The second (a
dropdown INQUIRY -> INVESTIGATING that did not transition) cannot occur:
``USER_SELECTABLE_ACTIONS`` offers only CLOSED from INQUIRY and
``earned_edge_refusal`` refuses any other ``status_transition`` at engine entry.

The channel is written onto the confirming turn's record, and BOTH counters are
read from the saved records by the investigation service after its save
(``turn_messages._save_and_emit_turn``). So everything that decides a count is
driven here through ``InvestigationService.process_turn`` — the path a click or
a typed reply actually takes — with only the LLM and the database doubled.

#1783 made consent the whole reply: a reply that opens with a token and then
refuses or defers ("ok, don't close it yet") no longer confirms. Its corpus
below is the pin for what the gate reads as consent, and the same harness
drives the gate on those replies.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import faultmaven.core.investigation.milestone_engine.transition_consent as transition_consent
import faultmaven.core.investigation.terminal_transitions as tt
import faultmaven.modules.agent.domain.services.investigation_service.turn_messages as turn_messages
import faultmaven.modules.case.domain.models.turn as turn_model
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    CLOSE_CONFIRMATION_PAYLOAD,
    _close_confirmation_suggestions,
    _gate_token_match,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
    RESOLVE_CONFIRMATION_PAYLOAD,
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _DECLINE_OPENERS,
    _EXPLICIT_CONFIRM_TOKENS,
    _OPENING_ONLY_DECLINES,
    _REFUSAL_PHRASES,
    _WEAK_CONFIRM_TOKENS,
    _minted_confirmation_conflicts,
    _user_declines_transition,
    confirmation_token_class,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.terminal_transitions import (
    is_substantive_reply,
    propose_transition,
)
from faultmaven.exceptions import ServiceException
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.exceptions import StaleCaseException

pytestmark = pytest.mark.unit

#: The label table. A consent OPENS with a token, and its set names the class
#: (#1748). ``*_token`` means the reply's words are exactly that token's, once
#: its positive emoji, shortcodes and emoticons are removed, so "ok 👍", "ok :+1:"
#: and "ok (y)" are bare; ``*_prefixed`` means a consent that carries more of
#: the vocabulary. A reply that is not consent has no label (#1783): see the
#: corpus below.
LABEL_TABLE = {
    "weak_token": [
        "ok",
        "ok!",
        "ok :)",
        "ok 👍",
        "ok =)",
        "lgtm",
        "sure.",
        "  Okay  ",
        # Slack's wire form of an emoji labels as the Unicode emoji does.
        "ok :+1:",
        "lgtm :rocket:",
        "OK :THUMBSUP:",
        # No space: the gate still reads "ok" (a word boundary before ":"), and
        # what follows is only a shortcode.
        "ok:+1:",
        # Positive emoticons, including those written with a letter or digit.
        "ok :D",
        "ok (y)",
        "ok <3",
        "ok 👍🏽",
    ],
    "weak_prefixed": [
        "ok go ahead",
        "ok ok",
        "sure, close it",
        "ok yes",
        "lgtm, confirmed",
        "ok thanks",
        "ok :+1: go ahead",
        "looks good to me",
    ],
    "explicit_token": [
        "yes",
        "yes 👍",
        "yes :+1:",
        "go ahead",
        "that's right",
        "that\u2019s right",
        "yes!",
        "yes :white_check_mark:",
        "yes ✅",
    ],
    "explicit_prefixed": [
        "yes please close it",
        "yes please",
        "yes, mark as resolved",
        "yes ok",
        "please do it",
        # The engine's own card payloads, typed or sent without their intent.
        RESOLVE_CONFIRMATION_PAYLOAD,
        CLOSE_CONFIRMATION_PAYLOAD,
    ],
}

#: What "Yes, mark as resolved" sends when clicked.
CONFIRM_CARD = _resolution_confirmation_suggestions()[0]

# #1783's corpus, every row pinned. It is the owning agent's probe of revision 1
# (the ruling on PR #1793), built negatives first from replies that share the
# tokens: the review's and the defeat pass's misses, apostrophe lookalikes,
# zero-width and fullwidth text, negative emoji, and other languages. ``main``
# confirmed every reply that OPENED with a token.

#: Consents, each with the class it is counted under.
MUST_CONFIRM = {
    "ok": "weak_token",
    "ok!": "weak_token",
    "OK.": "weak_token",
    "ok 👍": "weak_token",
    "ok :+1:": "weak_token",
    "okay": "weak_token",
    "sure": "weak_token",
    "lgtm": "weak_token",
    "sounds good": "weak_token",
    "looks good": "weak_token",
    "yes": "explicit_token",
    "yes!": "explicit_token",
    "yes please": "explicit_prefixed",
    "yes, please close it": "explicit_prefixed",
    "yes please close it": "explicit_prefixed",
    "ok go ahead": "weak_prefixed",
    "sure, close it": "weak_prefixed",
    "lgtm, confirmed": "weak_prefixed",
    "that's right": "explicit_token",
    "go ahead": "explicit_token",
    "please do": "explicit_token",
    "ok thanks": "weak_prefixed",
    "yes thank you": "explicit_prefixed",
    "ok ok": "weak_prefixed",
    "yep": "explicit_token",
    "confirmed": "explicit_token",
    "mark it as resolved": "explicit_token",
    "yes, mark as resolved": "explicit_prefixed",
    "absolutely": "explicit_token",
    "go for it": "explicit_token",
    "that\u2019s right": "explicit_token",
    "that\u2019s correct": "explicit_token",
    "ok 👍🏽": "weak_token",
    "yes ✅": "explicit_token",
    "ok :)": "weak_token",
    "ok :D": "weak_token",
    "ok (y)": "weak_token",
    "Yes, the issue is resolved. Please mark this case as resolved.": "explicit_prefixed",
    "Yes, close this case without resolution.": "explicit_prefixed",
    "looks good to me": "weak_prefixed",
    "yes it worked": "explicit_prefixed",
    "ok thx": "weak_prefixed",
    "yes pls": "explicit_prefixed",
    "yes, resolved": "explicit_prefixed",
    "yes, close": "explicit_prefixed",
    "yes it is": "explicit_prefixed",
    "yep, all good": "explicit_prefixed",
    "ok, go ahead and close it": "weak_prefixed",
    "please do it": "explicit_prefixed",
    "yes please do it": "explicit_prefixed",
    "sure, please do it": "weak_prefixed",
    "yes, it's fixed": "explicit_prefixed",
    "ok, all good now": "weak_prefixed",
    "yes everything is back to normal": "explicit_prefixed",
}

#: Certain declines: the reply opens with a decline word, carries a negative
#: emoji, or puts a refusal phrase after consent words alone.
DECLINED = [
    "ok ✓ later",
    "ok, don't close it yet",
    "ok, don\u2019t close it yet",
    "sure, do it later",
    "ok. mark as resolved later",
    "yes, don't close it yet",
    "do it later",
    "confirm later",
    "ok, wait",
    "ok not now",
    "ok hold on",
    "ok stop",
    "ok cancel that",
    "ok never mind",
    "❌ close it",
    "🚫 close it",
    "✗ close it",
    "⛔ resolve it",
    "👎 ok",
    "🛑 ok",
    ":x: close it",
    ":-1: ok",
    "thanks, ok 👎",
    "ok 👎",
    "yes ❌",
    "ok 🚫",
    "ok ✋",
    "ok ⏳",
    "ok 🙅",
    "ok :-1:",
    "ok :thumbsdown:",
    "yes :x:",
    "ok :no_entry:",
    "ok :raised_hand:",
    "ok :no_good:",
    "ok :stop:",
    "ok :wait:",
    "yes :later:",
    "ok :(",
    "ok >:(",
    "ok :-/",
    "yeah sure 🙄",
    "ok ⓝⓞⓣ ⓨⓔⓣ",
    "do\u200bn't close it",
    "ok, not  yet",
    "ok, don\u02bct close it yet",
    "ok dont close it yet",
]

#: Not consent and not a certain decline: the gate re-asks once, then withdraws.
RE_ASKED = [
    "ok нет",
    "ok 不要",
    "ok nein",
    "yes 👍 no",
    "ok no",
    "ok, no",
    "okay i'll confirm with the team",
    "lgtm, not approved yet",
    "yes no",
    "yes... actually no",
    "okay, let me double check first",
    "ok after lunch",
    "sure — tomorrow",
    "!ok",
    "¬ok",
    "!= ok",
    "!close it",
    "~ok",
    "~yes~",
    "~~ok~~",
    "~~close it~~",
    "[ ] close it",
    "- [ ] mark as resolved",
    "close it？",
    "ok？",
    "resolve it؟",
    "proceed‽",
    "confirm⁇",
    "ok, don\u00b4t close it yet",
    "yes, the issue is not resolved",
    "yes it is still broken",
    "ok it failed again",
    "sure, the errors are back",
    "ok, that's not it",
    # #1783 round 1 and #1748 rows, each still pinned.
    "yes but not yet",
    "sure, but first check the logs",
    "yesterday it was fine",
    "sure thing",
    "yes, no problem",
    "ok no worries",
    "sure, whenever",
    "yes, it's resolved, the error is gone",
    "yes :P",
    "ok XD",
    "hmm",
    "note the db latency spiked",
    # A filler or a symbol before the token: re-asked, as on ``main``.
    "thanks, ok",
    "please, yes",
    ":+1: ok",
    "👍 ok",
    # Held by the closed character set alone: no refusal signal, no unknown word.
    "ok 😠",
    "ok ⌛",
    "ok 💔",
]

#: A refusal word inside what may be a consent: ambiguous, so re-asked, and
#: never recorded as a refusal of the offer.
AMBIGUOUS_CONSENTS = [
    "yes, the errors don't come back",
    "sure, I don't mind",
    "yes, let's stop here",
    "go ahead, no need to wait",
]

#: Consents the certain-decline rule declines: see
#: ``test_the_accepted_false_declines``.
ACCEPTED_FALSE_DECLINES = [
    "yes, cancel the investigation",
    "go ahead, dont hold up the release",
    "yes, don't wait",
]

#: A question is the user deciding: never consent, never a certain decline.
QUESTIONS = [
    "What happens to the runbook if I cancel this?",
    "Should I wait for the change window before closing?",
    "Can we do this later, after the deploy?",
]

#: Typed text a resolver-minted confirmation conflicts with: re-asked, never
#: executed and never declined.
MINTED_CONFLICTS = [
    "ok ñot yet",
    "yes да нет",
    "no",
    "nope",
    "not ready",
    "no thanks",
    "nope, it's fine now",
    "no, not ready",
    "ok, not ready",
    "dont",
    "ok, dont close it yet",
    "nah",
    "negative",
    "hold up",
    "not so fast",
    "l8r",
    "ok nein",
    "ok, pas encore",
    "ok нет",
    "ok 不要",
    "not yet",
    "ok, hold on",
    "do\u200bn't",
    "don\u02bct",
    "don\u2032t",
    "don\uff07t",
    "ｄｏｎ't",
    "ok no",
    "ok after lunch",
    "lgtm, not approved yet",
    "yes... actually no",
    "ok 👎",
    "ok, don't close it yet",
    "yes, the errors don't come back",
    # A question in a non-ASCII mark: text outside the vocabulary would
    # otherwise be the classifier's reading.
    "that works\u061f",
    "that works\u203d",
]

#: Typed text a resolver-minted confirmation executes on.
MINTED_AGREES = [
    "that works",
    "Yes, the issue is resolved. Please mark this case as resolved.",
    "yes, mark as resolved",
    "sounds great, go",
    "ok",
    "yes please",
    "yes it worked",
    "perfect, thank you",
]

#: The residual: text wholly outside the vocabulary is the classifier's
#: reading, as "that works" is. See ``test_the_minted_residual``.
MINTED_RESIDUAL = [
    "oui non",
    "sí, pero no",
    "ja, aber noch nicht",
]

_ALL_TOKENS = _EXPLICIT_CONFIRM_TOKENS + _WEAK_CONFIRM_TOKENS


class TestTheClassifier:
    @pytest.mark.parametrize(
        "label, message",
        [(label, m) for label, rows in LABEL_TABLE.items() for m in rows],
    )
    def test_the_label_table(self, label, message):
        assert confirmation_token_class(message) == label

    @pytest.mark.parametrize("token", _WEAK_CONFIRM_TOKENS)
    def test_each_weak_token_alone_is_weak(self, token):
        assert confirmation_token_class(token) == "weak_token"

    @pytest.mark.parametrize("token", _EXPLICIT_CONFIRM_TOKENS)
    def test_each_explicit_token_alone_is_explicit(self, token):
        assert confirmation_token_class(token) == "explicit_token"

    @pytest.mark.parametrize("message", ["that works", "", "ok_", "yesterday"])
    def test_what_the_gate_does_not_read_as_consent_is_none(self, message):
        assert confirmation_token_class(message) is None

    def test_a_substantive_reply_is_not_a_confirmation(self):
        assert confirmation_token_class("ok but what is the root cause?") is None

    def test_a_token_later_in_the_reply_does_not_confirm(self):
        assert confirmation_token_class("well, yes") is None

    def test_no_two_tokens_can_both_open_one_message(self):
        """Two tokens can both match at a message's start only if one matches
        at the start of the other, so checking the tokens against each other
        covers every message. None does today: the class never depends on the
        longest-match rule. A token added that overlaps another fails here, and
        whoever adds it decides the class knowingly."""
        overlapping = [
            (a, b)
            for a in _ALL_TOKENS
            for b in _ALL_TOKENS
            if a != b and _gate_token_match(a, (b,)) is not None
        ]
        assert overlapping == []

    def test_the_longest_opening_token_wins(self):
        assert _gate_token_match("go ahead now", ("go", "go ahead")) == (
            "go ahead",
            8,
        )
        assert _gate_token_match("go ahead now", ("go ahead", "go")) == (
            "go ahead",
            8,
        )

    def test_the_match_keeps_the_word_boundary(self):
        assert _gate_token_match("ok_", ("ok",)) is None
        assert _gate_token_match("okay", ("ok",)) is None

    def test_the_weak_set_is_exactly_723s_note_1_list(self):
        assert _WEAK_CONFIRM_TOKENS == (
            "ok",
            "okay",
            "sure",
            "sounds good",
            "looks good",
            "lgtm",
        )

    def test_the_explicit_set_is_pinned(self):
        assert _EXPLICIT_CONFIRM_TOKENS == (
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


def _typed_verdict(message: str) -> str:
    """What the engine's gate does with a typed reply and no intent, in its
    order: consent executes, a certain decline declines, anything else is
    re-asked (then withdrawn)."""
    if confirmation_token_class(message) is not None:
        return "confirm"
    if _user_declines_transition(message):
        return "decline"
    return "re-ask"


class TestARefusalNeverConfirms:
    """#1783: a typed reply confirms only when all of it is consent, and a
    confirmation the resolver mints never outranks the text it came from."""

    @pytest.mark.parametrize("message, label", list(MUST_CONFIRM.items()))
    def test_every_consent_confirms_with_its_class(self, message, label):
        assert confirmation_token_class(message) == label

    @pytest.mark.parametrize("message", DECLINED)
    def test_a_refusal_declines(self, message):
        assert _typed_verdict(message) == "decline"

    @pytest.mark.parametrize("message", RE_ASKED + AMBIGUOUS_CONSENTS)
    def test_what_is_neither_consent_nor_a_certain_decline_is_re_asked(self, message):
        """Never executed, and never recorded as a refusal of the offer:
        "sure, I don't mind" carries a refusal word and may be consent."""
        assert _typed_verdict(message) == "re-ask"

    @pytest.mark.parametrize("message", ACCEPTED_FALSE_DECLINES)
    def test_the_accepted_false_declines(self, message):
        """Each is a consent with a refusal phrase preceded by consent words
        alone, which is the shape of "ok, don't close it yet" — the rule cannot
        tell them apart without parsing negation. Accepted: a false decline
        withdraws a proposal the user can ask for again ("close the case"),
        while a false consent closes a case irreversibly."""
        assert _typed_verdict(message) == "decline"

    @pytest.mark.parametrize("message", QUESTIONS)
    def test_a_question_is_never_a_certain_decline(self, message):
        assert _typed_verdict(message) == "re-ask"

    @pytest.mark.parametrize("message", MINTED_CONFLICTS)
    def test_a_minted_confirmation_the_text_conflicts_with_is_re_asked(self, message):
        assert _minted_confirmation_conflicts(message)

    @pytest.mark.parametrize("message", MINTED_AGREES)
    def test_a_minted_confirmation_the_text_agrees_with_executes(self, message):
        assert not _minted_confirmation_conflicts(message)

    @pytest.mark.parametrize("message", MINTED_RESIDUAL)
    def test_the_minted_residual(self, message):
        """The stated residual (PR #1793): a reply wholly outside the
        vocabulary is the classifier's reading, as "that works" is, so a
        confirmation minted from it executes. Pinned so that closing it is a
        visible change."""
        assert not _minted_confirmation_conflicts(message)

    @pytest.mark.parametrize(
        "message",
        [
            "ok\uff1f",
            "resolve it\u061f",
            "proceed\u203d",
            "ok\u2047",
            "ok\u2048",
            "ok\u2049",
        ],
    )
    def test_a_question_mark_in_any_script_is_substantive(self, message):
        """INV-26 reads the mark after NFKC, which folds the fullwidth one."""
        assert is_substantive_reply(message)

    def test_the_refusal_list_is_one_list(self):
        """The decline openers are the opening-only words plus a subset of THE
        refusal list: there is no second list to disagree with it."""
        assert set(_DECLINE_OPENERS) - set(_OPENING_ONLY_DECLINES) <= set(
            _REFUSAL_PHRASES
        )
        assert not set(_OPENING_ONLY_DECLINES) & set(_REFUSAL_PHRASES)

    def test_the_card_payloads_are_the_builders_own(self):
        """One source: the matcher reads the constants the card builders send."""
        assert CONFIRM_CARD["payload"] == RESOLVE_CONFIRMATION_PAYLOAD
        assert _close_confirmation_suggestions()[0]["payload"] == (
            CLOSE_CONFIRMATION_PAYLOAD
        )
        assert _close_confirmation_suggestions()[0]["intent"] == {
            "type": "confirmation",
            "confirmation_value": True,
        }

    def test_a_token_that_carries_a_refusal_still_never_confirms(self, monkeypatch):
        """The refusal veto holds on its own, not only because no token or
        vocabulary word today is a refusal: one added that is still refuses."""
        monkeypatch.setattr(
            transition_consent,
            "_EXPLICIT_CONFIRM_TOKENS",
            (*_EXPLICIT_CONFIRM_TOKENS, "close it later"),
        )
        monkeypatch.setattr(
            transition_consent,
            "_CONSENT_VOCABULARY",
            transition_consent._CONSENT_VOCABULARY | {"later"},
        )
        assert confirmation_token_class("close it later") is None
        assert confirmation_token_class("close it") == "explicit_token"


@pytest.fixture
def fresh_warnings(monkeypatch):
    """The warn-once memory is per process; each test starts with none."""
    monkeypatch.setattr(turn_model, "_WARNED_UNKNOWN_CHANNELS", set())


def _record(via):
    return TurnProgress(
        turn_number=3,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        terminal_confirmed_via=via,
    )


class TestAStoredChannelNeverBreaksALoad:
    """The loaders rebuild every record with ``TurnProgress(**t)``, so a value
    the ``Literal`` rejected would make the whole case unloadable (review F8)."""

    def test_an_unknown_stored_channel_loads_as_none_and_says_so(
        self, caplog, fresh_warnings
    ):
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            record = _record("bare_weak")
        assert record.terminal_confirmed_via is None
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "'bare_weak'" in caplog.records[0].getMessage()

    def test_the_same_stale_value_warns_once(self, caplog, fresh_warnings):
        """Review F11: a stale record is re-read on every load of its case."""
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            _record("bare_weak")
            _record("bare_weak")
            _record("retired_channel")
        assert [r.getMessage().split()[1] for r in caplog.records] == [
            "'bare_weak'",
            "'retired_channel'",
        ]

    @pytest.mark.parametrize("via", [*get_args(TerminalConfirmedVia), None])
    def test_every_known_channel_survives_silently(self, via, caplog, fresh_warnings):
        with caplog.at_level(logging.WARNING, logger=turn_model.__name__):
            record = _record(via)
        assert record.terminal_confirmed_via == via
        assert not caplog.records


# ---------------------------------------------------------------------------
# Driven through InvestigationService.process_turn
# ---------------------------------------------------------------------------

CASE_ID = "case_1748a1748a17"
USER_ID = "user_test"


def _investigating_case(pending: str = "resolved") -> Case:
    case = Case(
        case_id=CASE_ID,
        title="Terminal confirmation counters",
        state=CaseState.INQUIRY,
        user_id=USER_ID,
        enterprise_id="org_test",
        description="pool exhaustion",
        problem_verification=ProblemVerification(
            symptom_statement="pool exhaustion",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "pool exhaustion"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    propose_transition(case, to_state=pending, summary="Resolve it?")
    return case


class _Store:
    """The repository as a database behaves: a save stores a SNAPSHOT and a get
    returns a fresh copy. A turn whose save fails therefore leaves nothing for
    its retry to find, which is what makes the retry below a real one."""

    def __init__(self, case: Case) -> None:
        self._rows = {case.case_id: case.model_copy(deep=True)}
        self._failures: list[tuple] = []

    def fail_next_save(self, exc: Exception, when=lambda case: True) -> None:
        """Raise ``exc`` on the next save for which ``when(case)`` holds."""
        self._failures.append((when, exc))

    async def get(self, case_id: str):
        row = self._rows.get(case_id)
        return row.model_copy(deep=True) if row is not None else None

    async def save(self, case: Case) -> Case:
        for armed in list(self._failures):
            when, exc = armed
            if when(case):
                self._failures.remove(armed)
                raise exc
        self._rows[case.case_id] = case.model_copy(deep=True)
        return case

    def row(self) -> Case:
        return self._rows[CASE_ID]


def _service(store: _Store) -> InvestigationService:
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        side_effect=AssertionError("no route under test reaches the LLM")
    )
    return InvestigationService(engine, store)


async def _turn(svc: InvestigationService, query: str, intent: QueryIntent = None):
    return await svc.process_turn(
        case_id=CASE_ID,
        user_id=USER_ID,
        payload=TurnPayload(query=query, intent=intent),
    )


def _click_confirm() -> QueryIntent:
    return QueryIntent(**CONFIRM_CARD["intent"])


@pytest.fixture
def counters():
    with (
        patch.object(turn_messages, "terminal_confirmation_total") as confirmation,
        patch.object(turn_messages, "terminal_followup_total") as followup,
    ):
        yield confirmation, followup


def _offer_the_resolution_cards(store: _Store) -> None:
    """Put the confirmation pair on offer, as the proposing turn stored it."""
    row = store.row()
    row.last_suggestions = [
        {**card, "offered_turn": row.current_turn}
        for card in _resolution_confirmation_suggestions()
    ]


def _spy_on_the_engine(svc: InvestigationService) -> AsyncMock:
    spy = AsyncMock(wraps=svc.engine.process_turn)
    svc.engine.process_turn = spy
    return spy


class TestTheConfirmationCounter:
    async def test_a_typed_ok_counts_as_weak(self, counters):
        confirmation, followup = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), "ok")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        confirmation.labels.return_value.inc.assert_called_once()
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        followup.labels.assert_not_called()  # the confirming turn itself

    @pytest.mark.parametrize("message", ["yes", "go ahead"])
    async def test_a_typed_reply_opening_with_an_explicit_token_counts_as_explicit(
        self, counters, message
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), message)

        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
        )

    # A consent of more than one word. A refusal after the token confirms
    # nothing at all (#1783): TestTheGateOnARefusal.
    @pytest.mark.parametrize(
        "message", ["ok go ahead", "sure, close it", "ok yes", "lgtm, confirmed"]
    )
    async def test_a_weak_opener_that_says_more_counts_as_weak_prefixed(
        self, counters, message
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), message)

        confirmation.labels.assert_called_once_with(
            via="weak_prefixed", to_state="resolved"
        )

    async def test_an_explicit_opener_that_says_more_counts_as_explicit_prefixed(
        self, counters
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())

        await _turn(_service(store), "yes please close it")

        confirmation.labels.assert_called_once_with(
            via="explicit_prefixed", to_state="resolved"
        )

    async def test_a_confirming_turn_whose_final_save_fails_is_not_counted(
        self, counters
    ):
        """Review F12, pinning what the metrics doc says. The gate commits the
        transition at the engine's own saves; the service's final save then
        conflicts. The confirmation is never counted — and stays lost: the
        user's retry resubmits the same "ok", which is not a follow-up either
        (review F1 of e98382937), although the engine already saved the
        confirming turn's channel record."""
        confirmation, followup = counters
        store = _Store(_investigating_case())
        svc = _service(store)
        store.fail_next_save(
            StaleCaseException(CASE_ID, 3, 4),
            # The service's final save is the only one made after the agent's
            # reply row is appended.
            when=lambda case: case.messages[-1]["role"] == "assistant",
        )

        with pytest.raises(StaleCaseException):
            await _turn(svc, "ok")

        assert store.row().state == CaseState.RESOLVED
        assert store.row().turn_history[-1].terminal_confirmed_via == "weak_token"
        confirmation.labels.assert_not_called()
        followup.labels.assert_not_called()

        # The retry finds a RESOLVED case: its "ok" is a terminal Q&A turn.
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )
        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, "ok")

        answered.assert_awaited_once()
        confirmation.labels.assert_not_called()
        followup.labels.assert_not_called()

    async def test_an_ok_the_resolver_minted_into_an_intent_is_still_weak(
        self, counters
    ):
        """Review F2: a typed "ok" the resolver turns into a confirmation
        intent is a typed "ok", not a click."""
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        engine = _spy_on_the_engine(svc)

        await _turn(svc, "ok")

        # The mint happened and reached the gate as an intent — otherwise this
        # measures the plain typed path and cannot tell a click from a mint —
        # and ``typed`` travelled as the engine's own keyword, never inside the
        # client-filled ``intent_data`` (review F10).
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["typed"] is True
        assert "typed" not in engine.await_args.kwargs["intent_data"]
        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )

    async def test_typed_text_the_resolver_accepted_with_no_token_is_typed_other(
        self, counters
    ):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        assert confirmation_token_class("that works") is None

        await _turn(svc, "that works")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="typed_other", to_state="resolved"
        )

    async def test_a_click_counts_as_intent(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case())
        svc = _service(store)
        engine = _spy_on_the_engine(svc)

        await _turn(svc, CONFIRM_CARD["payload"], intent=_click_confirm())

        assert engine.await_args.kwargs["typed"] is False
        assert "typed" not in engine.await_args.kwargs["intent_data"]
        confirmation.labels.assert_called_once_with(via="intent", to_state="resolved")
        assert store.row().turn_history[-1].terminal_confirmed_via == "intent"

    async def test_a_confirmed_close_counts_as_closed(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))

        await _turn(_service(store), "yes")

        assert store.row().state == CaseState.CLOSED
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="closed"
        )

    async def test_the_inv37_pivot_counts_nothing(self, counters):
        confirmation, _ = counters
        store = _Store(_investigating_case("closed"))
        pivot = MagicMock(verdict=tt.ClosureReadiness.SUGGEST_RESOLVE, message="r")

        with patch.object(tt, "assess_closure_readiness", return_value=pivot):
            await _turn(_service(store), "ok")

        assert store.row().state == CaseState.INVESTIGATING
        assert store.row().pending_transition["to_state"] == "resolved"
        assert store.row().turn_history[-1].terminal_confirmed_via is None
        confirmation.labels.assert_not_called()

    async def test_a_raising_counter_never_fails_the_turn(self, counters):
        confirmation, _ = counters
        confirmation.labels.side_effect = RuntimeError("registry down")
        store = _Store(_investigating_case())

        response = await _turn(_service(store), "ok")

        confirmation.labels.assert_called_once()  # it did raise, here
        assert response.agent_response
        assert store.row().state == CaseState.RESOLVED


class TestTheGateOnARefusal:
    """#1783 through ``InvestigationService.process_turn``: a typed reply that
    refuses or defers never executes the pending terminal transition, whether
    the typed matcher reads it or the resolver mints a confirmation from it;
    and an ambiguous reply is re-asked, never recorded as a refusal."""

    SIGNATURE = "SUGGEST_RESOLVE|1|chain"

    def _store(self, pending: str = "resolved") -> _Store:
        case = _investigating_case(pending)
        # An engine proposer's offer: a refusal of it is recorded against this.
        case.pending_transition["justifying_signature"] = self.SIGNATURE
        return _Store(case)

    def _minting(self, store: _Store, card: dict):
        """The service with the confirmation pair on offer and the resolver
        minting ``card``'s intent from whatever is typed."""
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=card["intent"])
        return svc, _spy_on_the_engine(svc)

    def _assert_declined(self, store: _Store, response) -> None:
        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition is None
        assert row.progress.deferred_disposition_declined_signatures == [self.SIGNATURE]
        assert response.agent_response == (
            "Understood. The case remains open for further investigation."
        )

    def _assert_re_asked(self, store: _Store, response) -> None:
        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition["to_state"] == "resolved"
        assert row.pending_transition["re_presented"] is True
        assert row.progress.deferred_disposition_declined_signatures == []
        assert "Please select one of the options above" in response.agent_response

    async def test_a_typed_refusal_after_a_weak_token_declines(self, counters):
        confirmation, _ = counters
        store = self._store()

        response = await _turn(_service(store), "ok, don't close it yet")

        self._assert_declined(store, response)
        confirmation.labels.assert_not_called()

    @pytest.mark.parametrize(
        "message", ["\u274c close it", "\U0001f44e ok", "ok :wait:"]
    )
    async def test_a_refusal_symbol_before_or_after_the_token_declines(
        self, counters, message
    ):
        """The defeat pass's shapes: a negative emoji before the token, and a
        refusal shortcode after it."""
        confirmation, _ = counters
        store = self._store()

        response = await _turn(_service(store), message)

        self._assert_declined(store, response)
        confirmation.labels.assert_not_called()

    @pytest.mark.parametrize(
        "message", ["ok, don't close it yet", "nope, it's fine now"]
    )
    async def test_a_confirmation_minted_from_conflicting_text_is_re_asked(
        self, counters, message
    ):
        """Never executed, and never declined on the conflict."""
        confirmation, _ = counters
        store = self._store()
        svc, engine = self._minting(store, CONFIRM_CARD)

        response = await _turn(svc, message)

        # The mint reached the gate as a typed confirmation intent.
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["intent_data"] == {"value": True}
        assert engine.await_args.kwargs["typed"] is True
        self._assert_re_asked(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_minted_decline_outranks_a_typed_token(self, counters):
        confirmation, _ = counters
        store = self._store()
        svc, engine = self._minting(store, _resolution_confirmation_suggestions()[1])
        assert confirmation_token_class("ok") == "weak_token"

        response = await _turn(svc, "ok")

        assert engine.await_args.kwargs["intent_data"] == {"value": False}
        assert engine.await_args.kwargs["typed"] is True
        self._assert_declined(store, response)
        confirmation.labels.assert_not_called()

    @pytest.mark.parametrize(
        "query", [CONFIRM_CARD["payload"], "ok, don't close it yet"]
    )
    async def test_a_click_is_never_vetoed_by_its_text(self, counters, query):
        confirmation, _ = counters
        store = self._store()
        svc = _service(store)
        engine = _spy_on_the_engine(svc)

        await _turn(svc, query, intent=_click_confirm())

        assert engine.await_args.kwargs["typed"] is False
        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(via="intent", to_state="resolved")

    @pytest.mark.parametrize("message", ["ok no", "sure, I don't mind", "ok\uff1f"])
    async def test_an_ambiguous_reply_is_asked_again(self, counters, message):
        """A token then an unknown word, a refusal word inside what may be
        consent, a fullwidth question mark: none executes, none is recorded
        as a refusal of the offer."""
        confirmation, _ = counters
        store = self._store()

        response = await _turn(_service(store), message)

        self._assert_re_asked(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_question_carrying_a_refusal_word_records_no_refusal(self):
        """The question is answered by a normal turn (the LLM, doubled here to
        raise), and the offer is withdrawn for it without being recorded as
        refused."""
        store = self._store()
        svc = _service(store)
        engine = _spy_on_the_engine(svc)

        with pytest.raises(ServiceException, match="no route under test reaches"):
            await _turn(svc, "What happens to the runbook if I cancel this?")

        case = engine.await_args.kwargs["case"]
        assert svc.engine.generator.generate_structured_output.called
        assert case.state == CaseState.INVESTIGATING
        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == []

    @pytest.mark.parametrize(
        "message, via",
        [
            ("that\u2019s right", "explicit_token"),
            ("looks good to me", "weak_prefixed"),
        ],
    )
    async def test_a_typed_consent_confirms(self, counters, message, via):
        confirmation, _ = counters
        store = self._store()

        await _turn(_service(store), message)

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(via=via, to_state="resolved")

    @pytest.mark.parametrize(
        "pending, payload, state",
        [
            ("resolved", RESOLVE_CONFIRMATION_PAYLOAD, CaseState.RESOLVED),
            ("closed", CLOSE_CONFIRMATION_PAYLOAD, CaseState.CLOSED),
        ],
    )
    async def test_a_card_payload_typed_without_its_intent_confirms(
        self, counters, pending, payload, state
    ):
        confirmation, _ = counters
        store = self._store(pending)

        await _turn(_service(store), payload)

        assert store.row().state == state
        confirmation.labels.assert_called_once_with(
            via="explicit_prefixed", to_state=pending
        )


class TestTheFollowUpCounter:
    async def _resolved_on_ok(self, counters) -> tuple[_Store, InvestigationService]:
        store = _Store(_investigating_case())
        svc = _service(store)
        await _turn(svc, "ok")
        assert store.row().state == CaseState.RESOLVED
        counters[1].labels.assert_not_called()
        return store, svc

    async def test_only_the_first_message_after_counts_even_on_the_greeting_route(
        self, counters
    ):
        """Review F4: a greeting never reaches the engine, and is counted."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await _turn(svc, "hello")
        # The orientation lane answered it, not the engine's terminal branch.
        assert store.row().messages[-2]["metadata"].get("orientation") == "greeting"
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

        await _turn(svc, "hello")
        followup.labels.assert_called_once()  # still once: not the second message

    async def test_a_follow_up_whose_save_fails_counts_once_on_its_retry(
        self, counters
    ):
        """Review F3: counted after the save, so a retried turn counts once."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        store.fail_next_save(StaleCaseException(CASE_ID, 3, 4))

        with pytest.raises(StaleCaseException):
            await _turn(svc, "hello")
        followup.labels.assert_not_called()

        await _turn(svc, "hello")
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")
        followup.labels.return_value.inc.assert_called_once()

    async def test_a_click_after_the_confirmation_is_not_a_follow_up(self, counters):
        """Review F10: the confirmation card clicked again is not a user who
        kept talking to the case."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, CONFIRM_CARD["payload"], intent=_click_confirm())

        answered.assert_awaited_once()  # the click reached the terminal case
        followup.labels.assert_not_called()

    async def test_a_runbook_card_click_is_not_a_follow_up(self, counters):
        """Review F5: the ack turn's runbook card carries no intent and arrives
        as its payload text. The terminal handler's own recogniser says so."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        created = AsyncMock(
            side_effect=lambda case, metadata, dedup_confirmed=False: {
                "agent_response": "Creating your runbook draft.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(
            svc.engine.terminal.runbooks, "handle_runbook_creation", new=created
        ):
            await _turn(svc, GENERATE_RUNBOOK_PAYLOAD)

        created.assert_awaited_once()  # the text reached the card's branch
        followup.labels.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [REGENERATE_RESOLUTION_SUMMARY_PAYLOAD, REGENERATE_CLOSURE_SUMMARY_PAYLOAD],
    )
    async def test_a_regenerate_card_click_is_not_a_follow_up(self, counters, payload):
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)
        regenerated = AsyncMock(
            side_effect=lambda case, metadata: {
                "agent_response": "Regenerated.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(
            TerminalTurnHandler, "_handle_report_regeneration", new=regenerated
        ):
            await _turn(svc, payload)

        regenerated.assert_awaited_once()
        followup.labels.assert_not_called()

    @pytest.mark.parametrize("query", ["", "   "])
    async def test_an_empty_turn_is_not_a_follow_up(self, counters, query):
        """Review F3: an empty turn (a bare Slack mention) is an orientation
        request, answered on the greeting route with no intent."""
        _, followup = counters
        store = _Store(_investigating_case("closed"))
        svc = _service(store)
        await _turn(svc, "yes")
        assert store.row().state == CaseState.CLOSED

        await _turn(svc, query)

        assert store.row().messages[-2]["metadata"].get("orientation") == "empty"
        followup.labels.assert_not_called()

    async def test_the_runbook_text_on_a_closed_case_is_typed(self, counters):
        """Review F9: the runbook card acts only on a RESOLVED case, so on a
        CLOSED one its text goes to Q&A like any typed message — and is counted
        like one, by the same eligibility the handler dispatches on."""
        _, followup = counters
        store = _Store(_investigating_case("closed"))
        svc = _service(store)
        await _turn(svc, "yes")
        assert store.row().state == CaseState.CLOSED
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, user_id=None: {
                "agent_response": "Runbooks need a resolved case.",
                "case_updated": case,
                "metadata": metadata,
            }
        )

        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, GENERATE_RUNBOOK_PAYLOAD)

        answered.assert_awaited_once()  # Q&A answered it: not a card here
        followup.labels.assert_called_once_with(via="explicit_token", to_state="closed")

    async def _answered_on_terminal(self, svc, message):
        answered = AsyncMock(
            side_effect=lambda case, user_message, metadata, user_id=None: {
                "agent_response": "It is resolved.",
                "case_updated": case,
                "metadata": metadata,
            }
        )
        with patch.object(TerminalTurnHandler, "_process_terminal_qa", new=answered):
            await _turn(svc, message)
        answered.assert_awaited_once()

    @pytest.mark.parametrize("again", ["ok", "OK", " ok "])
    async def test_a_resubmitted_confirmation_is_not_a_follow_up(self, counters, again):
        """Review F1 of e98382937: a double submit carries nothing new."""
        confirmation, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await self._answered_on_terminal(svc, again)

        confirmation.labels.assert_called_once_with(
            via="weak_token", to_state="resolved"
        )
        followup.labels.assert_not_called()

    async def test_a_different_message_after_the_confirmation_is_a_follow_up(
        self, counters
    ):
        """The positive control for the resubmission screen."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await self._answered_on_terminal(svc, "wait, it's still failing")

        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")

    async def test_a_client_sent_greeting_is_typed(self, counters):
        """Review F8: the service ignores a client-sent GREETING and re-derives
        the intent from the text, so the user typed "hi" — the effective intent
        is None even though the request carried one."""
        _, followup = counters
        store, svc = await self._resolved_on_ok(counters)

        await _turn(svc, "hi", intent=QueryIntent(type=IntentType.GREETING))

        assert store.row().messages[-2]["metadata"].get("orientation") == "greeting"
        followup.labels.assert_called_once_with(via="weak_token", to_state="resolved")


_SERIES_CHILD = r"""
import json
from typing import get_args

from prometheus_client import REGISTRY

import faultmaven.core.investigation.lifecycle_metrics as lifecycle_metrics

series = {}
for family in REGISTRY.collect():
    if family.name not in (
        "faultmaven_terminal_confirmation",
        "faultmaven_terminal_followup",
    ):
        continue
    for sample in family.samples:
        if sample.name.endswith("_total"):
            key = f"{family.name}|{sample.labels['via']}|{sample.labels['to_state']}"
            series[key] = sample.value
print("@@RESULT@@" + json.dumps({"file": lifecycle_metrics.__file__, "series": series}))
"""


def test_every_series_is_exposed_at_zero_before_any_turn():
    """Review F2 of e98382937: a series born by its first increment is born at
    1, and the documented ``increase()`` query never sees that event. Every
    child of both counters must exist at 0 from import.

    A subprocess, because the shim decides real-or-NoOp when the counters are
    CREATED, at import: ``ENABLE_METRICS`` has to be set before that, and
    ``prometheus-client`` is a cloud extra (``.venv-cloud``), so the
    standalone leg skips.
    """
    pytest.importorskip("prometheus_client")
    repo_root = Path(__file__).resolve().parents[4]
    env = dict(os.environ)
    env["ENABLE_METRICS"] = "true"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH")) if p
    )

    proc = subprocess.run(
        [sys.executable, "-c", _SERIES_CHILD],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith("@@RESULT@@")
    )
    result = json.loads(line[len("@@RESULT@@") :])

    # The child measured THIS tree, not an editable install elsewhere.
    assert result["file"].startswith(str(repo_root))
    expected = {
        f"{family}|{via}|{to_state}": 0.0
        for family in (
            "faultmaven_terminal_confirmation",
            "faultmaven_terminal_followup",
        )
        for via in get_args(TerminalConfirmedVia)
        for to_state in (CaseState.RESOLVED.value, CaseState.CLOSED.value)
    }
    assert result["series"] == expected
