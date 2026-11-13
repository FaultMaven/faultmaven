"""The decider reorder and the final recompute (#2636 B1e, #2615 Q8 (a)).

Within a decision cycle, the backstop and the outcome record read the pool
the final recompute left after every other decider: housekeeping, the stall
counter, the monitor's repair and the need sweeps. A deferred close whose M5
licence fell inside the cycle is lapsed before the narration, logged as
``OFFER_LAPSED``, shows no card (R2), and a model proposal step 2 dropped for
it is reported against the lapse; a deferred resolve stands (R1). No live
LLM: the generator is stubbed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import (
    turn_application,
    turn_completion,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_application import (
    CAUSE_STATE_AT_START_KEY,
    ResponseApplier,
)
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    _NARRATION_OVERCLAIM_NOTICE,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    REPLAYED_DECISION,
    _flag_absence_inside_a_failed_fix_window,
    _flag_unbound_post_fix_rows,
    _refresh_working_conclusion,
    _solution_cause_validated,
    merge_decisions,
    merge_system_notices,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_deferred_close,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _determine_turn_outcome,
)
from faultmaven.core.investigation.narration.codes import DECISIONS_KEY, NoticeCode
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.offers import lapse_offers
from faultmaven.core.investigation.problem_status import (
    decline_revision,
    propose_revision,
)
from faultmaven.core.investigation.progress_monitor import ProgressCheckResult
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    SuggestedFollowUp,
    TerminalResponse,
)
from faultmaven.modules.case.contracts import (
    AnswerVia,
    CaseState,
    CauseState,
    EvidenceCategory,
    EvidenceStance,
    HypothesisCategory,
    HypothesisState,
    NodeEvidenceLink,
    OfferKind,
    ProblemStatus,
    SolutionFeasible,
    TurnOutcome,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation.bound_rows import _row
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
from tests.unit.core.investigation.test_lapsed_offer_b3_2636 import RULED
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
    _identified_case,
)
from tests.unit.core.investigation.test_mitigation_record_2100 import _pending
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _DSU
from tests.unit.core.investigation.test_resolution_backstop_turn import _make_repo

pytestmark = pytest.mark.unit

ORACLE = golden.json.loads(golden.GOLDEN.read_text())


def _responses():
    return ResponseApplier(
        deps=SimpleNamespace(llm_provider=SimpleNamespace(provider_name="stub")),
        kb_prefetcher=SimpleNamespace(prefetch_kb_context=AsyncMock()),
    )


def _cycle_metadata(**extra) -> dict:
    return {
        "milestones_completed": [],
        "hypotheses_validated": [],
        "progress_made": True,
        turn_application.ASSESSMENT_RECOMPUTED_KEY: True,
        **extra,
    }


def _codes(metadata: dict) -> list:
    return [e["code"] for e in metadata.get(DECISIONS_KEY) or []]


# ---------------------------------------------------------------------------
# The final recompute, on its own (ported from d0d2682fa)
# ---------------------------------------------------------------------------


async def test_the_final_recompute_drops_a_fallen_identification():
    """The falling edge (A12): step 1 identified the cause, and the hypothesis
    it rests on left the pool after it (as housekeeping retires one)."""
    case, _root, hyp = _identified_case()
    case.progress.cause_state = CauseState.CANDIDATES  # as the turn began
    step_one = _cycle_metadata()
    _recompute_assessment_state(case, metadata=step_one)
    assert step_one["milestones_completed"] == ["root_cause_identified"]
    step_one["outcome"] = TurnOutcome.MILESTONE_COMPLETED
    hyp.state = HypothesisState.RETIRED
    await turn_application._final_recompute(_responses(), case, step_one)
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert step_one["milestones_completed"] == []
    assert step_one["outcome"] != TurnOutcome.MILESTONE_COMPLETED
    assert step_one["progress_made"] is True


async def test_the_final_recompute_takes_a_rising_edge_as_step_one_does():
    """An identification the final recompute raises records the milestone and
    the validated hypothesis, re-derives the outcome and runs step 1's KB
    warm-up (INV-35)."""
    case, _root, hyp = _identified_case()
    case.progress.cause_state = CauseState.CANDIDATES
    hyp.state = HypothesisState.ACTIVE
    metadata = _cycle_metadata(outcome=TurnOutcome.CONVERSATION)
    responses = _responses()
    await turn_application._final_recompute(responses, case, metadata)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert metadata["milestones_completed"] == ["root_cause_identified"]
    assert metadata["hypotheses_validated"] == [hyp.hypothesis_id]
    assert metadata["outcome"] != TurnOutcome.CONVERSATION
    prefetch = responses.kb_prefetcher.prefetch_kb_context
    prefetch.assert_awaited_once()
    assert prefetch.await_args.args[2] == "root_cause"


async def test_a_hypothesis_validated_then_retired_leaves_the_record():
    """Review fix 2: step 1 validates the hypothesis, a later decider retires
    it; ``hypotheses_validated`` carries the end-of-cycle reading (C3)."""
    case, _root, hyp = _identified_case()
    case.progress.cause_state = CauseState.CANDIDATES
    hyp.state = HypothesisState.ACTIVE
    metadata = _cycle_metadata()
    _recompute_assessment_state(case, metadata=metadata)
    assert metadata["hypotheses_validated"] == [hyp.hypothesis_id]
    hyp.state = HypothesisState.RETIRED
    await turn_application._final_recompute(_responses(), case, metadata)
    assert metadata["hypotheses_validated"] == []


async def test_a_replayed_identification_survives_the_final_recompute():
    """Review fix 1: a replayed bundle's ``root_cause_identified`` (folded by
    ``merge_statement_commit``) with step 1 at CANDIDATES is kept, as
    ``main`` keeps it, and the outcome agrees with the milestones."""
    case, _root, hyp = _identified_case()
    hyp.state = HypothesisState.RETIRED
    case.progress.cause_state = CauseState.CANDIDATES
    metadata = _cycle_metadata(
        milestones_completed=["root_cause_identified"],
        outcome=TurnOutcome.MILESTONE_COMPLETED,
    )
    metadata[CAUSE_STATE_AT_START_KEY] = CauseState.CANDIDATES
    await turn_application._final_recompute(_responses(), case, metadata)
    assert case.progress.cause_state != CauseState.IDENTIFIED, "premise"
    assert metadata["milestones_completed"] == ["root_cause_identified"]
    assert metadata["outcome"] == _determine_turn_outcome(
        case, metadata, TurnOutcome.CONVERSATION
    )


async def test_a_turn_that_dips_and_recovers_identifies_nothing():
    """Review fix 3: the turn starts IDENTIFIED, step 1 dips to CANDIDATES
    (a contest a later decider resolves), and the final recompute recovers:
    no milestone and no second KB prefetch."""
    case, _root, _hyp = _identified_case()
    metadata = _cycle_metadata()
    metadata[CAUSE_STATE_AT_START_KEY] = CauseState.IDENTIFIED
    case.progress.cause_state = CauseState.CANDIDATES  # step 1's dip
    responses = _responses()
    await turn_application._final_recompute(responses, case, metadata)
    assert case.progress.cause_state == CauseState.IDENTIFIED, "premise"
    assert metadata["milestones_completed"] == []
    responses.kb_prefetcher.prefetch_kb_context.assert_not_awaited()


async def test_an_m6_refusal_and_the_grounding_trace_count_once_per_turn():
    """Review fix 4: the final recompute re-runs no per-evaluation counter or
    trace. The identified root carries a decisive counterfactual refute with
    no fix applied, so M6 refuses at each recompute that starts IDENTIFIED."""
    from faultmaven.core.investigation.milestone_engine import cause_state
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _row as _m6_row,
    )

    case, root, _hyp = _identified_case()
    _recompute_assessment_state(case, metadata={})
    row = _m6_row("cf", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE, 5, "errors go on")
    case.evidence.append(row)
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="the limit was raised and the errors go on",
            linked_at_turn=5,
            stance_confidence=0.9,
        )
    )
    case.current_turn = 5
    with patch(
        "faultmaven.core.investigation.causal_graph.disconfirmation."
        "m6_demotion_refused_total"
    ) as counter:
        # Step 1's recompute and the final one, each entered IDENTIFIED.
        _recompute_assessment_state(case.model_copy(deep=True), metadata={})
        _recompute_assessment_state(case, metadata={}, final=True)
    counter.labels.assert_called_once_with(reason="no_fix_applied")
    metadata = _cycle_metadata()
    with patch.object(cause_state, "_log_grounding_assessment") as trace:
        _recompute_assessment_state(case, metadata=metadata)
        await turn_application._final_recompute(_responses(), case, metadata)
    trace.assert_called_once()


def _held_recheck():
    """A fix run at 6; a revision held at 7, on whose turn the user's re-check
    of the problem lands naming nothing it re-checks; the revision declined."""
    case, _root, _hyp = _identified_case()
    _fix(case)
    case.current_turn = 7
    propose_revision(
        case,
        text="checkout returns 503 only under peak load",
        evidence_ids=[case.evidence[0].evidence_id],
        basis="the 503s come back only at peak",
        wording_key="revision:rev_2555",
    )
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("held"),
        summary="checkout answers 200",
    )
    decline_revision(case, via=AnswerVia.CLICK)
    return case


async def test_the_final_recompute_does_not_double_a_step_one_notice():
    case = _held_recheck()
    metadata = _cycle_metadata()
    _recompute_assessment_state(case, metadata=metadata)
    await turn_application._final_recompute(_responses(), case, metadata)
    assert _codes(metadata).count(NoticeCode.UNBOUND_RECHECKS) == 1
    assert len(narrated(case, metadata)) == 1


# ---------------------------------------------------------------------------
# The two flag codes: replaced at the end-of-cycle reading (C5)
# ---------------------------------------------------------------------------


def test_a_flag_whose_pool_moved_renders_one_line_at_the_final_reading():
    """Step 1 flagged the unbound rows; housekeeping then retired a competing
    hypothesis, which moves the rows the line names. The final recompute's
    flag replaces step 1's, token kept; appended, as on ``main``, the two
    readings would narrate the rows twice."""
    case, metadata = golden.unbound_before_housekeeping_retires_a_competitor()
    (step_one,) = metadata[DECISIONS_KEY]
    token, params = step_one["token"], dict(step_one["params"])
    doubled = {**metadata, DECISIONS_KEY: [dict(step_one)]}
    doubled["system_notices"] = list(metadata["system_notices"])
    assert _flag_unbound_post_fix_rows(case, doubled)
    assert len(doubled[DECISIONS_KEY]) == 2, "premise: the readings differ"

    assert _flag_unbound_post_fix_rows(case, metadata, replace=True)
    (entry,) = metadata[DECISIONS_KEY]
    assert entry["token"] == token and entry["params"] != params
    assert entry["params"] == doubled[DECISIONS_KEY][1]["params"]
    (line,) = narrated(case, metadata)
    assert "re-check" in line


def test_a_flag_the_final_reading_does_not_raise_keeps_step_ones():
    case, metadata = golden.unbound_before_housekeeping_retires_a_competitor()
    before = [dict(e) for e in metadata[DECISIONS_KEY]]
    case.progress.problem_status = ProblemStatus.UNVERIFIED  # nothing binds now
    assert not _flag_unbound_post_fix_rows(case, metadata, replace=True)
    assert metadata[DECISIONS_KEY] == before


def test_a_replayed_bundles_flag_is_never_replaced():
    """A replayed bundle's entry is untouched: a replay-plus-live turn renders
    as on ``main`` (its doubling is B2's)."""
    case, bundle = golden.absence_after_a_persistence()
    for entry in bundle[DECISIONS_KEY]:
        entry[REPLAYED_DECISION] = True
    replayed = dict(bundle[DECISIONS_KEY][0])
    live: dict = {}
    _flag_absence_inside_a_failed_fix_window(case, live)
    merge_system_notices(bundle, live)
    merge_decisions(bundle, live)
    _flag_absence_inside_a_failed_fix_window(case, bundle, replace=True)
    assert bundle[DECISIONS_KEY][0] == replayed
    assert golden.cycle_text(narrated(case, bundle)) == (
        ORACLE["cycle:absence_decided_by_a_bundle_and_the_live_apply"]
    )


# ---------------------------------------------------------------------------
# The deferred close's licence lapse (INV-32, R1)
# ---------------------------------------------------------------------------


def _deferred_case():
    """Deferred fix, licensed only by a working conclusion at 0.8."""
    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
    from tests.unit.core.investigation.test_surgical_strip_and_cause_state import (
        TestDeferredImplementationClose,
        _solution,
    )

    case = TestDeferredImplementationClose()._case(
        feasible=SolutionFeasible.DEFERRED,
        solution_proposed=True,
        cause_identified=False,
    )
    case.solutions = [_solution()]
    case.progress.problem_status = ProblemStatus.VERIFIED
    h = HypothesisManager().create_hypothesis(
        statement="the role's trust policy names the wrong OIDC provider",
        category=HypothesisCategory.CONFIG,
        initial_likelihood=0.8,
        current_turn=1,
    )
    h.likelihood = 0.8
    case.hypotheses[h.hypothesis_id] = h
    _refresh_working_conclusion(case)
    assert _solution_cause_validated(case), "premise: licensed"
    _maybe_propose_deferred_close(case, _cycle_metadata())
    assert case.offer_record.open.kind == OfferKind.CLOSE
    return case


def _decay(case) -> None:
    for hypothesis in case.hypotheses.values():
        hypothesis.likelihood = 0.2
    _refresh_working_conclusion(case)


async def test_a_deferred_offer_whose_licence_decayed_lapses_unrendered():
    case = _deferred_case()
    metadata = _cycle_metadata()
    _decay(case)
    await turn_application._final_recompute(_responses(), case, metadata)
    assert case.offer_record.open is None
    (answer,) = case.offer_record.answers
    assert (answer.answer, answer.via) == ("lapsed", "engine")
    assert _codes(metadata) == [NoticeCode.OFFER_LAPSED]
    assert metadata[DECISIONS_KEY][0]["params"] == {"proposal_id": answer.proposal_id}


def test_the_lapse_at_step_3b_is_recorded_and_shows_no_card():
    """C1: shown on turn N, its licence fallen by N+1's step 3b (unreachable
    through ``process_turn``, whose step 0b answers the open offer first)."""
    case = _deferred_case()
    case.current_turn += 1
    _decay(case)
    metadata: dict = {}
    turn_application._lapse_and_record(case, metadata)
    assert case.offer_record.open is None
    assert _codes(metadata) == [NoticeCode.OFFER_LAPSED]
    assert project_offers(case).pair == []


def test_the_lapse_and_the_deferred_opener_read_one_predicate():
    """C6, C9: the lapse and M5 (the deferred opener's gate) read one
    function, ``cause_identification.cause_identification_leg``, and agree."""
    from faultmaven.core.investigation import cause_identification, offers
    from faultmaven.core.investigation.milestone_engine import stage_gates

    leg = cause_identification.cause_identification_leg
    assert offers.cause_identification_leg is leg
    assert stage_gates.cause_identification_leg is leg
    for licensed in (True, False):
        case = _deferred_case()
        if not licensed:
            _decay(case)
        assert _solution_cause_validated(case) is licensed
        assert (lapse_offers(case).open is not None) is licensed


@pytest.mark.parametrize("confirmed", [False, True])
def test_the_save_lapses_a_deferred_close_and_keeps_a_resolve(confirmed):
    """C8: the save-time repeat writes no decision (it takes no log); a
    deferred resolve stands when the licence falls (R1)."""
    case, _metadata = golden._deferred(confirmed=confirmed)
    kind = case.offer_record.open.kind
    assert kind == (OfferKind.RESOLVE if confirmed else OfferKind.CLOSE)
    case.progress.cause_state = CauseState.CANDIDATES
    case.root_cause_conclusion = case.working_conclusion = None
    CaseRepository.prepare_for_save(case)
    assert (case.offer_record.open is not None) is confirmed


def test_the_engine_offer_is_read_at_the_end_of_the_cycle():
    """B3's reader on the lapse this tree makes: step 2 dropped the model's
    ``closed`` for the deferred close, which lapsed later in the cycle."""
    case = _deferred_case()
    metadata = _cycle_metadata()
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    metadata["response_obj"] = MagicMock()
    metadata["response_obj"].state_updates.proposed_transition = MagicMock(
        to_state="closed", evidence_ids=[]
    )
    golden.asyncio.run(
        engine.transitions.check_automatic_transitions(case, metadata, "closed")
    )
    metadata.pop("response_obj")
    assert metadata["transition_superseded_by_engine"] is True
    _decay(case)
    turn_application._lapse_and_record(case, metadata)
    notices = narrated(case, metadata)
    assert RULED.format(to_state="closed") in notices
    assert metadata["transition_dropped_for_lapsed_offer"] is True
    assert "deferred_solution_gate_message" not in metadata


# ---------------------------------------------------------------------------
# Whole turns
# ---------------------------------------------------------------------------


def _deferred_turn_case(*, confirmed: bool):
    """A deferred fix licensed only by a working-conclusion leader at 0.68,
    which housekeeping ages under the bar this turn (``_wc_leg_case``)."""
    case = golden._wc_leg_case(confirmed=confirmed)
    case.progress.solution_feasible = SolutionFeasible.DEFERRED
    case.progress.solution_proposed = True
    return case


async def _turn(monkeypatch, case, response=None, **state_updates) -> tuple:
    """One turn through ``process_turn``: the reply, the metadata as the
    narration step left it, and the ``transition_compliance`` record."""
    seen: dict = {}
    real = turn_completion.narrate_turn

    def _narrate(case, metadata):
        real(case, metadata)
        seen.update(metadata)

    monkeypatch.setattr(turn_completion, "narrate_turn", _narrate)
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=response
        or InvestigationResponse_Diagnosis(
            agent_response="Case resolved.",
            state_updates=_DSU(**state_updates),
            suggested_follow_ups=[
                SuggestedFollowUp(
                    label="Share the role's trust policy",
                    action_type="EVIDENCE",
                    payload="the trust policy JSON",
                )
            ],
        )
    )
    with patch.object(turn_completion.logger, "info") as info:
        result = await engine.process_turn(
            case=case, user_message="it works now", redaction_ctx=None
        )
    compliance = [
        c.kwargs["extra"]
        for c in info.call_args_list
        if c.args == ("transition_compliance",)
    ]
    return result, seen, compliance


