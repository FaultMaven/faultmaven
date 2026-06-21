"""Unit tests for the transitional flat->graph bridge (Phase 2, Option 1).

The bridge projects a case's flat hypotheses into degenerate root->D chains so
the chain-based engine has a populated graph before the LLM emits chains.
"""

import pytest

from faultmaven.core.investigation.causal_graph import (
    bridge_flat_hypotheses_to_graph,
    demote_disconfirmed_cause,
    promote_grounded_chain_root,
)
from faultmaven.core.investigation.milestone_engine import _recompute_assessment_state
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    ConfidenceLevel,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
    RootCauseConclusion,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _rcc(validated_hypothesis_id=None):
    return RootCauseConclusion(
        root_cause="NetworkPolicy denies ingress to postgres",
        mechanism="ingress rule has no from-clause -> default deny",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        validated_hypothesis_id=validated_hypothesis_id,
    )


def _investigating_case(**overrides) -> Case:
    base = {
        "case_id": f"case_{'0' * 12}",
        "user_id": "user_alpha",
        "organization_id": "org_alpha",
        "title": "Deploy fails",
        "description": "The 'Deploy to on-prem' job is failing",
        "state": CaseState.INVESTIGATING,
        "current_turn": 3,
        "inquiry": InquiryData(
            proposed_problem_statement="Deploy to on-prem job fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        "problem_verification": ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    }
    base.update(overrides)
    return Case(**base)


def _hyp(statement: str, evidence=None) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="initial",
        evidence_links=evidence or [],
    )


def test_bridge_seeds_problem_node_and_root_chains():
    h1 = _hyp("NetworkPolicy blocks the connection")
    h2 = _hyp("collation mismatch")
    case = _investigating_case()
    case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}

    bridge_flat_hypotheses_to_graph(case)

    problem_nodes = [
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    ]
    assert len(problem_nodes) == 1
    d = problem_nodes[0]
    assert d.statement == "Deploy to on-prem job fails"

    roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
    assert len(roots) == 2
    assert all(r.node_state == NodeState.CANDIDATE for r in roots)  # not fabricated

    # Each hypothesis became a root->D chain.
    for h in (h1, h2):
        assert h.root_node_id in case.causal_nodes
        assert h.path == [h.root_node_id, d.node_id]
    # Two edges, both into D.
    assert len(case.causal_edges) == 2
    assert all(e.effect_node_id == d.node_id for e in case.causal_edges)


def test_bridge_maps_hypothesis_evidence_onto_root():
    link = HypothesisEvidenceLink(
        hypothesis_id="hyp_unused000000",
        evidence_id="ev_0123456789ab",
        stance=EvidenceStance.SUPPORTS,
        reasoning="timeout signature",
        stance_confidence=0.9,
    )
    h = _hyp("NetworkPolicy blocks the connection", evidence=[link])
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}

    bridge_flat_hypotheses_to_graph(case)

    root = case.causal_nodes[h.root_node_id]
    assert len(root.evidence_links) == 1
    assert root.evidence_links[0].evidence_id == "ev_0123456789ab"
    assert root.evidence_links[0].stance == EvidenceStance.SUPPORTS


def test_bridge_is_idempotent():
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}

    bridge_flat_hypotheses_to_graph(case)
    nodes_after_first = dict(case.causal_nodes)
    edges_after_first = list(case.causal_edges)

    bridge_flat_hypotheses_to_graph(case)  # second pass

    assert case.causal_nodes.keys() == nodes_after_first.keys()
    assert len(case.causal_edges) == len(edges_after_first)


def test_bridge_noop_without_problem_statement():
    h = _hyp("some hypothesis")
    # INQUIRY case has no problem_verification to anchor D.
    case = Case(
        case_id=f"case_{'1' * 12}",
        user_id="user_alpha",
        organization_id="org_alpha",
        title="t",
        state=CaseState.INQUIRY,
    )
    case.hypotheses = {h.hypothesis_id: h}

    bridge_flat_hypotheses_to_graph(case)

    assert case.causal_nodes == {}
    assert case.causal_edges == []
    assert h.root_node_id is None


# ---------------------------------------------------------------------------
# slice 4: promote_grounded_chain_root (mirror grounding onto the chain)
# ---------------------------------------------------------------------------


