"""Runbook Cause matcher — per-turn instantiation (increments 4a, 4b-1, 4b-2).

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

Matching fires on the **T2 semantic tier** (``case_evidence_qa`` over the case's
vectorized evidence — wired in 4b-2). The T1 deterministic tier stays inert:
FaultMaven investigates uploaded evidence rather than executing a runbook's
numbered diagnostic steps, so there is no per-step output to resolve (the spec
frames T1 as an opportunistic fast-path, T2 as the canonical robustness floor).

The matched Cause's documented fixes (``interventions``) are stashed on the chain
ROOT node's metadata (4b-3); the prompt context surfaces them as a
``<documented_fixes>`` block ONLY once that cause is established
(``cause_state == IDENTIFIED``), so the LLM proposes them and they flow through
the M5 gate normally. The matcher never creates ``Solution`` objects directly —
that would bypass M5. Still flag-gated OFF until increment 5.
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

# A matched runbook seeds a PRIOR, not a conclusion. Cap its hypothesis
# likelihood strictly below the 0.6 "cause identified" threshold
# (``working_conclusion.likelihood >= 0.6`` → ``terminal_transitions._cause_identified``,
# which gates the M5 solution gate and resolution readiness) so a runbook match
# ALONE can never make FM conclude the cause. Real evidence lifts it past 0.6.
_MATCHER_MAX_PRIOR = 0.5

# Prefix on a matcher hypothesis's ``rationale``. It doubles as the per-case
# skip-guard signal (``is_runbook_match_hypothesis``): the matcher runs every
# turn, so it must recognize its own prior cheaply — and ``rationale`` PERSISTS
# (a ``hypotheses`` column) where a fresh model field would not, so the guard
# survives the case being reloaded between turns.
RUNBOOK_MATCH_RATIONALE_PREFIX = "Instantiated from runbook"


def is_runbook_match_hypothesis(hyp: "Hypothesis") -> bool:
    """True if ``hyp`` was seeded by the runbook Cause matcher (its rationale
    carries the matcher's marker prefix). The per-case skip-guard signal."""
    return str(getattr(hyp, "rationale", "") or "").startswith(
        RUNBOOK_MATCH_RATIONALE_PREFIX
    )


# Budget for the raw-evidence fallback text (chars). Case evidence is curated and
# bounded (summary ≤500 + optional extract per row), so this comfortably holds
# a typical case's evidence while capping the classifier prompt on large cases.
_FALLBACK_EVIDENCE_MAX_CHARS = 8000


def build_case_evidence_fallback_text(
    case: "Case", max_chars: int = _FALLBACK_EVIDENCE_MAX_CHARS
) -> Optional[str]:
    """Assemble the T2 raw-evidence fallback from ``case.evidence``.

    The matcher's T2 tier judges each rung indicator against the case's
    *vectorized* evidence. But case evidence is vectorized only in DA mode and
    above a size gate, so small/conversational evidence is frequently absent from
    the index — leaving T2 nothing to retrieve and the matcher unable to fire
    (issue #543). This renders the already-recorded ``Evidence`` rows (the LLM's
    claim-anchored ``summary`` + optional verbatim ``extract``) into a compact
    text block the tool can judge against when the vector collection is empty.

    Returns ``None`` when the case has no evidence (the tool then abstains, i.e.
    keeps the conservative pre-fallback behavior). Output is truncated to
    ``max_chars`` (whole rows; a partial final row is dropped) so the classifier
    prompt stays bounded on evidence-heavy cases.
    """
    evidence = getattr(case, "evidence", None) or []
    if not evidence:
        return None

    parts: List[str] = []
    used = 0
    for ev in evidence:
        summary = (getattr(ev, "summary", "") or "").strip()
        if not summary:
            continue
        category = getattr(getattr(ev, "category", None), "value", "") or ""
        extract = (getattr(ev, "extract", "") or "").strip()
        header = f"[{category}] {summary}" if category else summary
        block = f"{header}\n{extract}" if extract else header
        # Stop at the budget on a whole-row boundary (never emit a partial row).
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block) + 2  # +2 for the joiner
    if not parts:
        return None
    return "\n\n".join(parts)


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


