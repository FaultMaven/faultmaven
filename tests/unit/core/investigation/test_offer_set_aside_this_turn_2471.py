"""#2471: an offer the user set aside on a turn stays quiet for the rest of it.

fm#1122's turn quiet was held by two callers, the engine's backstop and its
deferred proposer, so the model's step-2 proposal re-opened an offer of the
kind the user had just withdrawn (a question, a long typed reply) or declined
without binding (a bare "no" with an upload), in the same reply. Rule (a),
ruled 2026-11-03: the writer refuses an offer of the same kind answered this
turn, after readiness and the decline refusals, unless the turn recorded a
confirmation the answer never saw (INV-43). The user's own offers and the
rca-infeasible stabilized close are exempt.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.milestone_engine.transitions import (
    DECLINED_RESOLVE_FEEDBACK,
    TRANSITION_QUIET_FEEDBACK,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    ProblemVerificationUpdate,
    ProposedTransition,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import EvidenceCategory, OfferKind
from tests.case_builders import with_open_offer
from tests.unit.core.investigation.bound_rows import bound_row
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _ROW,
    _confirmed_case,
    _engine,
    _feedback,
    _qualifying,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _case as _deferred_case,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _deferred_response,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _DSU,
    _false_alarm_case,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _false_alarm_engine,
)
from tests.unit.core.investigation.test_rca_infeasible_closure import (
    _turn_case as _rca_case,
)
from tests.unit.core.investigation.test_rca_infeasible_closure import (
    _turn_engine as _rca_engine,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _FILE_ID,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _case as _backstop_case,
)

pytestmark = pytest.mark.unit

#: A question about the open offer: it withdraws the offer (fm#1122).
_QUESTION = "How do we know the fix held for the overnight batch?"
#: A long typed refusal: no decline since #2222, so it is withdrawn too.
_REFUSAL = (
    "No, keep it open - we will verify after the Friday maintenance window first."
)
_SOLUTION = {
    "description": "Pin the token audience on the service account",
    "solution_type": "config_change",
    "estimated_impact": "the batch pods assume the role",
    "risks": "none",
}
_UPLOAD = [{"file_id": _FILE_ID, "filename": "sts.log"}]


async def _turn(
    engine,
    case,
    message: str,
    *,
    propose: str | None = None,
    row: bool = False,
    solution: bool = False,
    **process_kw,
) -> dict:
    case.current_turn += 1
    updates: dict = {}
    if row:
        updates["evidence_to_add"] = [dict(_ROW)]
    if solution:
        updates["solutions_to_add"] = [dict(_SOLUTION)]
    if propose:
        updates["proposed_transition"] = {"to_state": propose}
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=updates
        )
    )
    return await engine.process_turn(
        case=case, user_message=message, redaction_ctx=None, **process_kw
    )


async def _backstop_offered():
    """READY: the engine's backstop offers the resolution on turn 1."""
    case = _confirmed_case()
    engine = _engine()
    await _turn(engine, case, "errors gone")
    assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
        "resolve",
        "backstop",
    )
    return engine, case


def _quiet(kind: str) -> str:
    return TRANSITION_QUIET_FEEDBACK[OfferKind(kind)]


def _compliance(caplog):
    lines = [r for r in caplog.records if r.getMessage() == "transition_compliance"]
    assert lines, "premise: the turn logged its transition_compliance line"
    return lines[-1]


def _last(case):
    return case.offer_record.answers[-1]


