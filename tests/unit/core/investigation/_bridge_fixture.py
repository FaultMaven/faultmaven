"""Test fixture: the retired Option-1 flat->chain bridge.

``bridge_flat_hypotheses_to_graph`` projected each flat hypothesis into a
degenerate ``root -> D`` chain so the chain engine had a graph to work on before
the LLM emitted real chains. PR B2c removed it from the production flow (the
graph is now LLM-emission-only). It survives here as a TEST FIXTURE: a compact
way to stand up a known degenerate-chain graph for engine-lane tests
(promote/demote) and to exercise the orphan-resolution stub path. Production no
longer auto-projects flat hypotheses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.causal_graph import seed_problem_node
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    NodeEvidenceLink,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


def bridge_flat_hypotheses_to_graph(case: "Case") -> None:
    """Project the case's flat hypotheses into degenerate ``root -> D`` chains.

    Each not-yet-chained ``Hypothesis`` becomes a ROOT node (the hypothesis
    statement) -> the single PROBLEM node ``D``, carrying the hypothesis's
    evidence on the root and left ``CANDIDATE``. Idempotent; no-op until a
    problem statement exists to anchor ``D``.
    """
    problem_node = seed_problem_node(case)
    if problem_node is None:
        return  # nothing to anchor on yet
    d_id = problem_node.node_id

    for hyp in case.hypotheses.values():
        if hyp.root_node_id:
            continue
        root = CausalNode(
            statement=hyp.statement[:500],
            node_type=NodeType.ROOT,
            category=hyp.category,
            generated_at_turn=hyp.generated_at_turn,
            evidence_links=[
                NodeEvidenceLink(
                    evidence_id=link.evidence_id,
                    stance=link.stance,
                    reasoning=link.reasoning,
                    stance_confidence=link.stance_confidence,
                )
                for link in hyp.evidence_links
            ],
        )
        case.causal_nodes[root.node_id] = root
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=root.node_id,
                effect_node_id=d_id,
                created_at_turn=hyp.generated_at_turn,
            )
        )
        hyp.root_node_id = root.node_id
        hyp.path = [root.node_id, d_id]
