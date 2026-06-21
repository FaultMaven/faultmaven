"""Unit tests for the pure causal-graph validation primitives (Phase 2, PR A).

Exercises the structural methodology mechanics against hand-built graphs:
M7 AND-proof (symmetric), chain-root validation, and deductive strict-exclusion
(§7.1.1). No engine, no DB, no LLM.
"""

import pytest

from faultmaven.core.investigation.causal_graph import (
    DEDUCTIVE_EXCLUSION_MAX_BELIEF,
    and_constraints_refuted,
    and_constraints_satisfied,
    deductively_validated,
    incoming_and_groups,
    is_chain_root_validated,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    NodeState,
    NodeType,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _node(
    node_id: str,
    *,
    state=NodeState.CANDIDATE,
    node_type=NodeType.INTERMEDIATE,
    belief=0.5,
    refutation_reason=None,
    actionable=False,
    method=ValidationMethod.NONE,
) -> CausalNode:
    # Keep cross-field validators satisfied: VALIDATED needs a method; a
    # VALIDATED root needs actionable; REFUTED needs a reason.
    if state == NodeState.VALIDATED and method == ValidationMethod.NONE:
        method = ValidationMethod.EMPIRICAL
    if state == NodeState.VALIDATED and node_type == NodeType.ROOT:
        actionable = True
    if state == NodeState.REFUTED and not refutation_reason:
        refutation_reason = "refuted in test"
    return CausalNode(
        node_id=node_id,
        statement=f"node {node_id}",
        node_type=node_type,
        node_state=state,
        validation_method=method,
        belief=belief,
        actionable=actionable,
        refutation_reason=refutation_reason,
        generated_at_turn=1,
    )


def _edge(cause: str, effect: str, and_group=None) -> CausalEdge:
    return CausalEdge(cause_node_id=cause, effect_node_id=effect, and_group=and_group)


# ---------------------------------------------------------------------------
# incoming_and_groups
# ---------------------------------------------------------------------------


def test_incoming_and_groups_partitions_by_group():
    edges = [
        _edge("cn_a00000000001", "cn_e00000000001", and_group="g1"),
        _edge("cn_b00000000002", "cn_e00000000001", and_group="g1"),
        _edge("cn_c00000000003", "cn_e00000000001"),  # OR alternative
        _edge("cn_d00000000004", "cn_other0000005"),  # different effect
    ]
    groups = incoming_and_groups("cn_e00000000001", edges)
    assert groups["g1"] == ["cn_a00000000001", "cn_b00000000002"]
    assert groups[None] == ["cn_c00000000003"]


# ---------------------------------------------------------------------------
# M7 AND-proof
# ---------------------------------------------------------------------------


def test_and_satisfied_requires_all_members_validated():
    nodes = {
        "cn_a00000000001": _node("cn_a00000000001", state=NodeState.VALIDATED),
        "cn_b00000000002": _node("cn_b00000000002", state=NodeState.VALIDATED),
        "cn_e00000000001": _node("cn_e00000000001"),
    }
    edges = [
        _edge("cn_a00000000001", "cn_e00000000001", and_group="g1"),
        _edge("cn_b00000000002", "cn_e00000000001", and_group="g1"),
    ]
    assert and_constraints_satisfied("cn_e00000000001", nodes, edges) is True


def test_and_not_satisfied_when_one_member_candidate():
    nodes = {
        "cn_a00000000001": _node("cn_a00000000001", state=NodeState.VALIDATED),
        "cn_b00000000002": _node("cn_b00000000002", state=NodeState.CANDIDATE),
        "cn_e00000000001": _node("cn_e00000000001"),
    }
    edges = [
        _edge("cn_a00000000001", "cn_e00000000001", and_group="g1"),
        _edge("cn_b00000000002", "cn_e00000000001", and_group="g1"),
    ]
    assert and_constraints_satisfied("cn_e00000000001", nodes, edges) is False


def test_no_and_set_is_vacuously_satisfied():
    nodes = {"cn_e00000000001": _node("cn_e00000000001")}
    # only an OR-alternative parent, no conjunction
    edges = [_edge("cn_c00000000003", "cn_e00000000001")]
    assert and_constraints_satisfied("cn_e00000000001", nodes, edges) is True


def test_and_refuted_when_any_member_refuted():
    nodes = {
        "cn_a00000000001": _node("cn_a00000000001", state=NodeState.VALIDATED),
        "cn_b00000000002": _node("cn_b00000000002", state=NodeState.REFUTED),
        "cn_e00000000001": _node("cn_e00000000001"),
    }
    edges = [
        _edge("cn_a00000000001", "cn_e00000000001", and_group="g1"),
        _edge("cn_b00000000002", "cn_e00000000001", and_group="g1"),
    ]
    assert and_constraints_refuted("cn_e00000000001", nodes, edges) is True


def test_or_alternative_refuted_does_not_refute_conjunction():
    nodes = {
        "cn_c00000000003": _node("cn_c00000000003", state=NodeState.REFUTED),
        "cn_e00000000001": _node("cn_e00000000001"),
    }
    edges = [_edge("cn_c00000000003", "cn_e00000000001")]  # OR alternative
    assert and_constraints_refuted("cn_e00000000001", nodes, edges) is False


# ---------------------------------------------------------------------------
# chain-root validation
# ---------------------------------------------------------------------------


def _hyp(root_node_id=None) -> Hypothesis:
    return Hypothesis(
        statement="chain",
        category=HypothesisCategory.NETWORK,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=1,
        rationale="x",
        root_node_id=root_node_id,
        path=[root_node_id] if root_node_id else [],
    )


def test_chain_root_validated_true():
    root = _node("cn_900000000001", state=NodeState.VALIDATED, node_type=NodeType.ROOT)
    nodes = {root.node_id: root}
    assert is_chain_root_validated(_hyp(root.node_id), nodes) is True


def test_chain_root_not_validated_when_candidate():
    root = _node("cn_900000000001", node_type=NodeType.ROOT)
    nodes = {root.node_id: root}
    assert is_chain_root_validated(_hyp(root.node_id), nodes) is False


def test_chain_root_none_returns_false():
    assert is_chain_root_validated(_hyp(None), {}) is False


# ---------------------------------------------------------------------------
# §7.1.1 deductive strict-exclusion
# ---------------------------------------------------------------------------


def _refuted(node_id: str, belief: float) -> CausalNode:
    return _node(node_id, state=NodeState.REFUTED, belief=belief)


def test_deductive_validation_when_all_others_absolutely_excluded():
    survivor = "cn_500000000001"
    nodes = {
        survivor: _node(survivor),
        "cn_600000000002": _refuted("cn_600000000002", 0.0),
        "cn_700000000003": _refuted("cn_700000000003", 0.02),
    }
    assert (
        deductively_validated(survivor, list(nodes.keys()), nodes, exhaustive=True)
        is True
    )


def test_deductive_blocked_without_exhaustiveness():
    survivor = "cn_500000000001"
    nodes = {
        survivor: _node(survivor),
        "cn_600000000002": _refuted("cn_600000000002", 0.0),
    }
    assert (
        deductively_validated(survivor, list(nodes.keys()), nodes, exhaustive=False)
        is False
    )


def test_deductive_blocked_when_sibling_inconclusive():
    survivor = "cn_500000000001"
    nodes = {
        survivor: _node(survivor),
        "cn_600000000002": _refuted("cn_600000000002", 0.0),
        "cn_800000000004": _node("cn_800000000004", state=NodeState.INCONCLUSIVE),
    }
    assert (
        deductively_validated(survivor, list(nodes.keys()), nodes, exhaustive=True)
        is False
    )


def test_deductive_blocked_when_sibling_weakly_refuted():
    survivor = "cn_500000000001"
    # refuted but belief above the strict bar -> not absolutely excluded
    nodes = {
        survivor: _node(survivor),
        "cn_600000000002": _refuted(
            "cn_600000000002", DEDUCTIVE_EXCLUSION_MAX_BELIEF + 0.2
        ),
    }
    assert (
        deductively_validated(survivor, list(nodes.keys()), nodes, exhaustive=True)
        is False
    )


def test_deductive_blocked_with_single_member():
    survivor = "cn_500000000001"
    nodes = {survivor: _node(survivor)}
    assert deductively_validated(survivor, [survivor], nodes, exhaustive=True) is False