async def test_done_when_a_deferred_close_lapsed_in_its_cycle(monkeypatch):
    """The deferred proposer opens a close at step 1, step 2 drops the
    model's ``resolved`` for it, housekeeping ages the licence away, and the
    final recompute lapses the offer. On ``main`` the offer stayed open and
    the drop was reported superseded."""
    case = _deferred_turn_case(confirmed=False)
    result, seen, (extra,) = await _turn(
        monkeypatch, case, proposed_transition={"to_state": "resolved"}
    )
    assert case.offer_record.open is None
    (answer,) = case.offer_record.answers
    assert (answer.kind, answer.basis, answer.answer) == ("close", "deferred", "lapsed")
    assert extra["transition_dropped_for_lapsed_offer"] is True
    assert extra["transition_superseded_by_engine"] is False
    notices = case.turn_history[-1].system_notices
    assert RULED.format(to_state="resolved") in notices
    assert NoticeCode.OFFER_LAPSED in _codes(seen)
    # No card (R2): no prose, no confirmation pair; the model's follow-ups
    # stand and INV-40 corrects its "Case resolved." with the no-offer line.
    assert "deferred_solution_gate_message" not in seen
    assert [f["label"] for f in result["suggested_follow_ups"]] == [
        "Share the role's trust policy"
    ]
    assert result["agent_response"].endswith(_NARRATION_OVERCLAIM_NOTICE)
    record = case.turn_history[-1]
    assert record.milestones_completed == seen["milestones_completed"]
    assert record.outcome == seen["outcome"]


