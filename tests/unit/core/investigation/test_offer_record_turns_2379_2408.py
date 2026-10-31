"""Two shapes the offer record fixes, through real turns (#2379, #2408).

#2379 state 1: a resolvable case whose resolution the user declined, then held
on a revised problem statement. ``closed`` read ``suggests_alternative`` there,
so the status menu hid Close while nothing the user could answer offered a
resolve. Close shows; choosing it withdraws the revision and offers the close
as ``resolution_declined``, and confirming it closes the case. Close is hidden
only while a resolve offer is open (ruling 6).

#2408: a model ``resolved`` the case had not earned (NEEDS_INFO) stored a
pending resolve with confirmation chips under a reply asking for more. It is a
request, not an offer (ruling 1): nothing opens, no chips show, Close stays in
the menu, and the ask goes below the reply. The next turn reads the request
once: READY offers the resolve, still NEEDS_INFO offers the close.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.offers import standing_false_alarm_decline
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    EvidenceTrail,
    InvestigationResponse_Mitigation,
    MilestoneJustifications,
    MilestoneUpdates,
    ProblemVerificationUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    derive_disposition_eligibility,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    CaseState,
    ClosureReason,
    MitigationRecord,
    ProblemStatus,
)
from faultmaven.modules.case.domain.models.evidence import (
    EvidenceCategory,
    EvidenceSourceType,
)
from tests.unit.core.investigation.test_offer_record_declines_bind_2379 import (
    _DSU,
    _FILE_ID,
    CLOSE_PAIR,
    RESOLVE_PAIR,
    _base_case,
    _declined_resolve,
    _engine,
    _labels,
    _proposes,
    _resolvable_case,
    _revision,
    _to_add,
    _turn,
)

pytestmark = pytest.mark.unit

#: The status menu's Close, as the copilot sends it.
_MENU_CLOSE = {"from_state": "investigating", "to_state": "closed"}


def _confirmation_keys(result) -> list:
    return [
        (f.get("intent") or {}).get("proposal_id")
        for f in result["suggested_follow_ups"]
        if (f.get("intent") or {}).get("type") == "confirmation"
    ]


def _closed(case) -> str:
    return derive_disposition_eligibility(case)["closed"]


class TestCloseUnderAPendingRevision:
    async def test_close_shows_and_closes_as_resolution_declined(self):
        engine, case = await _declined_resolve()
        await _revision(engine, case)
        assert case.offer_record.open.kind == "revision", "premise"
        assert _closed(case) == "ready"

        offered = await _turn(
            engine,
            case,
            "Close this case as unresolved. Summarize what we found so far.",
            intent_type="status_transition",
            intent_data=_MENU_CLOSE,
        )

        # The revision is withdrawn and the status it held returns.
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        revision_answer = next(
            a for a in case.offer_record.answers if a.kind == "revision"
        )
        assert revision_answer.answer == AnswerKind.WITHDRAWN
        offer = case.offer_record.open
        assert offer.kind == "close"
        assert offer.closure_reason == ClosureReason.RESOLUTION_DECLINED
        assert _labels(offered) == CLOSE_PAIR

        key = _confirmation_keys(offered)[0]
        await _turn(
            engine,
            case,
            "Yes, close this case without resolution.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": key},
        )
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == ClosureReason.RESOLUTION_DECLINED

    async def test_close_is_hidden_beside_an_open_resolve_offer(self):
        case, engine = _resolvable_case(), _engine()
        offered = await _turn(engine, case, "the errors are gone now")

        assert _labels(offered) == RESOLVE_PAIR
        assert _closed(case) == "suggests_alternative"


def _needs_info_case():
    case = _base_case()
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO, "premise"
    return case, readiness.message


async def _asked(engine, case) -> dict:
    """A model ``resolved`` on NEEDS_INFO: the request turn."""
    return await _turn(engine, case, "the fix is in", _proposes("resolved"))


class TestANeedsInfoProposalIsARequest:
    async def test_it_opens_nothing_and_asks_below_the_reply(self):
        case, message = _needs_info_case()
        engine = _engine()

        result = await _asked(engine, case)

        assert case.offer_record.open is None
        assert case.offer_record.resolve_request is not None
        assert _confirmation_keys(result) == []
        assert not set(RESOLVE_PAIR) & set(_labels(result))
        assert derive_disposition_eligibility(case) == {
            "resolved": "needs_info",
            "closed": "ready",
        }
        reply = result["agent_response"]
        assert reply.startswith("Noted.")
        assert message in reply

    async def test_the_next_turn_offers_the_resolve_once_it_is_earned(self):
        case, _ = _needs_info_case()
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(
            engine,
            case,
            "the overnight batch ran clean",
            _DSU(
                evidence_to_add=[
                    _to_add(
                        "causal_absence_evidence",
                        "The provider's ClientIDList now reads sts.amazonaws.com.",
                    ),
                    _to_add(
                        "symptom_absence_evidence",
                        "Zero AssumeRole failures in the overnight batch.",
                    ),
                ]
            ),
        )

        assert case.offer_record.open.kind == "resolve"
        assert _labels(result) == RESOLVE_PAIR
        assert case.offer_record.resolve_request is None

    async def test_the_next_turn_still_short_offers_the_close(self):
        case, _ = _needs_info_case()
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(engine, case, "we have not checked anything else")

        offer = case.offer_record.open
        assert offer.kind == "close"
        assert offer.basis == "needs_info_unmet"
        assert _labels(result) == CLOSE_PAIR
        assert case.offer_record.resolve_request is None

    async def test_the_request_is_rechecked_before_any_engine_opener(self):
        """The apply-step openers stand down while a request awaits step 0:
        a deferred close the next turn would have opened does not pre-empt
        the re-check, whose still-short outcome is the close it opens."""
        case, _ = _needs_info_case()
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(
            engine,
            case,
            "we can't apply it until next week's change window",
            _DSU(milestones=MilestoneUpdates(solution_feasible="deferred")),
        )

        offer = case.offer_record.open
        assert offer.basis == "needs_info_unmet"
        assert _labels(result) == CLOSE_PAIR
        assert case.offer_record.resolve_request is None

    async def test_a_false_alarm_finding_waits_for_the_recheck(self):
        """The false-alarm twin: a finding recorded on the turn after the
        request does not open the false-alarm proposer's close; the re-check
        opens its close, on the finding's reason."""
        case, _ = _needs_info_case()
        case.solutions = []
        case.root_cause_conclusion = None
        engine = _engine()
        await _asked(engine, case)

        result = await _turn(
            engine,
            case,
            "the gateway log shows no errors",
            _finding("No 5xx 13:55-14:05.", "no 5xx in the window"),
        )

        assert case.progress.problem_status == ProblemStatus.INVALIDATED, "premise"
        offer = case.offer_record.open
        assert offer.basis == "needs_info_unmet"
        assert offer.closure_reason == ClosureReason.CLOSED_FALSE_ALARM
        assert _labels(result) == CLOSE_PAIR

    async def test_an_rca_infeasible_close_waits_for_the_recheck(self):
        """The rca-infeasible twin: a mitigation verified on the turn after
        the request does not open the stabilized close; the re-check opens
        its close."""
        case = _rca_case()
        assert assess_resolution_readiness(case).verdict == (
            ResolutionReadiness.NEEDS_INFO
        ), "premise"
        engine = _engine()
        await _asked(engine, case)
        assert case.offer_record.resolve_request is not None, "premise"

        case.current_turn += 1
        engine.generator.generate_structured_output = AsyncMock(
            return_value=_mitigation_verified()
        )
        result = await engine.process_turn(
            case=case, user_message="the failover held", redaction_ctx=None
        )

        assert case.progress.mitigation.verified is True, "premise"
        offer = case.offer_record.open
        assert offer.basis == "needs_info_unmet"
        assert _labels(result) == CLOSE_PAIR


