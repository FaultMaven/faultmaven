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

Contents: structural primitives (AND-proof, chain-root validation, deductive
strict-exclusion); the transitional flat->graph bridge and grounded-root
promotion (Option-1); and M6 counterfactual-disconfirmation demotion. Belief
propagation (§6.1 / §9.4) is a follow-on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    EvidenceStance,
    HypothesisState,
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


def seed_problem_node(case: Case) -> CausalNode | None:
    """Return the case's single PROBLEM node ``D``, creating it from the
    confirmed problem statement when absent.

    ``D`` is engine-owned (deterministic), not LLM-emitted: it anchors every
    chain. Returns None when there is no problem statement to anchor on yet.
    Idempotent — at most one PROBLEM node per case.
    """
    problem_node = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem_node is not None:
        return problem_node
    pv = case.problem_verification
    statement = pv.symptom_statement if pv else None
    if not statement or not statement.strip():
        return None
    problem_node = CausalNode(
        statement=statement[:500],
        node_type=NodeType.PROBLEM,
        generated_at_turn=case.current_turn,
    )
    case.causal_nodes[problem_node.node_id] = problem_node
    return problem_node


def chain_path_to_problem(root_id: str, case: Case) -> list[str]:
    """Walk cause→effect edges from ``root_id`` down to the PROBLEM node ``D``,
    returning the ordered path ``[root_id, ..., d_id]`` (methodology: a
    ``Hypothesis`` is a root→D path).

    Follows one outgoing edge per node (the lazy single-arrow primitive, S1);
    where a node has several downstream edges the first toward an unvisited node
    is taken. Returns ``[]`` if no path reaches ``D`` (the chain is still open) —
    the caller leaves ``root_node_id``/``path`` unset until it does.
    """
    problem = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem is None or root_id not in case.causal_nodes:
        return []
    d_id = problem.node_id
    # Adjacency: cause -> [effects].
    out: dict[str, list[str]] = {}
    for e in case.causal_edges:
        out.setdefault(e.cause_node_id, []).append(e.effect_node_id)
    path = [root_id]
    seen = {root_id}
    cur = root_id
    while cur != d_id:
        nxt = next((e for e in out.get(cur, []) if e not in seen), None)
        if nxt is None:
            return []  # open chain — no route to D
        path.append(nxt)
        seen.add(nxt)
        cur = nxt
    return path