def test_promote_validates_linked_root():
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=h.hypothesis_id)

    assert promote_grounded_chain_root(case) is True
    root = case.causal_nodes[h.root_node_id]
    assert root.node_state == NodeState.VALIDATED
    assert root.validation_method == ValidationMethod.EMPIRICAL
    assert root.actionable is True


def test_promote_noop_without_validated_hyp_id():
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=None)  # no link

    assert promote_grounded_chain_root(case) is False
    assert case.causal_nodes[h.root_node_id].node_state == NodeState.CANDIDATE


def test_promote_idempotent():
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=h.hypothesis_id)

    assert promote_grounded_chain_root(case) is True
    assert promote_grounded_chain_root(case) is False  # already validated
    assert case.causal_nodes[h.root_node_id].node_state == NodeState.VALIDATED


def test_promote_noop_without_conclusion():
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    # no root_cause_conclusion at all
    assert promote_grounded_chain_root(case) is False


def test_recompute_grounds_cause_state_and_promotes_root_end_to_end():
    """Integration: a grounded case with a bridged chain + validated_hypothesis_id
    -> _recompute_assessment_state marks IDENTIFIED AND promotes the chain root.
    Exercises the same path the engine wires the bridge into (slice 3)."""
    h = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {h.hypothesis_id: h}
    bridge_flat_hypotheses_to_graph(case)
    # Ground the cause: high-confidence, substantiated conclusion naming the hyp.
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=h.hypothesis_id)

    _recompute_assessment_state(case)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.causal_nodes[h.root_node_id].node_state == NodeState.VALIDATED


# ---------------------------------------------------------------------------
# slice 5: demote_disconfirmed_cause (M6 — the turn-28 fix)
# ---------------------------------------------------------------------------


def _refutes_link(evidence_id="ev_0123456789ab") -> HypothesisEvidenceLink:
    return HypothesisEvidenceLink(
        hypothesis_id="hyp_unused000000",
        evidence_id=evidence_id,
        stance=EvidenceStance.REFUTES,
        reasoning="re-ran with the cause already correct; still failed",
        stance_confidence=1.0,
    )


def _grounded(case) -> None:
    """Mark the case as grounded (sticky IDENTIFIED) — demote's precondition."""
    case.progress.cause_state = CauseState.IDENTIFIED
    case.progress.root_cause_likelihood = 0.9


def test_demote_on_refuted_hypothesis():
    # Trigger A: the LLM refuted the grounded cause's hypothesis.
    hyp = Hypothesis(
        statement="NetworkPolicy blocks the connection",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="x",
        state=HypothesisState.REFUTED,
        refutation_reason="policy was already correct; still failed",
    )
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)
    _grounded(case)

    assert demote_disconfirmed_cause(case) is True
    assert case.causal_nodes[hyp.root_node_id].node_state == NodeState.REFUTED
    # The disconfirmed conclusion is RETRACTED, not left behind (review #2/#3).
    assert case.root_cause_conclusion is None
    assert case.progress.cause_state == CauseState.UNKNOWN
    assert case.progress.root_cause_likelihood == 0.0


def test_demote_on_refutes_evidence():
    # Trigger B: net-refuting evidence (here a lone REFUTES link, no support) is
    # decisive disconfirmation.
    hyp = _hyp("NetworkPolicy blocks the connection", evidence=[_refutes_link()])
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)
    promote_grounded_chain_root(case)  # root was validated a prior turn
    _grounded(case)

    assert demote_disconfirmed_cause(case) is True
    assert hyp.state == HypothesisState.REFUTED
    # Canonical refutation zeroed likelihood + bumped the turn (review #6).
    assert hyp.likelihood == 0.0
    assert hyp.last_updated_turn == case.current_turn
    root = case.causal_nodes[hyp.root_node_id]
    assert root.node_state == NodeState.REFUTED
    # A REFUTED root must not keep advertising its prior validation (review #7).
    assert root.validation_method == ValidationMethod.NONE
    assert root.actionable is False
    assert case.root_cause_conclusion is None


