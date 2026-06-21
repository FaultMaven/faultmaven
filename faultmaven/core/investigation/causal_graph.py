"""Pure causal-graph mechanics for the Two-Dimensional Hypothesis Methodology.

Operates on a case's causal graph — ``causal_nodes`` (dict ``node_id -> CausalNode``)
and ``causal_edges`` (list of ``CausalEdge``) — with NO I/O and NO LLM. These are
the engine-side validation primitives the ``cause_state`` derivation, the
failed-treatment demotion, and (later) the prompt-driven chain emission all build
on. Keeping them pure makes the methodology's load-bearing invariants
unit-testable against hand-built graphs.

Spec: docs/architecture/investigation-engine/two-dimensional-hypothesis-methodology.md
  - §0 invariants (M4 empirical/deductive validation, M7 AND-proof)
  - §7.1 / §7.1.1 (empirical vs deductive validation, strict exclusion)

This slice covers the STRUCTURAL primitives (AND-proof, chain-root validation,
deductive strict-exclusion). Belief propagation (§6.1 / §9.4) is a follow-on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis

# §7.1.1 guard 3: a sibling counts as "excluded" only when its refutation is
# ABSOLUTE — REFUTED and belief at/under this bar. A merely-inconclusive or
# weakly-refuted sibling does NOT count, so deductive validation cannot fire on
# partial exclusion.
DEDUCTIVE_EXCLUSION_MAX_BELIEF = 0.05


# ---------------------------------------------------------------------------
# Edge / AND-set helpers
# ---------------------------------------------------------------------------


def incoming_and_groups(
    node_id: str, edges: list[CausalEdge]
) -> dict[str | None, list[str]]:
    """Group the *direct causes* of ``node_id`` by their ``and_group``.

    Returns ``{and_group_key: [cause_node_id, ...]}``. Edges sharing the same
    ``(effect_node_id, and_group)`` are co-necessary (an AND-set, M7). A
    ``None`` key collects the independent (OR-alternative) direct causes — each
    is its own sufficient cause, not part of a conjunction.
    """
    groups: dict[str | None, list[str]] = {}
    for e in edges:
        if e.effect_node_id == node_id:
            groups.setdefault(e.and_group, []).append(e.cause_node_id)
    return groups


def _state(node_id: str, nodes: dict[str, CausalNode]) -> NodeState | None:
    n = nodes.get(node_id)
    return n.node_state if n else None


# ---------------------------------------------------------------------------
# M7 — AND-gate proof (symmetric: strict to prove, asymmetric to refute)
# ---------------------------------------------------------------------------


def and_constraints_refuted(
    node_id: str, nodes: dict[str, CausalNode], edges: list[CausalEdge]
) -> bool:
    """M7 disproof (asymmetric): refuting ANY one co-necessary member refutes
    the conjunction — the node cannot occur via that AND-path.

    Returns True if any member of any AND-set feeding ``node_id`` is REFUTED.
    OR-alternative causes (``and_group is None``) are independent and do not
    count — one of them being refuted doesn't break the others.
    """
    for and_group, cause_ids in incoming_and_groups(node_id, edges).items():
        if and_group is None:
            continue  # OR alternatives, not a conjunction
        if any(_state(cid, nodes) == NodeState.REFUTED for cid in cause_ids):
            return True
    return False


def and_constraints_satisfied(
    node_id: str, nodes: dict[str, CausalNode], edges: list[CausalEdge]
) -> bool:
    """M7 proof (symmetric, strict): every co-necessary member of every AND-set
    feeding ``node_id`` must be VALIDATED.

    A node with no AND-sets (only OR-alternative parents, or no parents) is
    vacuously satisfied — its own evidence governs it (M4), not a conjunction.
    While any AND-member is still a candidate/inconclusive, this returns False:
    the node cannot be considered conjunctively established.
    """
    for and_group, cause_ids in incoming_and_groups(node_id, edges).items():
        if and_group is None:
            continue
        if not all(_state(cid, nodes) == NodeState.VALIDATED for cid in cause_ids):
            return False
    return True


# ---------------------------------------------------------------------------
# Chain-level validation (what cause_state=IDENTIFIED reads)
# ---------------------------------------------------------------------------


def is_chain_root_validated(
    hypothesis: Hypothesis, nodes: dict[str, CausalNode]
) -> bool:
    """A chain (hypothesis) grounds ``cause_state=IDENTIFIED`` only when its
    ROOT node exists and is VALIDATED (methodology §9.2). The root is the top of
    the ``root -> ... -> D`` path; M3 requires it to be set before validation.
    """
    root_id = hypothesis.root_node_id
    if not root_id:
        return False
    return _state(root_id, nodes) == NodeState.VALIDATED


# ---------------------------------------------------------------------------
# §7.1.1 — deductive validation (proof by exclusion, strict)
# ---------------------------------------------------------------------------


def deductively_validated(
    survivor_id: str,
    or_set_ids: list[str],
    nodes: dict[str, CausalNode],
    *,
    exhaustive: bool,
) -> bool:
    """Validate ``survivor_id`` by exclusion (§7.1.1): if its OR-set has ``N``
    mutually-exclusive members and the other ``N-1`` are ABSOLUTELY excluded,
    the survivor is validated by deduction — even if it cannot be observed.

    Strict guards (all required):

    - ``exhaustive`` — the OR-set must be certified collectively exhaustive
      (family-completeness sweep passed). Proof-by-exclusion over an incomplete
      differential concludes the wrong survivor; the caller asserts this.
    - the survivor must be a member of ``or_set_ids`` and there must be ≥2
      members (with one survivor you have learned nothing by exclusion).
    - every non-survivor must be ABSOLUTELY excluded: ``REFUTED`` AND
      ``belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF``. A merely inconclusive or
      weakly-refuted sibling blocks the deduction (the survivor stays a
      candidate — graceful denial).

    This is binary by design — it backs an invariant (M4), not a probabilistic
    estimate. Deductive validation is *mechanistic* grade only (§7.2): it
    unlocks treatment but counterfactual confirmation is still required to
    resolve.
    """
    if not exhaustive:
        return False
    members = list(dict.fromkeys(or_set_ids))  # dedup, preserve order
    if survivor_id not in members or len(members) < 2:
        return False
    for cid in members:
        if cid == survivor_id:
            continue
        node = nodes.get(cid)
        if node is None:
            return False
        if node.node_state != NodeState.REFUTED:
            return False
        if (node.belief or 0.0) > DEDUCTIVE_EXCLUSION_MAX_BELIEF:
            return False
    return True


# ---------------------------------------------------------------------------
# TRANSITIONAL bridge (Option-1): project flat hypotheses onto the graph
# ---------------------------------------------------------------------------


def bridge_flat_hypotheses_to_graph(case: Case) -> None:
    """Populate the causal graph from the case's *flat* hypotheses so the
    chain-based engine (cause_state-over-chains, M6 demotion) has a graph to
    work on WITHOUT the LLM emitting chains yet.

    **Transitional.** This bridge is removed once the prompt contract (PR B)
    makes the LLM emit real multi-rung chains directly. Each flat ``Hypothesis``
    becomes a degenerate 2-node chain — a ROOT node (the hypothesis statement)
    → the single PROBLEM node ``D`` — carrying the hypothesis's evidence on the
    root. Projected roots are left ``CANDIDATE``: validation/demotion is the
    engine's job (a later slice promotes a *grounded* root, fabricating the
    actionable/method that the flat model doesn't track — kept out of this pure
    structural projection on purpose).

    Idempotent: a hypothesis that already carries a ``root_node_id`` is skipped,
    and the PROBLEM node is created at most once. No-op until a problem
    statement exists to anchor ``D``.
    """
    # 1. Ensure the single PROBLEM node D (seeded from the confirmed problem).
    problem_node = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem_node is None:
        pv = case.problem_verification
        statement = pv.symptom_statement if pv else None
        if not statement or not statement.strip():
            return  # nothing to anchor on yet
        problem_node = CausalNode(
            statement=statement[:500],
            node_type=NodeType.PROBLEM,
            generated_at_turn=case.current_turn,
        )
        case.causal_nodes[problem_node.node_id] = problem_node
    d_id = problem_node.node_id

    # 2. One root→D chain per not-yet-bridged flat hypothesis.
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


def promote_grounded_chain_root(case: "Case") -> bool:
    """Mirror the engine's case-wide grounding onto the chain graph: when a
    SPECIFIC hypothesis is named the validated cause
    (``RootCauseConclusion.validated_hypothesis_id``), promote that chain's ROOT
    node to VALIDATED so the graph reflects what the flat grounding already
    knows. Returns True if a root was promoted.

    Transitional (Option-1) and deliberately conservative: it fires ONLY on an
    explicit ``validated_hypothesis_id`` — never guesses from "the sole active
    chain". The EMPIRICAL method + ``actionable=True`` are fabricated here
    because the flat model doesn't track them; PR B removes this once the LLM
    supplies real validated chains. Idempotent (a root already VALIDATED is left
    alone). Callers gate this on the case being grounded
    (``cause_state=IDENTIFIED``); it is the prerequisite that gives M6 a
    validated root to demote on counterfactual disconfirmation.
    """
    rcc = case.root_cause_conclusion
    hyp_id = getattr(rcc, "validated_hypothesis_id", None) if rcc else None
    if not hyp_id:
        return False
    hyp = case.hypotheses.get(hyp_id)
    if hyp is None or not hyp.root_node_id:
        return False
    root = case.causal_nodes.get(hyp.root_node_id)
    if root is None or root.node_state == NodeState.VALIDATED:
        return False
    # Set method + actionable before state so the combination is valid at every
    # step (M4: validated⇒method; M1: validated root⇒actionable).
    root.validation_method = ValidationMethod.EMPIRICAL
    root.actionable = True
    root.node_state = NodeState.VALIDATED
    return True
