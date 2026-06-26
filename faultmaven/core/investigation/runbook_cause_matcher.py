"""Runbook Cause matcher — per-turn instantiation (increment 4a).

Bridges the structured matcher (``kb_qa.aget_cause_matches`` → ``CauseMatchResult``)
to the case's causal graph: when a retrieved runbook's Cause matches with a single
confident verdict, instantiate that Cause's causal chain as CANDIDATE nodes by
REUSING ``causal_graph.ingest_emitted_chain`` (seed-D, exact-match dedup, ``cn_``
id render-back, edges, never-``VALIDATED``). The matcher seeds a structural
*prior*; everything downstream (``derive_node_states``, RCC synthesis, the M5
solution gate) then treats these nodes exactly like LLM-emitted ones — which is
what keeps the soundness guarantees automatic.

Flag-gated OFF until increment 5. The deterministic (step-output) and semantic
(``case_evidence_qa``) resolvers that drive matching are supplied by the caller;
in 4a they are not yet wired, so a flag-ON matcher abstains (verdict 'none')
rather than acting — this module is the structural seam, validated here with
fakes. Mapping interventions → ``Solution`` is increment 4b.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from faultmaven.core.investigation.causal_graph import ingest_emitted_chain
from faultmaven.core.investigation.cause_schemas import CauseMatchResult, CauseRecord
from faultmaven.core.investigation.schemas import CausalEdgeToAdd, CausalNodeToAdd
from faultmaven.modules.case.domain.models import NodeType

if TYPE_CHECKING:
    from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
    from faultmaven.modules.case.domain.models import Case

logger = logging.getLogger(__name__)

# Distinct runbooks the matcher evaluates per turn.
_DEFAULT_MAX_RUNBOOKS = 3


def chain_to_specs(
    cause: CauseRecord,
) -> Tuple[List[CausalNodeToAdd], List[CausalEdgeToAdd]]:
    """Convert a Cause's chain into ``ingest_emitted_chain`` spec shapes.

    - The PROBLEM node (D) is engine-seeded, so it is dropped from the node
      specs; any ref to it maps to the literal ``'D'`` token ingest understands.
    - Every other chain node becomes a ``CausalNodeToAdd``; its ref maps to
      ``'new_index_N'`` (its position in the node-spec list).
    - Edge endpoints map through that table; an edge with an unresolvable
      endpoint is dropped (ingest would skip it anyway).
    """
    nodes: List[CausalNodeToAdd] = []
    ref_token: Dict[str, str] = {}
    for node in cause.chain_nodes:
        ref = str(node.get("ref", "")).strip()
        ntype_raw = str(node.get("node_type", "")).strip().lower()
        # D is engine-seeded; never emit it, just record its ref → 'D'.
        if ntype_raw == NodeType.PROBLEM.value or ref == "D":
            if ref:
                ref_token[ref] = "D"
            continue
        statement = str(node.get("statement", "")).strip()
        if not statement:
            continue
        # A duplicate ref would overwrite the earlier node's token and misdirect
        # any edge pointing at it — skip the duplicate rather than mis-wire.
        if ref and ref in ref_token:
            logger.warning(
                "Duplicate chain ref %r in cause %s; skipping duplicate node",
                ref,
                cause.cause_letter,
            )
            continue
        try:
            node_type = NodeType(ntype_raw)
        except ValueError:
            node_type = NodeType.INTERMEDIATE
        # An unreferenced (empty-ref) node is still a valid node; just keep it out
        # of the ref table (no edge can target it).
        if ref:
            ref_token[ref] = f"new_index_{len(nodes)}"
        nodes.append(CausalNodeToAdd(statement=statement, node_type=node_type))

    edges: List[CausalEdgeToAdd] = []
    for edge in cause.chain_edges:
        cause_tok = ref_token.get(str(edge.get("cause_ref", "")).strip())
        effect_tok = ref_token.get(str(edge.get("effect_ref", "")).strip())
        if cause_tok and effect_tok:
            edges.append(CausalEdgeToAdd(cause=cause_tok, effect=effect_tok))
    return nodes, edges


def instantiate_cause_chain(
    case: "Case", cause: CauseRecord, current_turn: int
) -> List[Optional[str]]:
    """Instantiate ``cause``'s chain into ``case`` via ``ingest_emitted_chain``.

    Returns the created node ids (empty when the chain has no instantiable node,
    e.g. a degenerate Cause carrying only the problem node)."""
    nodes, edges = chain_to_specs(cause)
    if not nodes:
        return []
    return ingest_emitted_chain(case, nodes, edges, [], current_turn)


async def apply_runbook_cause_matcher(
    case: "Case",
    *,
    kb_tool,
    resolve_causes,
    evaluator: "IndicatorEvaluator",
    question: str,
    user_id: str,
    team_ids: Optional[List[str]] = None,
    max_runbooks: int = _DEFAULT_MAX_RUNBOOKS,
) -> Optional[CauseMatchResult]:
    """Match retrieved runbooks against the case and instantiate the winner.

    Runs the structured matcher, picks the first runbook with a confident
    single-Cause verdict, and instantiates that Cause's chain as CANDIDATE
    priors. Returns the chosen ``CauseMatchResult`` (or None if nothing matched
    confidently). The matcher is conservative by construction: 'none'/'multiple'
    verdicts instantiate nothing, leaving attribution to the LLM.

    A *prior, not a gate*: the engine caller wraps this so it can never break a
    turn.
    """
    matches = await kb_tool.aget_cause_matches(
        question,
        user_id,
        resolve_causes=resolve_causes,
        evaluator=evaluator,
        team_ids=team_ids,
        max_runbooks=max_runbooks,
    )
    chosen = next(
        (m for m in matches if m.verdict == "single" and m.selected_record is not None),
        None,
    )
    if chosen is None:
        return None

    created = instantiate_cause_chain(case, chosen.selected_record, case.current_turn)
    logger.info(
        "Runbook cause matcher: instantiated %d node(s) from runbook %s (cause %s)",
        len([c for c in created if c]),
        chosen.runbook_id,
        chosen.selected_cause.cause_letter if chosen.selected_cause else "?",
    )
    return chosen