def ingest_emitted_chain(
    case: Case,
    nodes_to_add: list,
    edges_to_add: list,
    node_evidence: list,
    current_turn: int,
) -> list[str]:
    """Build the causal graph from a turn's LLM-emitted chain fragments (lazy
    backward expansion, methodology §5/S3). Pure: no I/O, no LLM.

    Replaces the transitional bridge once the LLM emits chains directly. Each
    item is a duck-typed schema object:

    - ``nodes_to_add`` — ``statement``, ``node_type``, optional ``produces``
      (the node it directly causes: an existing id, ``'D'``, or ``'new_index_N'``
      into this same list) and ``and_group``.
    - ``edges_to_add`` — explicit ``cause``/``effect`` refs (+ ``and_group``,
      ``reasoning``) for convergence (S2) beyond a node's own ``produces``.
    - ``node_evidence`` — ``node_ref``, ``evidence_id``/``evidence_id_ref``,
      ``stance``, ``reasoning``, ``stance_confidence``.

    Returns the created node ids in emission order, so the caller can resolve
    ``new_index_N`` hypothesis ``root_node_ref`` against them. Best-effort:
    unresolvable refs and unknown evidence are skipped (never raised); ``D`` is
    seeded if a problem statement exists, otherwise ingestion is a no-op.
    """
    problem = seed_problem_node(case)
    if problem is None:
        return []
    d_id = problem.node_id

    # Pass 1: create the nodes; record ids in order for new_index_N resolution.
    created: list[str] = []
    for spec in nodes_to_add:
        node = CausalNode(
            statement=(spec.statement or "")[:500],
            node_type=spec.node_type,
            generated_at_turn=current_turn,
        )
        case.causal_nodes[node.node_id] = node
        created.append(node.node_id)

    def _resolve(ref: str | None) -> str | None:
        if not ref:
            return None
        if ref == "D":
            return d_id
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return created[idx] if 0 <= idx < len(created) else None
        return ref if ref in case.causal_nodes else None

    def _add_edge(cause_id, effect_id, and_group, reasoning):
        if not cause_id or not effect_id or cause_id == effect_id:
            return
        if cause_id not in case.causal_nodes or effect_id not in case.causal_nodes:
            return
        if any(
            e.cause_node_id == cause_id and e.effect_node_id == effect_id
            for e in case.causal_edges
        ):
            return  # idempotent
        case.causal_edges.append(
            CausalEdge(
                cause_node_id=cause_id,
                effect_node_id=effect_id,
                and_group=and_group,
                reasoning=reasoning,
                created_at_turn=current_turn,
            )
        )

    # Pass 2: edges from each node's `produces`, then explicit edges.
    for i, spec in enumerate(nodes_to_add):
        produces = getattr(spec, "produces", None)
        if produces:
            _add_edge(
                created[i], _resolve(produces), getattr(spec, "and_group", None), None
            )
    for e in edges_to_add:
        _add_edge(
            _resolve(getattr(e, "cause", None)),
            _resolve(getattr(e, "effect", None)),
            getattr(e, "and_group", None),
            getattr(e, "reasoning", None),
        )

    # Node-targeted evidence (rung-level stance).
    existing_ev = {ev.evidence_id for ev in case.evidence}
    for link in node_evidence:
        nid = _resolve(getattr(link, "node_ref", None))
        node = case.causal_nodes.get(nid) if nid else None
        ev_id = getattr(link, "evidence_id", None) or getattr(
            link, "evidence_id_ref", None
        )
        if node is None or ev_id not in existing_ev:
            continue
        if any(el.evidence_id == ev_id for el in node.evidence_links):
            continue
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=ev_id,
                stance=link.stance,
                reasoning=getattr(link, "reasoning", None) or "node evidence",
                stance_confidence=getattr(link, "stance_confidence", 1.0),
            )
        )

    return created


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
    problem_node = seed_problem_node(case)
    if problem_node is None:
        return  # nothing to anchor on yet
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


def promote_grounded_chain_root(case: Case) -> bool:
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


def _net_refuted(hyp: Hypothesis) -> bool:
    """Decisive-disconfirmation test for M6: refuting evidence at least matches
    supporting evidence (and there is at least one refuting link).

    A *lone* refuting link among a body of supporting evidence is NOT decisive —
    treating it as such tears down a legitimately grounded cause that merely
    attracted one contrary data point during search (a real false-demotion risk).
    A refutation that flips or ties the support balance is. With no links at all
    this is False (absence of evidence is not disconfirmation).
    """
    refuting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.REFUTES
    )
    if refuting == 0:
        return False
    supporting = sum(
        1 for link in hyp.evidence_links if link.stance == EvidenceStance.SUPPORTS
    )
    return refuting >= supporting


def _representative_cause_hypothesis(case: Case) -> Hypothesis | None:
    """The hypothesis that best represents the currently-grounded cause.

    When the conclusion names one (``validated_hypothesis_id``) that is
    authoritative. Otherwise the cause was grounded case-wide (high likelihood +
    causal evidence / ``evidence_basis``) WITHOUT naming a hypothesis — the
    common grounding shape, and the one in case_e970a5c24fe1 (``likelihood`` 1.0,
    ``validated_hypothesis_id`` null). The engine's best proxy is then the
    strongest hypothesis by ORIGINAL confidence: ``initial_likelihood`` is stable
    where ``likelihood`` is not — refutation zeroes ``likelihood``, so a
    just-refuted believed-cause would vanish from a max-by-``likelihood`` pick.
    """
    rcc = case.root_cause_conclusion
    if rcc and rcc.validated_hypothesis_id:
        return case.hypotheses.get(rcc.validated_hypothesis_id)
    if not case.hypotheses:
        return None
    return max(case.hypotheses.values(), key=lambda h: h.initial_likelihood)