def _rca_case():
    """A verified case whose RCA was declared infeasible, with a mitigation
    accepted and not yet verified, and a symptom on record (NEEDS_INFO)."""
    case = _base_case()
    case.solutions = []
    case.root_cause_conclusion = None
    case.problem_verification.rca_infeasible = True
    case.problem_verification.rca_infeasible_rationale = "third-party API outage"
    case.progress.mitigation = MitigationRecord(proposed_at_turn=0, accepted=True)
    return case


def _mitigation_verified() -> InvestigationResponse_Mitigation:
    return InvestigationResponse_Mitigation(
        agent_response="The failover held.",
        state_updates={
            "milestones": MilestoneUpdates(mitigation_verified=True),
            "evidence_to_add": [
                EvidenceToAdd(
                    summary="error rate 0% for 30 minutes after failover",
                    extract="gateway 5xx rate: 0.0 at 14:30-15:00",
                    category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                    source_type=EvidenceSourceType.LOGS,
                    source_file_id=_FILE_ID,
                )
            ],
        },
        evidence_trail=EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(
                mitigation_verified="error rate back to 0 after the failover"
            ),
        ),
    )


def _finding(summary: str, basis: str) -> _DSU:
    return _DSU(
        evidence_to_add=[
            EvidenceToAdd(
                summary=summary,
                extract=summary,
                category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                source_type=EvidenceSourceType.LOGS,
                source_file_id=_FILE_ID,
            )
        ],
        verification_updates=ProblemVerificationUpdate(
            problem_invalidated=True,
            invalidation_evidence_ids=["new_index_0"],
            invalidation_basis=basis,
        ),
    )


class TestAFalseAlarmDeclineKeepsItsFinding:
    async def test_a_finding_recorded_on_the_decline_turn_was_not_declined(self):
        """A substantive "no" on which the model withdraws the finding and
        records another: the decline is of the first finding, so it does not
        bind the second."""
        case = _base_case(problem_status=ProblemStatus.UNVERIFIED)
        case.root_cause_conclusion = None
        case.solutions = []
        engine = _engine()
        offered = await _turn(
            engine,
            case,
            "the gateway log shows no errors at 14:00",
            _finding("No 5xx 13:55-14:05.", "no 5xx in the window"),
        )
        first = case.problem_verification.invalidation.turn

        second = _finding("No 5xx at the edge for 13:00-15:00.", "the edge is clean")
        second.verification_updates.invalidation_withdrawn = True
        second.verification_updates.withdrawal_basis = "the gateway log was wrong"
        card = next(
            f
            for f in offered["suggested_follow_ups"]
            if (f.get("intent") or {}).get("confirmation_value") is False
        )
        await _turn(
            engine,
            case,
            "No - that gateway log is the wrong one, the errors were at the edge; "
            "please re-check the edge log for me",
            second,
            intent_type="confirmation",
            intent_data={"value": False, "proposal_id": card["intent"]["proposal_id"]},
        )

        assert case.problem_verification.invalidation.turn != first, "premise"
        decline = next(a for a in case.offer_record.answers if a.answer == "declined")
        assert decline.premise.finding_turn == first
        assert standing_false_alarm_decline(case) is None