class TestTheModelsProposalIsQuiet:
    @pytest.mark.parametrize("message", [_QUESTION, _REFUSAL])
    async def test_a_withdrawn_resolve_is_not_re_opened_by_the_model(
        self, message, caplog
    ):
        """P1, P2: on ``main`` a new model offer opened in the same reply."""
        engine, case = await _backstop_offered()
        with caplog.at_level("INFO"):
            result = await _turn(engine, case, message, propose="resolved")
        assert case.offer_record.open is None
        assert (_last(case).answer, _last(case).at_turn) == (
            "withdrawn",
            case.current_turn,
        )
        assert _quiet("resolve") in _feedback(result)
        line = _compliance(caplog)
        assert line.transition_quiet_this_turn is True
        assert line.transition_refused_as_declined is None

    async def test_a_new_confirmation_re_offers_on_the_same_turn(self):
        """P3, INV-43: the turn recorded a row the withdrawal never saw."""
        engine, case = await _backstop_offered()
        result = await _turn(engine, case, _QUESTION, row=True, propose="resolved")
        assert not set(_qualifying(case)) <= set(_last(case).premise.confirmation_ids)
        assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
            "resolve",
            "model",
        )
        assert _quiet("resolve") not in _feedback(result)

    async def test_a_close_that_pivots_to_the_withdrawn_resolve_is_quiet(self):
        """P4: a model ``closed`` on a resolvable case is the resolve again."""
        engine, case = await _backstop_offered()
        result = await _turn(engine, case, _QUESTION, propose="closed")
        assert case.offer_record.open is None
        assert _quiet("resolve") in _feedback(result)

    @pytest.mark.parametrize("propose", ["closed", "resolved"])
    async def test_a_solution_recorded_this_turn_does_not_lift_it(self, propose):
        """A solution is not a confirmation (critique 1)."""
        engine, case = await _backstop_offered()
        solutions = len(case.solutions)
        result = await _turn(engine, case, _QUESTION, solution=True, propose=propose)
        assert len(case.solutions) == solutions + 1, "premise: a new solution"
        assert case.offer_record.open is None
        assert _quiet("resolve") in _feedback(result)

    async def test_a_withdrawn_close_leaves_the_models_resolve(self):
        """The other kind opens. The close is opened before the confirmation
        rows are recorded, and the turn records none: the kind is the only
        reason it opens (critique 7)."""
        case = _backstop_case(absence=None)
        with_open_offer(case, kind="close", basis="model")
        for category in (
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        ):
            bound_row(case, category, turn=1, source_file_id=_FILE_ID)
        verdict = assess_resolution_readiness(case).verdict
        assert verdict == ResolutionReadiness.READY, "premise: READY"
        await _turn(_engine(), case, _QUESTION, propose="resolved")
        assert (_last(case).kind, _last(case).answer) == ("close", "withdrawn")
        assert set(_qualifying(case)) <= set(_last(case).premise.confirmation_ids)
        assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
            "resolve",
            "model",
        )

    async def test_a_needs_info_request_is_still_remembered(self, caplog):
        """#2408: the quiet sits after readiness, so a request is a request."""
        case = _backstop_case(absence=None)
        verdict = assess_resolution_readiness(case).verdict
        assert verdict == ResolutionReadiness.NEEDS_INFO, "premise: NEEDS_INFO"
        with_open_offer(case, kind="resolve", basis="model")
        engine = _engine()
        with caplog.at_level("INFO"):
            await _turn(engine, case, _QUESTION, propose="resolved")
        assert case.offer_record.open is None
        assert case.offer_record.resolve_request is not None
        assert _compliance(caplog).transition_quiet_this_turn is False

    async def test_a_bare_decline_still_reports_its_refusal(self, caplog):
        """#1895: a binding decline is what the refusal reports."""
        engine, case = await _backstop_offered()
        with caplog.at_level("INFO"):
            result = await _turn(engine, case, "no", propose="resolved")
        assert _last(case).answer == "declined"
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert _quiet("resolve") not in _feedback(result)
        line = _compliance(caplog)
        assert line.transition_refused_as_declined == "resolve"
        assert line.transition_quiet_this_turn is False

    @pytest.mark.parametrize("propose", ["closed", "resolved"])
    async def test_a_finding_withdrawn_with_the_question_keeps_the_close_quiet(
        self, propose
    ):
        """The user's question withdraws the false-alarm close; the model
        withdraws the finding and proposes in the same reply. A withdrawn
        finding is not a new one: no close re-opens (review of 63619934c)."""
        case = _false_alarm_case(model_close=False)
        engine = _false_alarm_engine()
        engine.generator.generate_structured_output = AsyncMock(
            return_value=InvestigationResponse_Diagnosis(
                agent_response="Noted.",
                state_updates=_DSU(
                    verification_updates=ProblemVerificationUpdate(
                        invalidation_withdrawn=True,
                        withdrawal_basis="the user has customer tickets from then",
                    ),
                    proposed_transition=ProposedTransition(to_state=propose),
                ),
            )
        )
        case.current_turn += 1
        result = await engine.process_turn(
            case=case,
            user_message="Why call it a false alarm? We have tickets from then.",
            redaction_ctx=None,
        )
        assert case.problem_verification.invalidation is None, "premise"
        assert (_last(case).kind, _last(case).answer) == ("close", "withdrawn")
        assert case.offer_record.open is None
        if propose == "closed":
            assert _quiet("close") in _feedback(result)

    async def test_a_standing_deferred_decline_still_reports_itself(self, caplog):
        """The deferred side of #1889: beside a close the user set aside this
        turn, the model's ``closed`` is refused as the declined deferred close
        it is, with its card, not as the quiet."""
        case = _deferred_case(confirmed=False)
        engine = _engine()
        engine.generator.generate_structured_output = AsyncMock(
            return_value=_deferred_response()
        )
        await engine.process_turn(
            case=case, user_message="the platform team ships it", redaction_ctx=None
        )
        assert case.offer_record.open.basis == "deferred", "premise"
        case.current_turn += 1
        await engine.process_turn(case=case, user_message="no", redaction_ctx=None)
        assert _last(case).answer == "declined", "premise: the deferred decline"
        with_open_offer(case, kind="close", basis="menu")
        with caplog.at_level("INFO"):
            result = await _turn(engine, case, _QUESTION, propose="closed")
        assert (_last(case).basis, _last(case).answer) == ("menu", "withdrawn")
        assert case.offer_record.open is None
        assert "deferred-implementation close" in _feedback(result)
        assert _quiet("close") not in _feedback(result)
        line = _compliance(caplog)
        assert line.transition_refused_as_declined == "deferred"
        assert line.transition_quiet_this_turn is False

    async def test_a_close_declined_with_an_upload_is_not_re_opened(self, caplog):
        """A bare "no" with an upload declines a model close, binding
        nothing; the model's same-turn ``closed`` used to re-open it."""
        case = _backstop_case(absence=None)
        with_open_offer(case, kind="close", basis="model")
        engine = _engine()
        with caplog.at_level("INFO"):
            result = await _turn(
                engine, case, "no", propose="closed", attachments=_UPLOAD
            )
        assert (_last(case).kind, _last(case).answer) == ("close", "declined")
        assert case.offer_record.open is None
        assert _quiet("close") in _feedback(result)
        assert _compliance(caplog).transition_refused_as_declined is None