async def test_a_deferred_resolve_stands_when_its_licence_falls(monkeypatch):
    """R1 (passes on ``main`` too, where nothing lapsed it): the case is never
    left without an offer."""
    case = _deferred_turn_case(confirmed=True)
    _result, seen, _ = await _turn(monkeypatch, case)
    offer = case.offer_record.open
    assert (offer.kind, offer.basis) == ("resolve", "deferred")
    assert NoticeCode.OFFER_LAPSED not in _codes(seen)
    assert seen["deferred_solution_gate_message"]


async def test_the_record_reads_the_final_recompute_on_a_falling_edge(monkeypatch):
    """C3: step 1 identifies, housekeeping retires the hypothesis the root
    rests on; the record carries no ``root_cause_identified``, the outcome
    re-derived, and ``progress_made`` stays step 1's (A12)."""
    case, _root, hyp = _identified_case()
    case.current_turn = 5
    case.progress.cause_state = CauseState.CANDIDATES
    real = turn_application._perform_hypothesis_housekeeping

    def _then_retire(*args, **kwargs):
        real(*args, **kwargs)
        hyp.state = HypothesisState.RETIRED

    monkeypatch.setattr(
        turn_application, "_perform_hypothesis_housekeeping", _then_retire
    )
    await _turn(monkeypatch, case)
    record = case.turn_history[-1]
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert "root_cause_identified" not in record.milestones_completed
    assert record.outcome != TurnOutcome.MILESTONE_COMPLETED
    assert record.progress_made is True


