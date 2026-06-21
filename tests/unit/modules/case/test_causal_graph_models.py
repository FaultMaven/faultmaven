"""Unit tests for the causal-graph domain models (Two-Dimensional Hypothesis
Methodology, slice 1a).

These pin the schema-layer backstops for the methodology invariants:
- M4: a node is VALIDATED only with a real validation_method (never asserted).
- M1: a ROOT node cannot be VALIDATED unless it is actionable.
- M7: AND-sets are expressed via (effect_node_id, and_group) on edges.
- refutation_reason ↔ node_state=REFUTED pairing (mirror of Hypothesis).

Design: docs/architecture/investigation-engine/
        two-dimensional-hypothesis-methodology.md
"""

import pytest
from pydantic import ValidationError

from faultmaven.modules.case.domain.models import (
    CausalEdge,
    CausalNode,
    EvidenceStance,
    HypothesisCategory,
    InterventionQuadrant,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Enum vocabulary
# ---------------------------------------------------------------------------


def test_enum_values():
    assert {t.value for t in NodeType} == {"problem", "intermediate", "root"}
    assert {s.value for s in NodeState} == {
        "candidate",
        "validated",
        "refuted",
        "inconclusive",
    }
    assert {m.value for m in ValidationMethod} == {"none", "empirical", "deductive"}
    assert {q.value for q in InterventionQuadrant} == {
        "remediation",
        "defensive_fix",
        "mitigation",
        "loop_break",
    }


# ---------------------------------------------------------------------------
# CausalNode — construction & identity
# ---------------------------------------------------------------------------


def test_problem_node_minimal():
    n = CausalNode(
        statement="Deploy to on-prem job fails",
        node_type=NodeType.PROBLEM,
        generated_at_turn=0,
    )
    assert n.node_id.startswith("cn_")
    assert len(n.node_id) == len("cn_") + 12
    assert n.node_state == NodeState.CANDIDATE
    assert n.validation_method == ValidationMethod.NONE
    assert n.signature_consistent is True
    assert n.actionable is False
    assert n.evidence_links == []


def test_node_id_pattern_enforced():
    with pytest.raises(ValidationError):
        CausalNode(
            node_id="bad-id",
            statement="x",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=1,
        )


def test_statement_not_whitespace_only():
    with pytest.raises(ValidationError):
        CausalNode(
            statement="   ",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=1,
        )


# ---------------------------------------------------------------------------
# M4 — validation is never asserted (VALIDATED requires a method)
# ---------------------------------------------------------------------------


def test_m4_validated_without_method_rejected():
    with pytest.raises(
        ValidationError, match="VALIDATED node requires validation_method"
    ):
        CausalNode(
            statement="DB unreachable on 5432",
            node_type=NodeType.INTERMEDIATE,
            node_state=NodeState.VALIDATED,
            validation_method=ValidationMethod.NONE,
            generated_at_turn=2,
        )


@pytest.mark.parametrize(
    "method", [ValidationMethod.EMPIRICAL, ValidationMethod.DEDUCTIVE]
)
def test_m4_validated_with_method_ok(method):
    n = CausalNode(
        statement="DB unreachable on 5432",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.VALIDATED,
        validation_method=method,
        generated_at_turn=2,
    )
    assert n.node_state == NodeState.VALIDATED
    assert n.validation_method == method


def test_candidate_node_keeps_method_none():
    n = CausalNode(
        statement="maybe a NetworkPolicy blocks ingress",
        node_type=NodeType.ROOT,
        generated_at_turn=3,
        category=HypothesisCategory.NETWORK,
    )
    assert n.node_state == NodeState.CANDIDATE
    assert n.validation_method == ValidationMethod.NONE


# ---------------------------------------------------------------------------
# M1 — a VALIDATED ROOT must be actionable
# ---------------------------------------------------------------------------


def test_m1_validated_root_without_actionable_rejected():
    with pytest.raises(ValidationError, match="VALIDATED ROOT node must be actionable"):
        CausalNode(
            statement="NetworkPolicy denies ingress to postgres",
            node_type=NodeType.ROOT,
            node_state=NodeState.VALIDATED,
            validation_method=ValidationMethod.EMPIRICAL,
            actionable=False,
            generated_at_turn=4,
        )


def test_m1_validated_root_actionable_ok():
    n = CausalNode(
        statement="NetworkPolicy denies ingress to postgres",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        actionable=True,
        generated_at_turn=4,
    )
    assert n.node_type == NodeType.ROOT
    assert n.actionable is True


def test_m1_does_not_constrain_intermediate_nodes():
    # M1 is about ROOT designation; an intermediate node may be validated
    # without being actionable.
    n = CausalNode(
        statement="connection attempt times out",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        actionable=False,
        generated_at_turn=2,
    )
    assert n.node_state == NodeState.VALIDATED


# ---------------------------------------------------------------------------
# refutation_reason ↔ REFUTED pairing
# ---------------------------------------------------------------------------


def test_refuted_requires_reason():
    with pytest.raises(ValidationError, match="refutation_reason is required"):
        CausalNode(
            statement="collation mismatch causes the timeout",
            node_type=NodeType.ROOT,
            node_state=NodeState.REFUTED,
            generated_at_turn=5,
        )


def test_refutation_reason_without_refuted_rejected():
    with pytest.raises(ValidationError, match="only valid when node_state=REFUTED"):
        CausalNode(
            statement="collation mismatch causes the timeout",
            node_type=NodeType.ROOT,
            node_state=NodeState.CANDIDATE,
            refutation_reason="signature-impossible: a warning can't cause a timeout",
            generated_at_turn=5,
        )


def test_refuted_with_reason_ok():
    n = CausalNode(
        statement="collation mismatch causes the timeout",
        node_type=NodeType.ROOT,
        node_state=NodeState.REFUTED,
        refutation_reason="re-run failed with NetworkPolicy already correct",
        generated_at_turn=5,
    )
    assert n.node_state == NodeState.REFUTED


# ---------------------------------------------------------------------------
# NodeEvidenceLink
# ---------------------------------------------------------------------------


def test_node_evidence_link():
    link = NodeEvidenceLink(
        evidence_id="ev_0123456789ab",
        stance=EvidenceStance.REFUTES,
        reasoning="re-run failed though the policy was already correct",
        linked_at_turn=28,
    )
    assert link.stance == EvidenceStance.REFUTES
    assert link.stance_confidence == 1.0


def test_node_evidence_link_confidence_bounds():
    with pytest.raises(ValidationError):
        NodeEvidenceLink(
            evidence_id="ev_0123456789ab",
            stance=EvidenceStance.SUPPORTS,
            reasoning="x",
            stance_confidence=1.5,
        )


# ---------------------------------------------------------------------------
# CausalEdge — M7 AND grouping + structural integrity
# ---------------------------------------------------------------------------


def test_edge_minimal_or_alternative():
    e = CausalEdge(
        cause_node_id="cn_000000000001",
        effect_node_id="cn_000000000002",
        created_at_turn=3,
    )
    assert e.edge_id.startswith("ce_")
    assert e.and_group is None  # independent / OR alternative cause


def test_edge_and_group_marks_conjunction():
    # Two co-necessary causes of the same effect share (effect, and_group).
    slow_db = CausalEdge(
        cause_node_id="cn_aaaaaaaaaaaa",
        effect_node_id="cn_deadline0001",
        and_group="g1",
    )
    aggressive_timeout = CausalEdge(
        cause_node_id="cn_bbbbbbbbbbbb",
        effect_node_id="cn_deadline0001",
        and_group="g1",
    )
    assert slow_db.and_group == aggressive_timeout.and_group == "g1"
    assert slow_db.effect_node_id == aggressive_timeout.effect_node_id


def test_edge_self_loop_rejected():
    with pytest.raises(ValidationError, match="cannot connect a node to itself"):
        CausalEdge(
            cause_node_id="cn_000000000001",
            effect_node_id="cn_000000000001",
        )


def test_edge_id_pattern_enforced():
    with pytest.raises(ValidationError):
        CausalEdge(
            edge_id="nope",
            cause_node_id="cn_000000000001",
            effect_node_id="cn_000000000002",
        )
