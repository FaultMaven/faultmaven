"""Unit tests for ``_apply_hypothesis_updates`` (PR B slice 1).

The LLM's per-turn ``state_updates.hypotheses_to_update`` carries its DIRECT
lifecycle signal — most importantly ``state=REFUTED`` + ``refutation_reason``,
the disconfirmation that drives M6 demotion. The schema and prompt have long
emitted it, but the engine never applied it; these tests pin the wiring and the
end-to-end effect (refutation -> M6 demotion of a grounded cause).
"""

from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import bridge_flat_hypotheses_to_graph
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _recompute_assessment_state,
)
from faultmaven.core.investigation.schemas import HypothesisUpdate
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    ConfidenceLevel,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
)

pytestmark = pytest.mark.unit


def _make_engine() -> MilestoneEngine:
    """Bare engine — only the apply helper is exercised. __init__ takes many
    deps, so bypass it and wire just the hypothesis manager the helper uses."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _empty_metadata() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _make_case() -> Case:
    inquiry = InquiryData(
        proposed_problem_statement="Deploy to on-prem job fails",
        problem_statement_confirmed=True,
        decided_to_investigate=True,
    )
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Deploy fails",
        description="The 'Deploy to on-prem' job is failing",
        state=CaseState.INVESTIGATING,
        inquiry=inquiry,
        problem_verification=ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 7
    return case


def _active_hyp(statement="NetworkPolicy blocks the connection") -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="initial",
        state=HypothesisState.ACTIVE,
        likelihood=0.7,
    )


def test_refuted_with_reason_applies():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {
            h.hypothesis_id: HypothesisUpdate(
                state=HypothesisState.REFUTED,
                refutation_reason="re-ran with policy correct; still failed",
            )
        },
        meta,
        case.current_turn,
    )

    assert h.state == HypothesisState.REFUTED
    assert h.likelihood == 0.0  # canonical refute zeroes it
    assert h.refutation_reason == "re-ran with policy correct; still failed"
    assert h.last_updated_turn == case.current_turn
    assert meta["hypotheses_updated"] == [h.hypothesis_id]


def test_refuted_without_reason_is_skipped_with_feedback():
    # Pair integrity: do not record a disproof on no stated grounds; tell the LLM.
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {h.hypothesis_id: HypothesisUpdate(state=HypothesisState.REFUTED)},
        meta,
        case.current_turn,
    )

    assert h.state == HypothesisState.ACTIVE  # unchanged
    assert meta["hypotheses_updated"] == []
    assert "refutation_reason" in meta.get("system_feedback", "")


def test_validated_transition_is_deferred_noop():
    # Non-REFUTED state transitions are intentionally NOT applied in this slice
    # (cause_state grounds off the RootCauseConclusion, not hypothesis.state).
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {h.hypothesis_id: HypothesisUpdate(state=HypothesisState.VALIDATED)},
        meta,
        case.current_turn,
    )

    assert h.state == HypothesisState.ACTIVE  # deferred: not applied
    assert meta["hypotheses_updated"] == []


def test_refuted_is_terminal_immutable_no_resurrection_or_corruption():
    # REFUTED is terminal: a later state-flip is refused (no resurrection of a
    # disproven cause, no stranded refutation_reason that would fail the model's
    # pair invariant on reload) and surfaced via system_feedback.
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}

    eng._apply_hypothesis_updates(
        case,
        {
            h.hypothesis_id: HypothesisUpdate(
                state=HypothesisState.REFUTED, refutation_reason="disproved"
            )
        },
        _empty_metadata(),
        case.current_turn,
    )
    assert h.state == HypothesisState.REFUTED

    meta2 = _empty_metadata()
    eng._apply_hypothesis_updates(
        case,
        {h.hypothesis_id: HypothesisUpdate(state=HypothesisState.ACTIVE)},
        meta2,
        case.current_turn,
    )

    assert h.state == HypothesisState.REFUTED  # not resurrected
    assert h.refutation_reason == "disproved"  # intact
    assert meta2["hypotheses_updated"] == []
    assert "terminal" in meta2.get("system_feedback", "").lower()
    # The pair stays consistent — the hypothesis still reconstructs cleanly.
    Hypothesis(**h.model_dump())  # must not raise


def test_likelihood_not_applied_to_terminal_hypothesis():
    eng = _make_engine()
    case = _make_case()
    h = Hypothesis(
        statement="dead theory",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="r",
        state=HypothesisState.REFUTED,
        refutation_reason="disproved",
        likelihood=0.0,
    )
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {h.hypothesis_id: HypothesisUpdate(likelihood=0.6)},
        meta,
        case.current_turn,
    )

    assert h.likelihood == 0.0  # unchanged — terminal
    assert meta["hypotheses_updated"] == []
    assert meta.get("system_feedback")


def test_refuted_without_reason_does_not_apply_same_entry_likelihood():
    # {state:REFUTED (no reason), likelihood:0.95}: refutation refused AND the
    # likelihood is NOT applied — the entry was a refutation, not a confidence bump.
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()  # likelihood 0.7
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {
            h.hypothesis_id: HypothesisUpdate(
                state=HypothesisState.REFUTED, likelihood=0.95
            )
        },
        meta,
        case.current_turn,
    )

    assert h.state == HypothesisState.ACTIVE  # not refuted (no reason)
    assert h.likelihood == 0.7  # NOT bumped to 0.95
    assert meta["hypotheses_updated"] == []
    assert meta.get("system_feedback")


def test_likelihood_only_update_applies():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {h.hypothesis_id: HypothesisUpdate(likelihood=0.3)},
        meta,
        case.current_turn,
    )

    assert h.state == HypothesisState.ACTIVE  # unchanged
    assert h.likelihood == 0.3
    assert meta["hypotheses_updated"] == [h.hypothesis_id]


def test_unknown_id_is_skipped_without_error():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        {"hyp_doesnotexist": HypothesisUpdate(likelihood=0.1)},
        meta,
        case.current_turn,
    )

    assert h.likelihood == 0.7  # untouched
    assert meta["hypotheses_updated"] == []


def test_new_index_placeholder_resolves_to_this_turn_hypothesis():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()
    meta["hypotheses_generated"] = [h.hypothesis_id]  # created this turn

    eng._apply_hypothesis_updates(
        case,
        {"new_index_0": HypothesisUpdate(likelihood=0.2)},
        meta,
        case.current_turn,
    )

    assert h.likelihood == 0.2
    assert meta["hypotheses_updated"] == [h.hypothesis_id]


def test_refuted_update_drives_m6_demotion_end_to_end():
    """The whole point of slice 1: the LLM's explicit REFUTED signal now flows
    through to M6 demotion of a grounded cause (the turn-28 path), without
    needing a REFUTES evidence link."""
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="NetworkPolicy denies ingress to postgres",
        mechanism="ingress rule has no from-clause -> default deny",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        validated_hypothesis_id=h.hypothesis_id,
    )
    # Grounded (sticky IDENTIFIED) from a prior turn.
    case.progress.cause_state = CauseState.IDENTIFIED
    case.progress.root_cause_likelihood = 0.9

    # The LLM disconfirms via hypotheses_to_update (not an evidence link).
    eng._apply_hypothesis_updates(
        case,
        {
            h.hypothesis_id: HypothesisUpdate(
                state=HypothesisState.REFUTED,
                refutation_reason="fix applied; problem persisted",
            )
        },
        _empty_metadata(),
        case.current_turn,
    )
    assert h.state == HypothesisState.REFUTED

    _recompute_assessment_state(case)

    assert case.progress.cause_state == CauseState.UNKNOWN
    assert case.root_cause_conclusion is None  # M6 retracted the conclusion
