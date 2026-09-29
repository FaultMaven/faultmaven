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
    _gate_token_match,
)
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
    REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.terminal_turns import (
    TerminalTurnHandler,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    _EXPLICIT_CONFIRM_TOKENS,
    _WEAK_CONFIRM_TOKENS,
    _user_declines_transition,
    confirmation_token_class,
)
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.terminal_transitions import propose_transition
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

#: The label table. The class is read from the FIRST token of a consenting
#: reply. BARE means no letter or digit after that token, so punctuation, emoji
#: and emoticons made of punctuation keep a reply bare; PREFIXED means a consent
#: of more than one word (another token, or a filler such as "please"). A reply
#: that refuses, defers or carries any word outside the consent grammar is not
#: a confirmation at all (#1783), so it has no label: see the corpus below.
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
    ],
    "weak_prefixed": [
        "ok go ahead",
        "ok ok",
        "sure, close it",
        "ok yes",
        "lgtm, confirmed",
        "ok thanks",
        "ok :+1: go ahead",
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
    ],
    "explicit_prefixed": [
        "yes please close it",
        "yes please",
        "yes, mark as resolved",
        "yes ok",
    ],
}

#: What "Yes, mark as resolved" sends when clicked.
CONFIRM_CARD = _resolution_confirmation_suggestions()[0]

# #1783's corpus: what the gate reads as consent. Built negatives first, from
# replies that share the tokens. ``main`` confirmed every reply that OPENED with
# a token, so every row of RE_ASKED and READ_AS_A_DECLINE, and every row of
# MUST_NOT_CONFIRM but its last three, was a terminal transition there;
# MUST_CONFIRM confirmed there too, except the curly apostrophe rows.

#: Consents: every one still confirms.
MUST_CONFIRM = [
    "ok",
    "ok!",
    "OK.",
    "ok 👍",
    "ok :+1:",
    "okay",
    "sure",
    "lgtm",
    "sounds good",
    "looks good",
    "yes",
    "yes!",
    "yes please",
    "yes, please close it",
    "yes please close it",
    "ok go ahead",
    "sure, close it",
    "lgtm, confirmed",
    "that's right",
    "go ahead",
    "please do",
    "ok thanks",
    "yes thank you",
    "ok ok",
    "yep",
    "confirmed",
    "mark it as resolved",
    "yes, mark as resolved",
    "absolutely",
    "go for it",
    # Curly apostrophes, as mobile and macOS autocorrect type them. Neither
    # confirmed on ``main``.
    "that\u2019s right",
    "that\u2019s correct",
]

#: Refusals and deferrals that open with a token: none confirms.
MUST_NOT_CONFIRM = [
    "ok, don't close it yet",
    "ok, don\u2019t close it yet",
    "ok no",
    "ok, no",
    "sure, do it later",
    "okay i'll confirm with the team",
    "lgtm, not approved yet",
    "ok. mark as resolved later",
    "yes, don't close it yet",
    "do it later",
    "confirm later",
    "ok, wait",
    "ok not now",
    "ok hold on",
    "yes no",
    "ok stop",
    "ok cancel that",
    "ok never mind",
    "yes... actually no",
    "okay, let me double check first",
    "ok after lunch",
    "sure \u2014 tomorrow",
    # Refused on ``main`` as well, and still refused.
    "yes but not yet",
    "sure, but first check the logs",
    "yesterday it was fine",
]

#: Consents outside the closed grammar. They confirmed on ``main``; now the gate
#: re-asks once rather than confirm (a missed consent costs one re-ask, a false
#: one closes a case irreversibly). The first four are the plan's; the rest were
#: measured on this change, from the rows #1748 and the escape lane pinned.
RE_ASKED = [
    "sure thing",
    "yes, no problem",
    "ok no worries",
    "sure, whenever",
    "looks good to me",
    "yes, it's resolved, the error is gone",
    "ok :D",
    "yes :P",
]

#: Consents that also carry a refusal phrase: the phrase vetoes and declines.
READ_AS_A_DECLINE = [
    "yes, don't wait",
    "go ahead, no need to wait",
]

