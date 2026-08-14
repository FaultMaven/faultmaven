"""Unit tests for ``_apply_hypothesis_updates`` (PR B slice 1).

The LLM's per-turn ``state_updates.hypotheses_to_update`` carries its DIRECT
lifecycle signal — most importantly ``state=REFUTED`` + ``refutation_reason``,
the disconfirmation that drives M6 demotion. The schema and prompt have long
emitted it, but the engine never applied it; these tests pin the wiring and the
end-to-end effect (refutation -> M6 demotion of a grounded cause).
"""

from uuid import uuid4

import pytest

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
from tests.utils import bridge_flat_hypotheses_to_graph

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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id,
                state=HypothesisState.REFUTED,
                refutation_reason="re-ran with policy correct; still failed",
            )
        ],
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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id, state=HypothesisState.REFUTED
            )
        ],
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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id, state=HypothesisState.VALIDATED
            )
        ],
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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id,
                state=HypothesisState.REFUTED,
                refutation_reason="disproved",
            )
        ],
        _empty_metadata(),
        case.current_turn,
    )
    assert h.state == HypothesisState.REFUTED

    meta2 = _empty_metadata()
    eng._apply_hypothesis_updates(
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, state=HypothesisState.ACTIVE)],
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
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.6)],
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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id,
                state=HypothesisState.REFUTED,
                likelihood=0.95,
            )
        ],
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
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.3)],
        meta,
        case.current_turn,
    )
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)

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
        [HypothesisUpdate(hypothesis_id="hyp_doesnotexist", likelihood=0.1)],
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
        [HypothesisUpdate(hypothesis_id="new_index_0", likelihood=0.2)],
        meta,
        case.current_turn,
    )
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)

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
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id,
                state=HypothesisState.REFUTED,
                refutation_reason="fix applied; problem persisted",
            )
        ],
        _empty_metadata(),
        case.current_turn,
    )
    assert h.state == HypothesisState.REFUTED

    _recompute_assessment_state(case)

    assert case.progress.cause_state == CauseState.UNKNOWN
    assert case.root_cause_conclusion is None  # M6 retracted the conclusion


def test_evidence_free_likelihood_update_capped_with_feedback():
    """INV-29 companion (#573 B1) at the apply layer: an over-cap likelihood on
    an evidence-free hypothesis is capped by the canonical mutator AND the LLM
    is told why via system_feedback (the recovery is to link evidence, not to
    re-assert a larger number)."""
    from faultmaven.core.investigation.hypothesis_manager import (
        NEW_HYPOTHESIS_MAX_PRIOR,
    )

    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()  # no evidence links
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.9)],
        meta,
        case.current_turn,
    )
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)

    # Ceiling semantics: the cap refuses the RAISE but never demotes below
    # the current earned value (0.7 here), so the applied value is unchanged.
    assert h.likelihood == 0.7
    assert "capped" in meta.get("system_feedback", "")
    assert meta["hypotheses_updated"] == [h.hypothesis_id]


def test_supported_likelihood_update_no_cap_feedback():
    """Control: with a supporting link the update applies untouched and no cap
    feedback is emitted."""
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id="ev_" + "0" * 12,
            stance=EvidenceStance.SUPPORTS,
            reasoning="linked",
            stance_confidence=0.9,
        )
    )
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    eng._apply_hypothesis_updates(
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.9)],
        meta,
        case.current_turn,
    )
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)

    assert h.likelihood == 0.9
    assert "capped" not in meta.get("system_feedback", "")


def test_same_turn_link_then_likelihood_is_not_capped():
    """Ordering pin (#573 B1): the prompt mandates record -> link -> set in ONE
    turn. Likelihood application is deferred until after the links pass, so a
    compliant single-turn emission is NOT capped and gets no gaslighting
    feedback."""
    from faultmaven.modules.case.contracts import (
        EvidenceStance,
        HypothesisEvidenceLink,
    )

    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()  # no links yet — they arrive "later this turn"
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    # Step 3b: the likelihood update is stashed, not applied.
    eng._apply_hypothesis_updates(
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.9)],
        meta,
        case.current_turn,
    )
    assert h.likelihood == 0.7  # untouched so far

    # Step 4: the same turn's evidence link lands.
    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id="ev_" + "0" * 12,
            stance=EvidenceStance.SUPPORTS,
            reasoning="linked this turn",
            stance_confidence=0.9,
        )
    )

    # Step 4a-bis: the deferred update now sees the link — no cap.
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)
    assert h.likelihood == 0.9
    assert "capped" not in meta.get("system_feedback", "")


def test_deferred_apply_respects_same_turn_refutation():
    """Terminal immutability re-checked at APPLY time: a hypothesis
    auto-REFUTED by the same turn's links pass must not have the stale
    stashed likelihood applied on top of its refutation."""
    from faultmaven.modules.case.contracts import HypothesisState as HS

    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    # Step 3b: stash while still ACTIVE.
    eng._apply_hypothesis_updates(
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.45)],
        meta,
        case.current_turn,
    )
    # Step 4 (links pass) auto-refutes it.
    eng.hypothesis_manager.refute_hypothesis(
        hypothesis=h,
        current_turn=case.current_turn,
        refuting_evidence_ids=[],
        reason="two refuting links this turn",
    )
    assert h.state == HS.REFUTED
    assert h.likelihood == 0.0

    # Step 4a-bis: the deferred apply must skip it with feedback.
    eng._apply_deferred_likelihood_updates(case, meta, case.current_turn)
    assert h.likelihood == 0.0  # not resurrected
    assert "immutable" in meta.get("system_feedback", "")