def test_demote_noop_when_active_and_unrefuted():
    hyp = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)
    promote_grounded_chain_root(case)
    _grounded(case)

    assert demote_disconfirmed_cause(case) is False
    assert case.causal_nodes[hyp.root_node_id].node_state == NodeState.VALIDATED
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id


def test_demote_noop_when_not_grounded():
    # Disconfirmation without a grounded (IDENTIFIED) cause is a no-op: there is
    # no sticky cause to un-stick.
    hyp = _hyp("NetworkPolicy blocks the connection", evidence=[_refutes_link()])
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)
    # cause_state left UNKNOWN (default) — not grounded.

    assert demote_disconfirmed_cause(case) is False
    assert case.root_cause_conclusion is not None


def test_demote_does_not_fire_on_minority_refute():
    # Review #4: a lone refuting link among supporting evidence is NOT decisive —
    # a well-supported grounded cause must survive one contrary data point.
    links = [
        HypothesisEvidenceLink(
            hypothesis_id="hyp_unused000000",
            evidence_id=f"ev_00000000000{i}",
            stance=EvidenceStance.SUPPORTS,
            reasoning="supports",
            stance_confidence=0.9,
        )
        for i in range(3)
    ] + [_refutes_link(evidence_id="ev_0123456789ff")]
    hyp = _hyp("NetworkPolicy blocks the connection", evidence=links)
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)
    promote_grounded_chain_root(case)
    _grounded(case)

    assert demote_disconfirmed_cause(case) is False
    assert case.causal_nodes[hyp.root_node_id].node_state == NodeState.VALIDATED
    assert case.root_cause_conclusion is not None
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_demote_fires_on_unnamed_grounding_via_leading_hypothesis():
    # Review #1: the common grounding shape has NO validated_hypothesis_id (as in
    # case_e970a5c24fe1). Demotion still fires, keyed on the strongest hypothesis
    # by initial_likelihood (stable under refutation).
    believed = Hypothesis(
        statement="NetworkPolicy blocks the connection",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="leading theory",
        initial_likelihood=0.9,
        state=HypothesisState.REFUTED,
        refutation_reason="re-ran with policy already correct; still failed",
    )
    alternative = Hypothesis(
        statement="collation mismatch",
        category=HypothesisCategory.CONFIG,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="weak alt",
        initial_likelihood=0.3,
    )
    case = _investigating_case()
    case.hypotheses = {
        believed.hypothesis_id: believed,
        alternative.hypothesis_id: alternative,
    }
    bridge_flat_hypotheses_to_graph(case)
    # Grounded case-wide (evidence_basis) WITHOUT naming a hypothesis.
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="NetworkPolicy denies ingress to postgres",
        mechanism="ingress rule has no from-clause -> default deny",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        evidence_basis=["ev_aaaaaaaaaaaa"],
        validated_hypothesis_id=None,
    )
    _grounded(case)

    assert demote_disconfirmed_cause(case) is True
    assert case.causal_nodes[believed.root_node_id].node_state == NodeState.REFUTED
    assert case.root_cause_conclusion is None
    assert case.progress.cause_state == CauseState.UNKNOWN


def test_turn28_grounded_then_disconfirmed_demotes_end_to_end():
    """Headline fix: a cause that grounds to IDENTIFIED, then is disconfirmed, is
    DEMOTED by the engine — not left standing as 'verified' (case_e970a5c24fe1)."""
    hyp = _hyp("NetworkPolicy blocks the connection")
    case = _investigating_case()
    case.hypotheses = {hyp.hypothesis_id: hyp}
    bridge_flat_hypotheses_to_graph(case)
    case.root_cause_conclusion = _rcc(validated_hypothesis_id=hyp.hypothesis_id)

    # Turn N: grounds, identifies, validates the root.
    _recompute_assessment_state(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.causal_nodes[hyp.root_node_id].node_state == NodeState.VALIDATED

    # Turn N+1: the user disproves it (re-ran, still failing) -> REFUTES evidence.
    hyp.evidence_links.append(_refutes_link())
    _recompute_assessment_state(case)

    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert case.causal_nodes[hyp.root_node_id].node_state == NodeState.REFUTED
    assert hyp.state == HypothesisState.REFUTED
    assert case.root_cause_conclusion is None
