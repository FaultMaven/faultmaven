"""Runbook Cause matcher — per-turn instantiation (increments 4a, 4b-1).

Bridges the structured matcher (``kb_qa.aget_cause_matches`` → ``CauseMatchResult``)
to the case's causal graph: when a retrieved runbook's Cause matches with a single
confident verdict, instantiate that Cause's causal chain as CANDIDATE nodes by
REUSING ``causal_graph.ingest_emitted_chain`` (seed-D, exact-match dedup, ``cn_``
id render-back, edges, never-``VALIDATED``), then attach a hypothesis to the
chain's root (4b-1) so the chain is *load-bearing* — an unattached chain is
invisible to ``cause_state`` / ``any_chain_root_validated`` / RCC synthesis. The
matcher seeds a structural *prior*; everything downstream (``derive_node_states``,
RCC synthesis, the M5 solution gate) then treats these nodes exactly like
LLM-emitted ones — which is what keeps the soundness guarantees automatic.

Flag-gated OFF. The deterministic (step-output) and semantic (``case_evidence_qa``)
resolvers that drive matching are NOT yet wired, so a flag-ON matcher abstains
(verdict 'none') rather than acting — this module is the structural seam,
validated here with fakes. Remaining 4b units: wire those resolvers so matching
fires, and map interventions → ``Solution`` (through the M5 gate).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from faultmaven.core.investigation.causal_graph import (
    chain_path_to_problem,
    ingest_emitted_chain,
)
from faultmaven.core.investigation.cause_schemas import CauseMatchResult, CauseRecord
from faultmaven.core.investigation.schemas import CausalEdgeToAdd, CausalNodeToAdd
from faultmaven.modules.case.domain.models import (
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
    from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
    from faultmaven.modules.case.domain.models import Case, Hypothesis

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


def _root_node_id(case: "Case", created: List[Optional[str]]) -> Optional[str]:
    """The instantiated chain's ROOT node id. Found by node_type (not position),
    so a skipped/deduped node can't misidentify the root. The v4 chain has
    exactly one ROOT."""
    for node_id in created:
        node = case.causal_nodes.get(node_id) if node_id else None
        if node is not None and node.node_type == NodeType.ROOT:
            return node_id
    return None


def attach_matched_hypothesis(
    case: "Case",
    match: CauseMatchResult,
    root_id: str,
    hypothesis_manager: "HypothesisManager",
) -> Optional["Hypothesis"]:
    """Create a hypothesis rooted at the matched chain's root, so the chain is
    *load-bearing* — an unattached chain is invisible to ``cause_state`` /
    ``any_chain_root_validated`` / RCC synthesis (those read standing
    hypotheses, not bare nodes).

    Idempotent: the matcher runs every turn, but ``ingest_emitted_chain`` dedups
    the nodes, so a re-match resolves to the SAME root id; if a hypothesis
    already roots there, do nothing rather than spawn a duplicate.

    The hypothesis is a *prior*: its likelihood seeds ranking only, and its root
    is a CANDIDATE node — ``cause_state`` reaches IDENTIFIED only when that root
    VALIDATES from real evidence (M4/M5). The runbook never concludes on its own.
    """
    if any(h.root_node_id == root_id for h in case.hypotheses.values()):
        return None

    record = match.selected_record
    cause = match.selected_cause
    statement = (
        (record.cause_statement or record.cause_name or "").strip()
        or (cause.cause_name if cause else "")
        or "Runbook-matched cause"
    )
    # belief seeds the prior likelihood (clamped); evidence adjusts it later.
    likelihood = max(0.0, min(1.0, float(cause.belief if cause else 0.5)))
    letter = cause.cause_letter if cause else "?"
    hyp = hypothesis_manager.create_hypothesis(
        statement=statement[:500],
        category=HypothesisCategory.OTHER.value,
        initial_likelihood=likelihood,
        current_turn=case.current_turn,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        state=HypothesisState.ACTIVE,
        rationale=(
            f"Instantiated from runbook {match.runbook_id} (cause {letter}) "
            "matching the case."
        ),
    )
    hyp.root_node_id = root_id
    hyp.path = chain_path_to_problem(root_id, case)
    case.hypotheses[hyp.hypothesis_id] = hyp
    return hyp


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
    hypothesis_manager: Optional["HypothesisManager"] = None,
) -> Optional[CauseMatchResult]:
    """Match retrieved runbooks against the case and instantiate the winner.

    Runs the structured matcher, picks the first runbook with a confident
    single-Cause verdict, instantiates that Cause's chain as CANDIDATE priors,
    and (when ``hypothesis_manager`` is supplied) attaches a hypothesis to the
    chain's root so it becomes load-bearing. Returns the chosen
    ``CauseMatchResult`` (or None if nothing matched confidently). The matcher is
    conservative by construction: 'none'/'multiple' verdicts instantiate nothing,
    leaving attribution to the LLM.

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
    root_id = _root_node_id(case, created)
    attached = None
    if root_id is not None and hypothesis_manager is not None:
        attached = attach_matched_hypothesis(case, chosen, root_id, hypothesis_manager)
    logger.info(
        "Runbook cause matcher: instantiated %d node(s) from runbook %s (cause %s); "
        "hypothesis %s",
        len([c for c in created if c]),
        chosen.runbook_id,
        chosen.selected_cause.cause_letter if chosen.selected_cause else "?",
        attached.hypothesis_id if attached else "none/existing",
    )
    return chosen
