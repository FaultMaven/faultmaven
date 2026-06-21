"""Unit tests for the transitional flat->graph bridge (Phase 2, Option 1).

The bridge projects a case's flat hypotheses into degenerate root->D chains so
the chain-based engine has a populated graph before the LLM emits chains.
"""

import pytest

from faultmaven.core.investigation.causal_graph import bridge_flat_hypotheses_to_graph
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


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
