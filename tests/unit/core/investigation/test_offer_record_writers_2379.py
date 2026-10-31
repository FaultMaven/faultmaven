"""The offer record's writers: open, answer, lapse (#2379).

The record (``cases.metadata["offer_record"]``) holds at most one open offer,
every answer, and a remembered resolution request. Only the writers in
``core/investigation/offers.py`` change it, each returning a new frozen record
its caller assigns. These pin the rules the writers enforce on their own,
apart from any turn: one open slot, a stale or foreign answer refused, the
driver as the only answerer, an accept recorded only after the executor
returns, and a bounded history that keeps every standing decline.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.offers import (
    MAX_ANSWERS,
    answer_offer,
    lapse_offers,
    open_offer,
    standing_resolve_decline,
)
from faultmaven.core.investigation.problem_status import propose_revision
from faultmaven.core.investigation.terminal_transitions import (
    confirm_open_offer,
    execute_user_closure,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    CaseState,
    ClosureReason,
    OfferBasis,
    OfferKind,
    ProblemStatus,
)
from tests.case_builders import (
    investigating_case,
    with_declined_offer,
    with_open_offer,
)

pytestmark = pytest.mark.unit


def _case():
    case = investigating_case(
        case_id="case_2379aaaaaaaa",
        title="Checkout 502s",
        user_id="user_creator",
        enterprise_id="org_test",
        statement="Checkout returns 502 for every user",
        problem_status=ProblemStatus.VERIFIED,
        current_turn=4,
    )
    return case


def _answers(case) -> list:
    return [answer.answer for answer in case.offer_record.answers]


class TestOneOpenSlot:
    def test_opening_while_an_offer_is_open_raises(self):
        case = _case()
        with_open_offer(case, kind="close")
        with pytest.raises(ValueError, match="already has a close offer open"):
            open_offer(case, kind=OfferKind.RESOLVE, basis=OfferBasis.BACKSTOP)

    def test_an_open_a_standing_decline_binds_writes_nothing(self):
        case = _case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        before = case.offer_record

        assert (
            open_offer(case, kind=OfferKind.RESOLVE, basis=OfferBasis.BACKSTOP) is None
        )
        assert case.offer_record is before
        # The user's own offer is never bound by a decline.
        assert open_offer(case, kind=OfferKind.RESOLVE, basis=OfferBasis.REOPEN)

    def test_a_close_offer_carries_its_reason_and_only_a_close_does(self):
        case = _case()
        with pytest.raises(ValueError, match="carries its closure_reason"):
            open_offer(case, kind=OfferKind.CLOSE, basis=OfferBasis.MODEL)
        with pytest.raises(ValueError, match="carries its closure_reason"):
            open_offer(
                case,
                kind=OfferKind.RESOLVE,
                basis=OfferBasis.MODEL,
                closure_reason=ClosureReason.SOLUTION_DEFERRED,
            )
        assert case.offer_record.open is None


class TestWhoAnswersWhat:
    def test_an_answer_naming_another_offer_is_refused(self):
        case = _case()
        offer = with_open_offer(case)
        with pytest.raises(ValueError, match="is not the open offer"):
            answer_offer(
                case,
                proposal_id="0123456789abcdef",
                answer=AnswerKind.ACCEPTED,
                via=AnswerVia.CLICK,
            )
        assert case.offer_record.open == offer

    def test_an_answer_from_anyone_but_the_driver_raises(self):
        case = _case()
        offer = with_open_offer(case)
        with pytest.raises(ValueError, match="only the case's driver"):
            answer_offer(
                case,
                proposal_id=offer.proposal_id,
                answer=AnswerKind.DECLINED,
                via=AnswerVia.CLICK,
                actor_id="user_reader",
            )
        assert case.offer_record.open == offer

    def test_a_reassignment_keeps_the_offer_and_the_new_driver_answers(self):
        """Ruling 7: a driver change lapses nothing, and the answer records the
        effective driver at the time it is given."""
        case = _case()
        offer = with_open_offer(case)
        case.driver_id = "user_new_driver"

        case.offer_record = lapse_offers(case)
        assert case.offer_record.open == offer

        case.offer_record = answer_offer(
            case,
            proposal_id=offer.proposal_id,
            answer=AnswerKind.DECLINED,
            via=AnswerVia.CLICK,
            actor_id="user_new_driver",
        )
        assert case.offer_record.answers[-1].answered_by == "user_new_driver"

    def test_a_lapse_records_no_answerer(self):
        case = _case()
        offer = with_open_offer(case)
        case.offer_record = answer_offer(
            case,
            proposal_id=offer.proposal_id,
            answer=AnswerKind.LAPSED,
            via=AnswerVia.MENU,
        )
        assert case.offer_record.answers[-1].answered_by is None


class TestTheRecordIsFrozen:
    def test_the_record_refuses_an_in_place_write(self):
        case = _case()
        with_open_offer(case)
        with pytest.raises(ValidationError):
            case.offer_record.open = None
        with pytest.raises(ValidationError):
            case.offer_record.open.summary = "edited"


class TestLapse:
    def test_lapse_offers_is_idempotent(self):
        case = _case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        case.current_turn += 1
        case.solutions = []  # nothing a resolve premise reads
        once = lapse_offers(case)
        case.offer_record = once
        assert lapse_offers(case) == once

    def test_an_open_offer_lapses_when_the_case_is_terminal(self):
        case = _case()
        with_open_offer(case, kind="close")
        case.atomic_update(
            state=CaseState.CLOSED,
            closure_reason="closed_insufficient_evidence",
            closed_at=datetime.now(timezone.utc),
        )
        case.offer_record = lapse_offers(case)
        assert case.offer_record.open is None
        assert _answers(case) == [AnswerKind.LAPSED]

    def test_the_rest_close_lapses_the_open_offer(self):
        case = _case()
        with_open_offer(case, kind="resolve", basis="backstop")

        execute_user_closure(case, "user_creator")

        assert case.state == CaseState.CLOSED
        assert case.offer_record.open is None
        assert _answers(case) == [AnswerKind.LAPSED]


class TestAccepting:
    def test_an_executor_that_raises_leaves_the_offer_open(self):
        case = _case()
        offer = with_open_offer(case, kind="resolve")
        with patch.object(
            terminal_transitions,
            "_execute_resolved_transition",
            side_effect=RuntimeError("executor failed"),
        ):
            with pytest.raises(RuntimeError, match="executor failed"):
                confirm_open_offer(case, "user_creator", via=AnswerVia.CLICK)

        assert case.offer_record.open == offer
        assert _answers(case) == []

    def test_an_accept_is_recorded_after_the_transition(self):
        case = _case()
        with_open_offer(case, kind="close")

        assert confirm_open_offer(case, "user_creator", via=AnswerVia.TOKEN)

        assert case.state == CaseState.CLOSED
        assert _answers(case) == [AnswerKind.ACCEPTED]
        assert case.offer_record.answers[-1].via == AnswerVia.TOKEN
        assert case.offer_record.answers[-1].answered_by == "user_creator"


class TestRevisionAnswers:
    def test_a_rewording_supersedes_the_open_revision(self):
        case = _case()
        propose_revision(
            case,
            text="Checkout returns 502 for EU users only",
            evidence_ids=[],
            basis="the US traffic is clean",
            wording_key="revision:first",
        )
        propose_revision(
            case,
            text="Checkout returns 502 for EU mobile users only",
            evidence_ids=[],
            basis="desktop EU traffic is clean too",
            wording_key="revision:second",
        )

        assert _answers(case) == [AnswerKind.SUPERSEDED]
        revision = case.offer_record.open.revision
        assert revision.wording_key == "revision:second"
        # The hold keeps the status the case had before the first wording.
        assert revision.prior_status == ProblemStatus.VERIFIED


class TestTheHistoryIsBounded:
    def test_the_cap_never_evicts_a_standing_decline(self):
        case = _case()
        decline = with_declined_offer(case, kind="resolve", basis="backstop")
        for _ in range(MAX_ANSWERS + 6):
            offer = with_open_offer(case, kind="close", basis="model")
            case.offer_record = answer_offer(
                case,
                proposal_id=offer.proposal_id,
                answer=AnswerKind.WITHDRAWN,
                via=AnswerVia.TYPED,
            )

        assert len(case.offer_record.answers) == MAX_ANSWERS
        assert case.offer_record.answers[0] == decline
        assert standing_resolve_decline(case) == decline
