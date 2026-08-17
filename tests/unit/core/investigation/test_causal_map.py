"""Causal-map serialization for terminal reports.

The map is a pure function of the persisted causal graph — engine-derived,
never LLM-authored — so these tests pin the two properties that make it safe
to embed in a final report: it renders only when it informs (assurance +
structural gates), and what it renders is deterministic, well-formed mermaid
that never over-claims (solid arrows only from VALIDATED causes).

Fixtures build the real graph shape for each grade rather than patching the
assurance gate (the test_resolution_assurance_note pattern).
"""

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.causal_map import (
    MAX_NODES,
    render_causal_map,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CausalEdge,
    CausalNode,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _hex12(seed: int) -> str:
    return f"{seed:012x}"


def _root_node(
    seed: int, statement: str, *, validated: bool = False, turn: int = 1
) -> CausalNode:
    kwargs = {}
    if validated:
        kwargs = {
            "node_state": NodeState.VALIDATED,
            "validation_method": ValidationMethod.EMPIRICAL,
            "actionable": True,
            "evidence_links": [
                NodeEvidenceLink(
                    evidence_id="ev_aaaaaaaaaaaa",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="observed directly",
                    linked_at_turn=turn,
                )
            ],
        }
    return CausalNode(
        node_id=f"cn_{_hex12(seed)}",
        statement=statement,
        node_type=NodeType.ROOT,
        generated_at_turn=turn,
        **kwargs,
    )


def _graded_case(
    nodes: list[CausalNode],
    edges: list[CausalEdge],
) -> Case:
    """A RESOLVED case carrying the given graph.

    The assurance grade follows the graph: a VALIDATED root grades
    MECHANISTIC (the map's minimum bar); an all-candidate graph leaves the
    stated conclusion at NO_ROOT.
    """
    case = Case(
        case_id="case_aa0000000001",
        user_id="user_x",
        organization_id="org_x",
        title="Checkout timeouts",
        description="p99 spikes on checkout.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
        inquiry=InquiryData(
            proposed_problem_statement="p99 spikes on checkout",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhausted",
        mechanism="pool saturation queues requests past the timeout",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    case.evidence = [
        Evidence(
            evidence_id="ev_aaaaaaaaaaaa",
            summary="pool metrics show saturation",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="u",
            collected_at_turn=1,
            collected_at=datetime(2026, 8, 17, 11, 0, 0, tzinfo=UTC),
        )
    ]
    case.causal_nodes = {n.node_id: n for n in nodes}
    case.causal_edges = list(edges)
    terminal_at = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", terminal_at)
    object.__setattr__(case, "closed_at", terminal_at)
    return case


def _three_node_graph() -> tuple[list[CausalNode], list[CausalEdge]]:
    problem = CausalNode(
        node_id=f"cn_{_hex12(0xD)}",
        statement="API requests time out",
        node_type=NodeType.PROBLEM,
        generated_at_turn=0,
    )
    root = _root_node(0xA, "connection pool exhausted", validated=True, turn=1)
    intermediate = CausalNode(
        node_id=f"cn_{_hex12(0xB)}",
        statement="requests queue behind saturated pool",
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=2,
    )
    edges = [
        CausalEdge(
            edge_id=f"ce_{_hex12(1)}",
            cause_node_id=root.node_id,
            effect_node_id=intermediate.node_id,
            created_at_turn=1,
        ),
        CausalEdge(
            edge_id=f"ce_{_hex12(2)}",
            cause_node_id=intermediate.node_id,
            effect_node_id=problem.node_id,
            created_at_turn=2,
        ),
    ]
    return [problem, root, intermediate], edges


# ---------------------------------------------------------------- rendering


def test_renders_fenced_mermaid_for_established_cause():
    nodes, edges = _three_node_graph()
    fenced = render_causal_map(_graded_case(nodes, edges))
    assert fenced is not None
    assert fenced.startswith("```mermaid\nflowchart LR\n")
    assert fenced.endswith("\n```")
    # Problem anchor is a stadium with no state glyph; others carry glyphs.
    assert '(["API requests time out"])' in fenced
    assert '["✓ connection pool exhausted"]' in fenced
    assert '["○ requests queue behind saturated pool"]' in fenced


def test_solid_arrow_only_from_validated_cause():
    nodes, edges = _three_node_graph()
    fenced = render_causal_map(_graded_case(nodes, edges))
    lines = fenced.splitlines()
    solid = [ln for ln in lines if " --> " in ln]
    dotted = [ln for ln in lines if " -.-> " in ln]
    # validated root -> intermediate is solid; candidate intermediate -> D
    # stays dotted (the map never over-claims).
    assert len(solid) == 1 and len(dotted) == 1


def test_refuted_node_renders_struck_glyph():
    nodes, edges = _three_node_graph()
    refuted = CausalNode(
        node_id=f"cn_{_hex12(0xC)}",
        statement="network partition to the database",
        node_type=NodeType.ROOT,
        node_state=NodeState.REFUTED,
        refutation_reason="timeouts, not connection refusals",
        generated_at_turn=3,
    )
    edges = edges + [
        CausalEdge(
            edge_id=f"ce_{_hex12(3)}",
            cause_node_id=refuted.node_id,
            effect_node_id=nodes[0].node_id,
            created_at_turn=3,
        )
    ]
    fenced = render_causal_map(_graded_case(nodes + [refuted], edges))
    assert '["✗ network partition to the database"]' in fenced


def test_output_is_deterministic_across_insertion_order():
    nodes, edges = _three_node_graph()
    first = render_causal_map(_graded_case(nodes, edges))
    second = render_causal_map(_graded_case(list(reversed(nodes)), edges[::-1]))
    assert first == second


def test_labels_are_sanitized_and_truncated():
    nodes, edges = _three_node_graph()
    nodes[2] = CausalNode(
        node_id=nodes[2].node_id,
        statement='queue <depth> exceeds "safe" `limit` & ' + "x" * 120,
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=2,
    )
    fenced = render_causal_map(_graded_case(nodes, edges))
    assert "&lt;depth&gt;" in fenced
    assert "'safe'" in fenced
    assert "&amp;" in fenced
    assert "…" in fenced
    assert '"safe"' not in fenced
    assert "`" not in fenced.replace("```mermaid", "").replace("```", "")
    # No label line exceeds the cap by more than the id/shape scaffolding.
    assert all(len(ln) < 150 for ln in fenced.splitlines())


def test_duplicate_edges_render_once():
    nodes, edges = _three_node_graph()
    edges = edges + [
        CausalEdge(
            edge_id=f"ce_{_hex12(9)}",
            cause_node_id=edges[0].cause_node_id,
            effect_node_id=edges[0].effect_node_id,
            created_at_turn=5,
        )
    ]
    fenced = render_causal_map(_graded_case(nodes, edges))
    arrow_lines = [ln for ln in fenced.splitlines() if "->" in ln]
    assert len(arrow_lines) == 2


# ---------------------------------------------------------------- gating


def test_no_map_when_cause_not_established():
    # Same topology, but no node ever reached VALIDATED: the conclusion is
    # stated, not established (NO_ROOT) — the map must not render.
    nodes, edges = _three_node_graph()
    nodes[1] = _root_node(0xA, "connection pool exhausted", validated=False, turn=1)
    case = _graded_case(nodes, edges)
    assert render_causal_map(case) is None


def test_no_map_for_trivial_graph():
    nodes, edges = _three_node_graph()
    # Two nodes / one edge: a restated sentence, not a map.
    assert render_causal_map(_graded_case([nodes[0], nodes[1]], [edges[0]])) is None


def test_no_map_without_problem_anchor():
    nodes, edges = _three_node_graph()
    assert render_causal_map(_graded_case(nodes[1:], edges)) is None


def test_no_map_when_graph_too_dense():
    nodes, edges = _three_node_graph()
    extra = [
        _root_node(0x100 + i, f"filler cause {i}", turn=4) for i in range(MAX_NODES)
    ]
    assert render_causal_map(_graded_case(nodes + extra, edges)) is None


def test_no_map_when_edges_dangle():
    nodes, edges = _three_node_graph()
    dangling = [
        CausalEdge(
            edge_id=e.edge_id,
            cause_node_id=f"cn_{_hex12(0xEE)}",
            effect_node_id=e.effect_node_id,
            created_at_turn=e.created_at_turn,
        )
        for e in edges
    ]
    assert render_causal_map(_graded_case(nodes, dangling)) is None
