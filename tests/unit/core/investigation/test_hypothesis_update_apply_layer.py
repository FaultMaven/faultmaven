"""Unit tests for ``_apply_hypothesis_updates`` (PR B slice 1).

The LLM's per-turn ``state_updates.hypotheses_to_update`` carries its DIRECT
lifecycle signal — most importantly ``state=REFUTED`` + ``refutation_reason``,
the disconfirmation that drives M6 demotion. The schema and prompt have long
emitted it, but the engine never applied it; these tests pin the wiring and the
end-to-end effect (refutation -> M6 demotion of a grounded cause).
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_deferred_likelihood_updates,
    _apply_hypothesis_updates,
    _apply_pending_refutations,
)
from faultmaven.core.investigation.schemas import HypothesisUpdate
from faultmaven.modules.case.contracts import (
    Case,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    NodeEvidenceLink,
    RootCauseConclusion,
)
from tests.case_builders import investigating_case
from tests.utils import bridge_flat_hypotheses_to_graph

pytestmark = pytest.mark.unit

_UPDATES = "faultmaven.core.investigation.milestone_engine.hypothesis_updates"


def _make_engine() -> MilestoneEngine:
    """Bare engine — only the apply helper is exercised. __init__ takes many
    deps, so bypass it and wire just the hypothesis manager the helper uses."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.deps = EngineDeps()
    eng.deps.hypothesis_manager = HypothesisManager()
    return eng


def _empty_metadata() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "evidence_slots": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _make_case() -> Case:
    return investigating_case(
        user_id="user_test",
        enterprise_id="org_test",
        title="Deploy fails",
        description="The 'Deploy to on-prem' job is failing",
        statement="Deploy to on-prem job fails",
        symptom="Deploy to on-prem job fails",
        current_turn=7,
    )


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


def _refuting_row(case: Case, h: Hypothesis, *, confidence=1.0) -> str:
    """Record a file-backed row on the case and link it REFUTES to ``h``."""
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            summary="policy re-applied correctly; the job still fails",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id="file_0123456789ab",
            collected_by="llm",
            collected_at_turn=case.current_turn,
        )
    )
    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id=ev_id,
            stance=EvidenceStance.REFUTES,
            reasoning="the policy was correct and the job still failed",
            stance_confidence=confidence,
        )
    )
    return ev_id


def _refute(eng, case, h, meta, reason="re-ran with policy correct; still failed"):
    """Emit ``state=REFUTED`` and judge it as the apply path does: queued by
    ``_apply_hypothesis_updates``, applied after the link passes."""
    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [
            HypothesisUpdate(
                hypothesis_id=h.hypothesis_id,
                state=HypothesisState.REFUTED,
                refutation_reason=reason,
            )
        ],
        meta,
        case.current_turn,
    )
    _apply_pending_refutations(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )


def test_refuted_with_refuting_evidence_on_record_applies():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    ev_id = _refuting_row(case, h)
    meta = _empty_metadata()

    _refute(eng, case, h, meta)

    assert h.state == HypothesisState.REFUTED
    assert h.likelihood == 0.0  # canonical refute zeroes it
    assert h.refutation_reason == "re-ran with policy correct; still failed"
    assert h.last_updated_turn == case.current_turn
    assert meta["hypotheses_updated"] == [h.hypothesis_id]
    assert [link.evidence_id for link in h.evidence_links] == [ev_id]


def test_refuted_on_a_reason_alone_is_refused_with_feedback():
    """#1911: a stated reason is an argument, not a disproof. With no row on
    record refuting it, the hypothesis stays open and the model is told why."""
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    with patch(f"{_UPDATES}.hypothesis_refutation_refused_total") as counter:
        _refute(eng, case, h, meta, reason="all nodes run the same code")

    assert h.state == HypothesisState.ACTIVE
    assert h.refutation_reason is None
    assert h.likelihood == 0.7
    assert meta["hypotheses_updated"] == []
    feedback = meta["system_feedback"]
    assert f"Hypothesis {h.hypothesis_id} was NOT refuted" in feedback
    assert "ask for the check that would settle it" in feedback
    counter.labels.assert_called_once_with(source="model")
    counter.labels.return_value.inc.assert_called_once_with()


def test_a_self_hedged_refutes_link_does_not_back_a_refutation():
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    _refuting_row(case, h, confidence=0.4)
    meta = _empty_metadata()

    _refute(eng, case, h, meta)

    assert h.state == HypothesisState.ACTIVE
    assert "self-hedged" in meta["system_feedback"]