async def test_the_pending_check_notice_follows_the_monitors(monkeypatch):
    """Declared: the backstop runs after the progress monitor, so its
    pending-check notice follows the monitor's injection."""
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _engine as _held_engine,
    )
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _turn as _held_turn,
    )

    engine, case = _held_engine(), _held_case()
    monkeypatch.setattr(
        engine.deps.progress_monitor,
        "check_progress",
        lambda _case: ProgressCheckResult(
            transparent_mode=True, prompt_injection="MONITOR INJECTION"
        ),
    )
    await _held_turn(engine, case, 8, "still checking")
    notices = case.turn_history[-1].system_notices
    held = next(i for i, n in enumerate(notices) if n.startswith("RESOLUTION HELD"))
    assert notices.index("MONITOR INJECTION") < held


async def test_momentum_is_what_main_records():
    """C2: metrics stay before the stall counter, so a turn that makes the
    fifth stall records the momentum ``main`` recorded, not this turn's."""
    case, _shown = _pending()
    case.turns_without_progress = 4
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        )
    )
    await engine.process_turn(case=case, user_message="ok", redaction_ctx=None)
    assert case.turns_without_progress == 5
    assert case.turn_history[-1].momentum != "blocked"


@pytest.mark.parametrize("shape", ["inquiry", "terminal", "hold", "investigating"])
async def test_the_final_recompute_runs_exactly_where_step_one_did(monkeypatch, shape):
    """C8: INQUIRY and terminal turns run neither; a hold turn runs the final
    recompute exactly when step 1 recomputed."""
    from faultmaven.core.investigation.milestone_engine import response_application
    from tests.case_builders import inquiry_case

    calls: list = []
    for module, name in (
        (response_application, "_recompute_assessment_state"),
        (turn_application, "_final_recompute"),
    ):
        real = getattr(module, name)

        def _spy(*a, _real=real, _name=name, **k):
            calls.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(module, name, _spy)
    response = None
    if shape == "inquiry":
        case = inquiry_case()
        response = InquiryResponse(agent_response="Noted.", state_updates={})
    elif shape == "terminal":
        case, _shown = _pending()
        case.atomic_update(
            state=CaseState.RESOLVED,
            resolved_at=case.updated_at,
            closed_at=case.updated_at,
        )
        response = TerminalResponse(agent_response="Noted.", state_updates={})
    elif shape == "hold":
        case = _held_recheck()
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
    else:
        case, _shown = _pending()
    await _turn(monkeypatch, case, response)
    step_one = "_recompute_assessment_state" in calls
    assert ("_final_recompute" in calls) is step_one
    assert step_one is (shape in ("hold", "investigating"))