#: The replies that refuse or defer anywhere, and so decline the transition.
DECLINES = [
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
    "yes but not yet",
    *READ_AS_A_DECLINE,
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


class TestARefusalNeverConfirms:
    """#1783: consent is the whole reply, and a refusal anywhere vetoes it."""

    @pytest.mark.parametrize("message", MUST_CONFIRM)
    def test_every_consent_confirms(self, message):
        assert confirmation_token_class(message) is not None

    @pytest.mark.parametrize("message", MUST_NOT_CONFIRM + RE_ASKED + READ_AS_A_DECLINE)
    def test_no_refusal_and_no_unknown_word_confirms(self, message):
        assert confirmation_token_class(message) is None

    @pytest.mark.parametrize("message", DECLINES)
    def test_a_refusal_anywhere_declines(self, message):
        assert _user_declines_transition(message)

    @pytest.mark.parametrize(
        "message",
        MUST_CONFIRM + [m for m in MUST_NOT_CONFIRM + RE_ASKED if m not in DECLINES],
    )
    def test_no_consent_and_no_bare_unknown_word_declines(self, message):
        """Bare "no" is not a refusal phrase: "yes, no problem" is re-asked,
        never declined, and "ok no" gets the same one-time re-ask."""
        assert not _user_declines_transition(message)

    def test_a_token_that_carries_a_refusal_still_never_confirms(self, monkeypatch):
        """The veto holds on its own, not only because no token today carries a
        refusal phrase: a token added that does is still refused."""
        monkeypatch.setattr(
            transition_consent,
            "_EXPLICIT_CONFIRM_TOKENS",
            (*_EXPLICIT_CONFIRM_TOKENS, "close it later"),
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
    the typed matcher reads it or the resolver mints a confirmation from it."""

    SIGNATURE = "SUGGEST_RESOLVE|1|chain"

    def _store(self) -> _Store:
        case = _investigating_case()
        # An engine proposer's offer: a refusal of it is recorded against this.
        case.pending_transition["justifying_signature"] = self.SIGNATURE
        return _Store(case)

    def _assert_declined(self, store: _Store, response) -> None:
        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition is None
        assert row.progress.deferred_disposition_declined_signatures == [self.SIGNATURE]
        assert response.agent_response == (
            "Understood. The case remains open for further investigation."
        )

    async def test_a_typed_refusal_after_a_weak_token_declines(self, counters):
        confirmation, _ = counters
        store = self._store()

        response = await _turn(_service(store), "ok, don't close it yet")

        self._assert_declined(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_confirmation_the_resolver_minted_from_a_refusal_declines(
        self, counters
    ):
        confirmation, _ = counters
        store = self._store()
        _offer_the_resolution_cards(store)
        svc = _service(store)
        svc.intent_resolver.resolve = AsyncMock(return_value=CONFIRM_CARD["intent"])
        engine = _spy_on_the_engine(svc)

        response = await _turn(svc, "ok, don't close it yet")

        # The mint reached the gate as a typed confirmation intent.
        assert engine.await_args.kwargs["intent_type"] == "confirmation"
        assert engine.await_args.kwargs["intent_data"] == {"value": True}
        assert engine.await_args.kwargs["typed"] is True
        self._assert_declined(store, response)
        confirmation.labels.assert_not_called()

    async def test_a_minted_decline_outranks_a_typed_token(self, counters):
        confirmation, _ = counters
        store = self._store()
        _offer_the_resolution_cards(store)
        svc = _service(store)
        decline_card = _resolution_confirmation_suggestions()[1]
        svc.intent_resolver.resolve = AsyncMock(return_value=decline_card["intent"])
        engine = _spy_on_the_engine(svc)
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

    async def test_a_token_then_an_unknown_word_is_asked_again(self, counters):
        confirmation, _ = counters
        store = self._store()

        response = await _turn(_service(store), "ok no")

        row = store.row()
        assert row.state == CaseState.INVESTIGATING
        assert row.pending_transition["to_state"] == "resolved"
        assert row.pending_transition["re_presented"] is True
        assert row.progress.deferred_disposition_declined_signatures == []
        assert "Please select one of the options above" in response.agent_response
        confirmation.labels.assert_not_called()

    async def test_a_curly_apostrophe_confirms(self, counters):
        confirmation, _ = counters
        store = self._store()

        await _turn(_service(store), "that\u2019s right")

        assert store.row().state == CaseState.RESOLVED
        confirmation.labels.assert_called_once_with(
            via="explicit_token", to_state="resolved"
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