# Node-metadata key under which the matched runbook's documented fixes are
# stashed on the chain ROOT node, for the prompt context to surface once the
# cause is established (see context_builder._build_documented_fixes_block).
RUNBOOK_INTERVENTIONS_META_KEY = "runbook_interventions"


def _stash_interventions(
    case: "Case", root_id: str, record: Optional[CauseRecord]
) -> None:
    """Stash the matched Cause's interventions (documented fixes) on its ROOT
    node's metadata. No-op when there are none. ``node.metadata`` persists (a
    JSON column), so the fixes survive reload until the cause is established."""
    interventions = list(record.interventions) if record else []
    if not interventions:
        return
    node = case.causal_nodes.get(root_id)
    if node is None:
        return
    # Symmetric with the reader's ``node.metadata or {}`` — never assume the dict.
    if not node.metadata:
        node.metadata = {}
    node.metadata[RUNBOOK_INTERVENTIONS_META_KEY] = interventions


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

    The hypothesis is a *prior*: its likelihood is capped below the
    "cause identified" gate (see ``_MATCHER_MAX_PRIOR``) and its root is a
    CANDIDATE node — ``cause_state`` reaches IDENTIFIED only when that root
    VALIDATES from real evidence (M4/M5). The runbook never concludes on its own.
    """
    if any(h.root_node_id == root_id for h in case.hypotheses.values()):
        return None

    # Only attach to a chain that actually materialized a root → D path. A
    # disconnected chain (malformed runbook edges, or a lone root never linked
    # to D) would otherwise yield a hypothesis with root_node_id set and an
    # empty path — an inconsistent record (latent path[0] failures). Skip it;
    # the bare nodes remain, inert.
    path = chain_path_to_problem(root_id, case)
    if not path:
        logger.warning(
            "Matched chain root %s has no path to D; skipping hypothesis", root_id
        )
        return None

    record = match.selected_record
    cause = match.selected_cause
    statement = (
        (record.cause_statement or record.cause_name or "").strip()
        or (cause.cause_name if cause else "")
        or "Runbook-matched cause"
    )
    # A runbook match is a PRIOR, never a conclusion. Cap the likelihood below
    # the 0.6 "cause identified" threshold (working_conclusion → _cause_identified,
    # which gates M5 + resolution) so a runbook ALONE can never make FM conclude
    # the cause. Only real evidence — the LLM raising the likelihood, or the root
    # VALIDATING — lifts it past the gate.
    belief = float(cause.belief if cause else 0.0)
    likelihood = min(max(0.0, belief), _MATCHER_MAX_PRIOR)
    letter = cause.cause_letter if cause else "?"
    hyp = hypothesis_manager.create_hypothesis(
        statement=statement[:500],
        category=HypothesisCategory.OTHER.value,
        initial_likelihood=likelihood,
        current_turn=case.current_turn,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        state=HypothesisState.ACTIVE,
        # The prefix is the persisted skip-guard signal (see
        # is_runbook_match_hypothesis) — keep it leading.
        rationale=(
            f"{RUNBOOK_MATCH_RATIONALE_PREFIX} {match.runbook_id} "
            f"(cause {letter}) matching the case."
        ),
    )
    # path before root_node_id so the lenient root==path[0] invariant holds at
    # every intermediate state.
    hyp.path = path
    hyp.root_node_id = root_id
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
    if root_id is not None:
        # Stash the runbook's documented fixes on the root node so the prompt
        # context can surface them ONCE this cause is established (4b-3). Stored
        # here (not re-resolved later) because context building is sync; the
        # node.metadata JSON column round-trips. The matcher never creates
        # Solutions — the LLM proposes the fix and the M5 gate governs it.
        _stash_interventions(case, root_id, chosen.selected_record)
        if hypothesis_manager is not None:
            attached = attach_matched_hypothesis(
                case, chosen, root_id, hypothesis_manager
            )
    logger.info(
        "Runbook cause matcher: instantiated %d node(s) from runbook %s (cause %s); "
        "hypothesis %s",
        len([c for c in created if c]),
        chosen.runbook_id,
        chosen.selected_cause.cause_letter if chosen.selected_cause else "?",
        attached.hypothesis_id if attached else "none/existing",
    )
    return chosen