def test_a_refutes_link_on_the_chain_root_backs_the_refutation():
    """The ground may sit on the chain axis: a row linked REFUTES to the
    hypothesis's root node, which ``refute_hypothesis`` then mirrors flat."""
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    ev_id = _refuting_row(case, h)
    h.evidence_links.clear()
    case.causal_nodes[h.root_node_id].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=ev_id,
            stance=EvidenceStance.REFUTES,
            reasoning="the policy was correct and the job still failed",
            linked_at_turn=case.current_turn,
        )
    )
    meta = _empty_metadata()

    _refute(eng, case, h, meta)

    assert h.state == HypothesisState.REFUTED
    assert [link.evidence_id for link in h.evidence_links] == [ev_id]


def test_refuted_without_reason_is_skipped_with_feedback():
    # Pair integrity: do not record a disproof on no stated grounds; tell the LLM.
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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
    _refuting_row(case, h)

    _refute(eng, case, h, _empty_metadata(), reason="disproved")
    assert h.state == HypothesisState.REFUTED

    meta2 = _empty_metadata()
    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.3)],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [HypothesisUpdate(hypothesis_id="new_index_0", likelihood=0.2)],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )

    assert h.likelihood == 0.2
    assert meta["hypotheses_updated"] == [h.hypothesis_id]


def test_refuted_update_drives_m6_demotion_end_to_end():
    """The whole point of slice 1: the LLM's explicit REFUTED signal flows
    through to M6 demotion of a grounded cause (the turn-28 path). One refuting
    row against two supporting ones does not net-refute the cause by itself,
    so the demotion here is the refutation's doing; the row is what lets the
    refutation stand (#1911)."""
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    for _ in range(2):
        supporting = _refuting_row(case, h)
        h.evidence_links[-1].stance = EvidenceStance.SUPPORTS
        assert h.evidence_links[-1].evidence_id == supporting
    _refuting_row(case, h)
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

    # The LLM disconfirms via hypotheses_to_update.
    _refute(eng, case, h, _empty_metadata(), reason="fix applied; problem persisted")
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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.9)],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )

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

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.9)],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )

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
    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
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
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )
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
    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.45)],
        meta,
        case.current_turn,
    )
    # Step 4 (links pass) auto-refutes it.
    eng.deps.hypothesis_manager.refute_hypothesis(
        hypothesis=h,
        current_turn=case.current_turn,
        refuting_evidence_ids=[],
        reason="two refuting links this turn",
    )
    assert h.state == HS.REFUTED
    assert h.likelihood == 0.0

    # Step 4a-bis: the deferred apply must skip it with feedback.
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )
    assert h.likelihood == 0.0  # not resurrected
    assert "immutable" in meta.get("system_feedback", "")


def test_duplicate_entries_for_one_hypothesis_are_applied_once():
    """A list, unlike the ``Dict[str, HypothesisUpdate]`` it replaced, can name
    the same hypothesis twice (fm#1057).

    Both entries applied means the SECOND ``update_hypothesis_likelihood`` reads
    the value the first just wrote, measures no change, and charges
    ``iterations_without_progress`` — stagnation recorded on a turn that made
    progress, which is what feeds the deadlock/exhaustion repair path. The
    counter is the observable: the likelihood alone looks correct either way.
    """
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()  # likelihood 0.7
    h.iterations_without_progress = 0
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()

    # A DOWNWARD move: the B1 evidence-free cap is a ceiling on raises only, so
    # this lands unclamped and is unambiguously progress (|delta| = 0.3).
    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [
            HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.4),
            HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.4),
        ],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )

    assert h.iterations_without_progress == 0, (
        "the duplicate entry was applied a second time and recorded stagnation "
        "on a turn that made progress"
    )
    assert h.last_progress_at_turn == case.current_turn


def test_an_id_and_its_new_index_alias_collapse_to_one_entry():
    """Dedup resolves BEFORE comparing: ``new_index_0`` and the id it resolves
    to are the same hypothesis, so two entries spelled differently must not
    both be applied."""
    eng = _make_engine()
    case = _make_case()
    h = _active_hyp()  # likelihood 0.7
    h.iterations_without_progress = 0
    case.hypotheses = {h.hypothesis_id: h}
    meta = _empty_metadata()
    meta["hypotheses_generated"] = [h.hypothesis_id]

    _apply_hypothesis_updates(
        eng.deps.hypothesis_manager,
        case,
        [
            HypothesisUpdate(hypothesis_id="new_index_0", likelihood=0.4),
            HypothesisUpdate(hypothesis_id=h.hypothesis_id, likelihood=0.4),
        ],
        meta,
        case.current_turn,
    )
    _apply_deferred_likelihood_updates(
        eng.deps.hypothesis_manager, case, meta, case.current_turn
    )

    assert h.iterations_without_progress == 0
