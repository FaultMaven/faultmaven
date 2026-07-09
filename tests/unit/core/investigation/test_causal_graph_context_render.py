"""Unit tests for the ``<causal_graph>`` context block (``_build_causal_graph_block``).

This block closes the node-identity loop: the engine assigns ``cn_...`` ids and
the chain-emission prompt tells the LLM to reference existing ids, but unless the
graph is RENDERED BACK into context with those ids the LLM cannot reference them
and re-states standing causes as duplicate nodes (fragmenting grounding, stalling
cause_state at UNKNOWN). These tests pin the rendered contract: node ids appear
with their [type/state], the "reference these ids" instruction is present, orphan
nodes are surfaced for re-attachment, and REFUTED hypotheses keep their reason
inline (anti-amnesia). See the design in
docs/architecture/investigation-engine/two-dimensional-hypothesis-methodology.md.
"""

import pytest

from faultmaven.core.investigation.causal_graph import seed_problem_node
from faultmaven.core.investigation.prompts.context_builder import (
    _build_causal_graph_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    *,
    statement: str,
    node_type: NodeType = NodeType.ROOT,
    state: NodeState = NodeState.CANDIDATE,
) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=state,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=node_type == NodeType.ROOT,
        generated_at_turn=1,
    )


def _hyp(
    *,
    hypothesis_id: str = "hyp_000000000001",
    statement: str = "connection pool exhausted",
    state: HypothesisState = HypothesisState.ACTIVE,
    root_node_id: str | None = None,
    path: list[str] | None = None,
    refutation_reason: str | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        path=path or [],
        refutation_reason=refutation_reason,
        generated_at_turn=1,
    )


def _case(nodes=None, edges=None, hyps=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="orders failing",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="orders failing", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {n.node_id: n for n in (nodes or [])}
    case.causal_edges = edges or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    return case


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_when_no_hypotheses_or_nodes():
    """Nothing to render yet → empty string (the block is omitted upstream)."""
    assert _build_causal_graph_block(_case()) == ""


def test_renders_node_ids_with_type_and_state():
    """A rooted hypothesis renders its chain nodes by id, tagged [type/state] —
    the loop-closing render that lets the LLM reference rather than re-emit."""
    root = _node("cn_aaaaaaaa0001", statement="deploy pinned a stale DB IP")
    case = _case(nodes=[root], hyps=[_hyp(root_node_id=root.node_id)])

    block = _build_causal_graph_block(case)

    assert block.startswith("<causal_graph>")
    assert block.endswith("</causal_graph>")
    # The reference instruction is the whole point of rendering ids back.
    assert "REFERENCE these cn_... ids" in block
    # The node id is rendered with its type/state so the LLM can target it.
    assert "cn_aaaaaaaa0001 [root/candidate]" in block
    assert "deploy pinned a stale DB IP" in block
    # The hypothesis header carries confidence + state.
    assert "State: active" in block


def test_renders_full_multi_rung_path_excluding_problem_node():
    """The whole declared root→…→D path renders, but the engine-owned PROBLEM
    node D is never emitted as a rung for the LLM to reference."""
    root = _node("cn_aaaaaaaa0001", statement="stale DB IP")
    inter = _node(
        "cn_bbbbbbbb0002",
        statement="connections time out",
        node_type=NodeType.INTERMEDIATE,
    )
    case = _case(nodes=[root, inter])
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=inter.node_id),
        CausalEdge(cause_node_id=inter.node_id, effect_node_id=d.node_id),
    ]
    case.hypotheses = {
        "hyp_000000000001": _hyp(
            root_node_id=root.node_id,
            path=[root.node_id, inter.node_id, d.node_id],
        )
    }

    block = _build_causal_graph_block(case)

    assert "cn_aaaaaaaa0001 [root/candidate]" in block
    assert "cn_bbbbbbbb0002 [intermediate/candidate]" in block
    # D is rendered as the conceptual anchor in prose but never as a cn_ rung.
    assert d.node_id not in block


def test_orphan_nodes_surfaced_for_reattachment():
    """A node on no hypothesis path is surfaced under the 'Unattached causes'
    banner so the LLM extends it instead of re-emitting the same cause."""
    rooted = _node("cn_aaaaaaaa0001", statement="rooted cause")
    orphan = _node("cn_cccccccc0003", statement="floating candidate cause")
    case = _case(
        nodes=[rooted, orphan],
        hyps=[_hyp(root_node_id=rooted.node_id)],
    )

    block = _build_causal_graph_block(case)

    assert "Unattached causes already in the graph" in block
    assert "cn_cccccccc0003 [root/candidate]" in block
    # The orphan banner must NOT swallow the rooted node.
    assert "cn_aaaaaaaa0001 [root/candidate]" in block


