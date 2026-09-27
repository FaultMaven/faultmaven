from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    NodeState,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis


# §7.1.1 guard 3: a sibling counts as "excluded" only when its refutation is
# ABSOLUTE — REFUTED and belief at/under this bar. A merely-inconclusive or
# weakly-refuted sibling does NOT count, so deductive validation cannot fire on
# partial exclusion.
DEDUCTIVE_EXCLUSION_MAX_BELIEF = 0.05

# The M2 confidence-band constants (MECHANISTIC_RCC_LIKELIHOOD /
# CONFIRMED_RCC_LIKELIHOOD_FLOOR) and the engine-mirror author marker live in
# ``cause_assurance`` (grade semantics, contracts-only) and are imported above —
# the terminal confirm-stamp there shares them and cannot import this module.


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

    A blank key ("" or whitespace) names no group and normalizes to ``None``.
    ``_add_edge`` normalizes on the way in, but ``and_group`` is an
    unconstrained ``Optional[str]`` at every layer and rows persisted before
    that guard cannot be reached by it — so the normalization lives HERE, where
    every reader is healed at once: the M7 prover would otherwise keep a
    silently-strengthened gate on legacy data, and ``validated_and_conjuncts``
    would publish "the cause required these conditions too" over causes the
    graph holds as independent alternatives.
    """
    groups: dict[str | None, list[str]] = {}
    for e in edges:
        if e.effect_node_id == node_id:
            key = e.and_group
            if isinstance(key, str) and not key.strip():
                key = None
            groups.setdefault(key, []).append(e.cause_node_id)
    return groups


def _state(node_id: str, nodes: dict[str, CausalNode]) -> NodeState | None:
    n = nodes.get(node_id)
    return n.node_state if n else None


def validated_and_conjuncts(case: "Case", chain_node_ids: list[str]) -> list[str]:
    """The VALIDATED causes co-necessary (M7 AND-set) with a chain, as statements.

    An AND-set is the graph's only representation of "the problem needed BOTH of
    these" — edges sharing ``(effect_node_id, and_group)`` are co-necessary. The
    conclusion mirror renders ONE chain (root -> ... -> D): its root becomes the
    cause text and its intermediate rungs the mechanism. A conjunct that is not
    itself on that chain is therefore established by the investigation and absent
    from the conclusion, which is how a genuinely two-factor cause reaches the
    report as a single clause (#1096). This is what the conclusion's
    ``contributing_factors`` carries.

    Only VALIDATED conjuncts are named. An AND-member still standing as a
    candidate is not established, and a conclusion is the one place that must
    never assert more than the graph proves — the same one-directional guarantee
    the rest of the engine lane keeps.

    Chain nodes themselves are excluded (they are already the conclusion's root
    and mechanism). The result is de-duplicated and SORTED: conjuncts of one
    effect are co-equal (an AND-set is unordered by construction) and neither
        repository loads ``causal_edges`` with an ``ORDER BY``, so deriving order
    from row order would make the list — and the equality check the mirror's
    faithfulness short-circuit runs on it — vary with fetch order on PostgreSQL,
    re-minting the conclusion every recompute and flipping the report's bullets
    between regenerations. Sorted, the output is a function of the graph's
    CONTENT rather than of its storage.
    """
    on_chain = set(chain_node_ids)
    statements: set[str] = set()
    for node_id in chain_node_ids:
        for and_group, cause_ids in incoming_and_groups(
            node_id, case.causal_edges
        ).items():
            if and_group is None:
                continue  # OR alternatives: each is its own sufficient cause
            for cause_id in cause_ids:
                if cause_id in on_chain:
                    continue
                node = case.causal_nodes.get(cause_id)
                if node is None or node.node_state != NodeState.VALIDATED:
                    continue
                statements.add(node.statement)
    return sorted(statements)


def mechanism_for_chain(case: "Case", hyp) -> str:
    """The mirrored chain's mechanism, as user-facing prose (#1097).

    The chain's INTERMEDIATE rungs, in order — the "how" between the cause and
    the symptom. Both mint sites (the per-turn mirror here and the terminal
    confirm-stamp in ``cause_assurance``) render one conclusion, so they build
    this one way: the duplicated copies drifted apart on exactly the field a
    reader sees, and the runbook harvest reads ``mechanism`` too.

    The PROBLEM node is deliberately NOT appended. It is the engine's synthetic
    anchor, not a mechanism step, and the report renders this under "How it
    produced the symptom" — so a trailing "→ the problem" restated the heading
    in arrow notation and read as debug output escaping into prose (#1097). The
    no-rung case already stated a sentence rather than an arrow; this makes the
    two agree.
    """
    inter = [
        case.causal_nodes[nid].statement
        for nid in (getattr(hyp, "path", None) or [])[1:-1]
        if nid in case.causal_nodes
    ]
    return (" → ".join(inter) if inter else "Directly produces the observed problem.")[
        :2000
    ]


def conjuncts_for_chain(case: "Case", hyp=None, root=None) -> list[str]:
    """``validated_and_conjuncts`` over the chain a conclusion would mirror.

    The ONE chain-builder, shared by the per-turn mirror here and the terminal
    confirm-stamp in ``cause_assurance`` (which reaches it through the graph
    hook). Both must name the same conjuncts for the same case; implementing
    the rule twice across that module boundary would let the two conclusions a
    single case passes through disagree, with nothing watching.

    The path is the chain when the hypothesis carries one — ``chain_path_to_problem``
    builds it as ``[root, ..., D]``, so an AND-set at any depth including D is
    covered. A path-less hypothesis still has its root (M3 requires it before
    validation), and the fallback adds the PROBLEM node: the canonical
    two-factor shape is both conjuncts pointing straight at D, so reading the
    root's incoming edges alone would report none.
    """
    chain = list(getattr(hyp, "path", None) or []) if hyp is not None else []
    if not chain:
        root_id = getattr(root, "node_id", None) or getattr(hyp, "root_node_id", None)
        problem = next(
            (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
            None,
        )
        chain = [x for x in (root_id, problem.node_id if problem else None) if x]
    return validated_and_conjuncts(case, chain)


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
# `exhaustive` (guard #1) is NEVER engine-inferred: F4 family-completeness is an
# LLM-judgment guideline, not an engine sweep, so a pure `(case)` function cannot
# certify the OR-set complete. It is supplied ONLY by an explicit agent assertion
# (``validate_by_exclusion`` is called with the survivors the LLM certified via
# ``deductive_validations``); this predicate still re-checks every guard the engine
# CAN compute (≥2 members, all-but-survivor absolutely refuted), so a mis-asserted
# exhaustiveness cannot fabricate a validation on its own. See #593 / the
# deductive-validation-wiring design doc for the full division of labour.


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