def demote_disconfirmed_cause(case: Case) -> bool:
    """M6 — the turn-28 fix. On counterfactual disconfirmation of the grounded
    root cause, demote it **deterministically** rather than waiting for the LLM
    to volunteer a downgrade (which it did not, in case_e970a5c24fe1).

    Acts ONLY on a grounded (sticky ``cause_state=IDENTIFIED``) case — the
    precondition that makes a demotion meaningful, gives clean idempotency (after
    it fires, ``cause_state`` is UNKNOWN so the next call no-ops), and lets it act
    on the COMMON grounding shape, where the cause is grounded by causal evidence
    / ``evidence_basis`` with NO ``validated_hypothesis_id`` (see
    ``_representative_cause_hypothesis``) — not just an explicitly-named cause.

    Triggers when that representative hypothesis is either:
      - **A** already ``REFUTED`` (the LLM refuted it), or
      - **B** *net-refuted* (``_net_refuted``) — refuting evidence flips/ties the
        balance. A single contrary link is deliberately NOT decisive (it would
        falsely demote a well-supported cause).

    Demotion is atomic and four-part so the *sticky* ``cause_state`` can neither
    keep a disproven cause, be re-grounded next turn, nor be reported as known by
    the disposition layer:
      1. refute the flat hypothesis via the canonical ``refute_hypothesis``
         (zeroes likelihood, bumps the turn, logs), preserving any LLM reason,
      2. refute the chain ROOT node and strip its now-stale validation marks
         (a REFUTED node must not keep advertising EMPIRICAL/actionable),
      3. RETRACT the ``RootCauseConclusion`` entirely — leaving it lets the
         disposition layer (``_cause_identified`` reads ``root_cause`` text) keep
         treating the cause as known and lets ``evidence_basis`` re-ground it, so
         every grounding anchor must go, not just ``validated_hypothesis_id``,
      4. un-stick the assessment — zero ``root_cause_likelihood`` and drop
         ``cause_state`` out of IDENTIFIED; the caller's recompute then derives
         CANDIDATES/UNKNOWN from the remaining active chains.

    Returns True if it demoted; False when the case is not grounded, has no
    representative cause, or that cause is not disconfirmed.
    """
    p = case.progress
    if p.cause_state != CauseState.IDENTIFIED:
        return False

    hyp = _representative_cause_hypothesis(case)
    if hyp is None:
        return False

    if not (hyp.state == HypothesisState.REFUTED or _net_refuted(hyp)):
        return False

    reason = (
        hyp.refutation_reason
        or "counterfactual disconfirmation: the cause was addressed or confirmed "
        "correct yet the problem persisted"
    )[:200]

    # 1. Refute the flat hypothesis via the canonical path (single source of
    # truth: likelihood=0.0 + last_updated_turn + logging). Skip if already
    # refuted so the LLM's own refutation reason is not clobbered.
    if hyp.state != HypothesisState.REFUTED:
        HypothesisManager().refute_hypothesis(hyp, case.current_turn, [], reason)

    # 2. Refute the chain ROOT and clear its stale validation marks (reason
    # before state so the REFUTED/refutation_reason pair is valid on round-trip).
    root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
    if root is not None and root.node_state != NodeState.REFUTED:
        root.refutation_reason = reason
        root.node_state = NodeState.REFUTED
        root.validation_method = ValidationMethod.NONE
        root.actionable = False

    # 3. Retract the disconfirmed conclusion outright — clears EVERY grounding
    # anchor (root_cause text, evidence_basis, validated_hypothesis_id) so the
    # demotion is durable and cannot be defeated downstream.
    case.root_cause_conclusion = None

    # 4. Un-stick the assessment; recompute re-derives from remaining chains.
    p.root_cause_likelihood = 0.0
    p.cause_state = CauseState.UNKNOWN
    return True