def test_refuted_hypothesis_keeps_reason_inline():
    """REFUTED hypotheses render their refutation_reason (Rule 8 anti-amnesia:
    prevents the LLM re-proposing a rejected theory)."""
    root = _node("cn_aaaaaaaa0001", statement="collation warning")
    case = _case(
        nodes=[root],
        hyps=[
            _hyp(
                state=HypothesisState.REFUTED,
                root_node_id=root.node_id,
                refutation_reason="a collation warning cannot cause a connection timeout",
            )
        ],
    )

    block = _build_causal_graph_block(case)

    assert "State: refuted" in block
    assert (
        "Refuted because: a collation warning cannot cause a connection timeout"
        in block
    )


def test_retired_hypotheses_excluded():
    """RETIRED hypotheses are dropped from the active view (only their nodes, if
    standalone, would surface as orphans)."""
    root = _node("cn_aaaaaaaa0001", statement="retired theory cause")
    case = _case(
        hyps=[
            _hyp(
                statement="a retired theory",
                state=HypothesisState.RETIRED,
                root_node_id=root.node_id,
            )
        ],
    )
    # No nodes registered and the only hypothesis is retired → nothing to show.
    block = _build_causal_graph_block(case)
    assert block == ""


def test_count_held_root_carries_recovery_annotation():
    """INV-29 elicitation: a ROOT held only by the independent-support bar is
    annotated with its recovery action — without it the model re-records the
    same datum (mirror-collapsed) and stalls."""
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
        NodeEvidenceLink,
    )

    root = _node(
        "cn_00000000ee1d",
        statement="undersized connection pool exhausts under load",
        state=NodeState.INCONCLUSIVE,
    )
    root.evidence_links = [
        NodeEvidenceLink(
            evidence_id="ev_" + "a" * 12,
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on the root",
            linked_at_turn=2,
        )
    ]
    hyp = _hyp(hypothesis_id="hyp_00000000ee1d", root_node_id=root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    case.evidence = [
        Evidence(
            evidence_id="ev_" + "a" * 12,
            summary="config diff shows pool max_size dropped to 5",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="llm",
            collected_at_turn=2,
            collected_at=datetime.now(timezone.utc),
        )
    ]
    block = _build_causal_graph_block(case)
    held_line = next(line for line in block.splitlines() if "cn_00000000ee1d" in line)
    assert "SECOND INDEPENDENT causal observation" in held_line

    # Control: a plain candidate root (no causal support) is NOT annotated.
    bare = _node("cn_00000000ba2e", statement="a bare unsupported cause")
    bare_hyp = _hyp(hypothesis_id="hyp_00000000ba2e", root_node_id=bare.node_id)
    control = _case(nodes=[bare], hyps=[bare_hyp])
    control_block = _build_causal_graph_block(control)
    bare_line = next(
        line for line in control_block.splitlines() if "cn_00000000ba2e" in line
    )
    assert "SECOND INDEPENDENT" not in bare_line


def test_hedged_only_root_gets_confident_link_recovery_note():
    """The hedged slice gets ITS recovery action — a CONFIDENT causal link —
    not the second-observation note (which would be factually wrong: one more
    observation still leaves zero qualifying supports)."""
    from datetime import datetime, timezone

    from faultmaven.modules.case.contracts import (
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        EvidenceStance,
        NodeEvidenceLink,
    )

    root = _node(
        "cn_00000000fed9",
        statement="undersized connection pool exhausts under load",
        state=NodeState.INCONCLUSIVE,
    )
    root.evidence_links = [
        NodeEvidenceLink(
            evidence_id="ev_" + "c" * 12,
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on the root",
            linked_at_turn=2,
            stance_confidence=0.4,  # self-hedged
        )
    ]
    hyp = _hyp(hypothesis_id="hyp_00000000fed9", root_node_id=root.node_id)
    case = _case(nodes=[root], hyps=[hyp])
    case.evidence = [
        Evidence(
            evidence_id="ev_" + "c" * 12,
            summary="config diff shows pool max_size dropped to 5",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="llm",
            collected_at_turn=2,
            collected_at=datetime.now(timezone.utc),
        )
    ]
    block = _build_causal_graph_block(case)
    line = next(ln for ln in block.splitlines() if "cn_00000000fed9" in ln)
    assert "CONFIDENT causal observation" in line
    assert "SECOND INDEPENDENT" not in line