class TestTheExemptOpeners:
    async def test_the_stabilized_close_opens_on_the_turn_a_close_was_withdrawn(
        self, caplog
    ):
        """Ruled 2026-11-03: the mitigation verified on this turn earns the
        rca-infeasible close, as a new confirmation earns a resolve. The
        model's same-turn ``closed`` stays quiet: the engine's offer stands."""
        case = _rca_case()
        withdrawn = with_open_offer(case, kind="close", basis="model")
        case.current_turn += 1
        with caplog.at_level("INFO"):
            result = await _rca_engine("closed").process_turn(
                case=case,
                user_message="the failover held - what happens if I close this now?",
                redaction_ctx=None,
            )
        assert (_last(case).proposal_id, _last(case).answer) == (
            withdrawn.proposal_id,
            "withdrawn",
        )
        offer = case.offer_record.open
        assert (offer.kind, offer.basis) == ("close", "rca_infeasible")
        # Its rca_infeasible_closure_message, rendered below the reply.
        assert offer.summary.startswith("The mitigation is verified and stable.")
        assert offer.summary in result["agent_response"]
        assert _compliance(caplog).transition_superseded_by_engine is True

    async def test_the_users_menu_close_opens_after_the_close_it_withdrew(self):
        """A dropdown Close with an upload withdraws the open close and opens
        the user's own (#1866's path, unchanged)."""
        case = _backstop_case(absence=None)
        withdrawn = with_open_offer(case, kind="close", basis="model")
        await _turn(
            _engine(),
            case,
            "Close this case",
            attachments=_UPLOAD,
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        assert (_last(case).proposal_id, _last(case).answer) == (
            withdrawn.proposal_id,
            "withdrawn",
        )
        assert (case.offer_record.open.kind, case.offer_record.open.basis) == (
            "close",
            "menu",
        )
