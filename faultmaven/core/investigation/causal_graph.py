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
strict-exclusion); LLM-emitted-chain ingestion + orphan-chain resolution;
grounded-root promotion; and M6 counterfactual-disconfirmation demotion. (The
transitional flat->graph bridge was removed in PR B2c — the graph is now
emission-only.) Belief propagation (§6.1 / §9.4) is a follow-on.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
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
# §7.1 — empirical node-state derivation (what feeds is_chain_root_validated)
# ---------------------------------------------------------------------------


def _node_evidence_tally(
    node: CausalNode, evidence_by_id: dict[str, EvidenceCategory | None]
) -> tuple[int, int, int, int]:
    """``(supports, refutes, causal_supports, counterfactual_refutes)`` for a node
    from its rung links.

    Counts only links whose backing evidence row actually exists (a dangling
    ``evidence_id`` is ignored, never assumed). ``causal_supports`` is the subset
    of SUPPORTS links backed by ``CAUSAL_EVIDENCE`` — the §7.1 "direct observable
    fact" bar (the same causal floor the flat ``_cause_state_grounded`` uses), so
    a node validates only on real causal grounding. ``counterfactual_refutes`` is
    the subset of REFUTES links backed by ``CAUSAL_ABSENCE_EVIDENCE`` — a
    counterfactual disconfirmation (the cause was addressed yet ``D`` persisted),
    the §7.2 strongest grade, which refutes DECISIVELY (it is not outweighed by
    correlational support).
    """
    supports = refutes = causal_supports = counterfactual_refutes = 0
    for link in node.evidence_links:
        if link.evidence_id not in evidence_by_id:
            continue  # dangling reference — never counts
        if link.stance == EvidenceStance.SUPPORTS:
            supports += 1
            if evidence_by_id[link.evidence_id] == EvidenceCategory.CAUSAL_EVIDENCE:
                causal_supports += 1
        elif link.stance == EvidenceStance.REFUTES:
            refutes += 1
            if (
                evidence_by_id[link.evidence_id]
                == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
            ):
                counterfactual_refutes += 1
    return supports, refutes, causal_supports, counterfactual_refutes


def derive_node_states(case: Case) -> bool:
    """Derive every causal node's ``node_state`` from its OWN rung evidence
    (§7.1) plus the M7 AND-gate — the evidence-grounded replacement for
    ``promote_grounded_chain_root``'s fabricated EMPIRICAL grade. This is what
    makes ``cause_state=IDENTIFIED`` (via ``is_chain_root_validated``, §9.2) a
    derived truth signal: a root reaches VALIDATED only when real causal evidence
    bears it out, never because the flat model already "knew" the answer.

    This is the EMPIRICAL lane only: a node's state follows its OWN rung evidence
    plus the M7 AND-gate used strictly as a VALIDATION gate. Structural
    refutation propagation (refuting an effect because an upstream cause is
    refuted) is deliberately NOT done here — it over-refutes a node that still
    has an intact OR-alternative and would invert precedence over a node's own
    direct observation; that belongs to the §9.4 belief-propagation slice.

    Per non-PROBLEM node (the PROBLEM node ``D`` is the engine-owned anchor and
    is left untouched):

    - **REFUTED** — a counterfactual disconfirmation bears on it (any
      ``CAUSAL_ABSENCE_EVIDENCE`` REFUTES link — §7.2 strongest grade, decisive),
      OR its links net-refute it ``refutes > supports`` (strict; a correlational
      tie is INCONCLUSIVE, not a disproof). ``validation_method=NONE``,
      ``actionable=False``.
    - **VALIDATED** — not refuted, has at least one CAUSAL_EVIDENCE-backed
      SUPPORTS link, is net-supporting (``supports > refutes``), AND every
      AND-set feeding it is fully VALIDATED (M7 proof, strict). EMPIRICAL grade;
      a validated ROOT is marked ``actionable`` (M1). Method/actionable/reason
      are kept mutually consistent so the node satisfies its M1/M4/refutation
      model-validators on reload (``CausalNode(**...)``; ``validate_assignment``
      is off in memory).
    - **INCONCLUSIVE** — has bearing evidence but neither validates nor refutes
      (including a support/refute tie).
    - **CANDIDATE** — no bearing evidence yet (the lazy default; a freshly
      emitted, untested rung).

    A node already VALIDATED by DEDUCTION (§7.1.1, proof-by-exclusion) carries no
    supporting evidence of its own by design, so the evidence-local lane LEAVES
    it intact — it is only overturned here by DIRECT refuting evidence
    (``refutes > supports``), never silently demoted to CANDIDATE.

    Iterates to a fixpoint (bounded by node count) so a cause validated this pass
    can satisfy its effect's AND-gate within the same recompute. On a DAG the
    longest dependency path is ``len(nodes)-1`` edges, so ``len(nodes)+1`` passes
    settle it; a malformed cyclic graph simply stops at the bound (no hang).
    Returns True if any node's state changed.
    """
    nodes = case.causal_nodes
    edges = case.causal_edges
    evidence_by_id: dict[str, EvidenceCategory | None] = {
        e.evidence_id: e.category for e in case.evidence
    }

    changed_any = False
    # Fixpoint: a validated parent can unlock a child's AND-gate. Bound the loop
    # by node count + 1 (a strictly longer dependency chain cannot exist).
    for _ in range(len(nodes) + 1):
        changed_this_pass = False
        for node in nodes.values():
            if node.node_type == NodeType.PROBLEM:
                continue
            supports, refutes, causal_supports, counterfactual_refutes = (
                _node_evidence_tally(node, evidence_by_id)
            )
            deductively_valid = (
                node.node_state == NodeState.VALIDATED
                and node.validation_method == ValidationMethod.DEDUCTIVE
            )
            # A counterfactual disconfirmation (§7.2/§7.3) refutes decisively; a
            # correlational tie/majority is the lesser ``refutes > supports`` bar.
            if counterfactual_refutes >= 1 or refutes > supports:
                target_state = NodeState.REFUTED
            elif deductively_valid:
                continue  # owned by the deductive lane — never demote here
            elif (
                causal_supports >= 1
                and supports > refutes
                and and_constraints_satisfied(node.node_id, nodes, edges)
            ):
                target_state = NodeState.VALIDATED
            elif supports or refutes:
                target_state = NodeState.INCONCLUSIVE
            else:
                target_state = NodeState.CANDIDATE

            if target_state == node.node_state:
                continue

            # Keep the FINAL field combination invariant-valid (the model
            # validators run on reload via CausalNode(**...)): M4 validated ⇒
            # method != NONE; M1 validated ROOT ⇒ actionable; REFUTED ⇔
            # refutation_reason present (and a reason is illegal on any other
            # state, so it is cleared when leaving REFUTED).
            if target_state == NodeState.VALIDATED:
                node.validation_method = ValidationMethod.EMPIRICAL
                node.refutation_reason = None
                if node.node_type == NodeType.ROOT:
                    node.actionable = True
            elif target_state == NodeState.REFUTED:
                node.validation_method = ValidationMethod.NONE
                node.actionable = False
                if not node.refutation_reason:
                    node.refutation_reason = (
                        "refuted by rung evidence / a refuted AND-member (M7)"
                    )
            else:  # CANDIDATE / INCONCLUSIVE
                node.validation_method = ValidationMethod.NONE
                node.refutation_reason = None
            node.node_state = target_state
            changed_this_pass = changed_any = True
        if not changed_this_pass:
            break
    return changed_any


# ---------------------------------------------------------------------------
# Graph anchoring + path walking (the PROBLEM node D + root->D paths)
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

    Breadth-first search for the shortest ``root → D`` route. A node may have
    several downstream edges (convergence, S2) and some branches dead-end; a
    greedy single-arrow walk would wrongly report an open chain when it picked a
    dead branch first, so the search explores all branches. Returns ``[]`` if no
    path reaches ``D`` (the chain is still open, or ``root_id`` *is* ``D`` — a
    root cause cannot be the symptom itself) — the caller then leaves
    ``root_node_id``/``path`` unset.
    """
    problem = next(
        (n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM),
        None,
    )
    if problem is None or root_id not in case.causal_nodes:
        return []
    d_id = problem.node_id
    if root_id == d_id:
        return []  # the symptom is not its own root cause
    # Adjacency: cause -> [effects].
    out: dict[str, list[str]] = {}
    for e in case.causal_edges:
        out.setdefault(e.cause_node_id, []).append(e.effect_node_id)
    parent: dict[str, str | None] = {root_id: None}
    queue: deque[str] = deque([root_id])
    while queue:
        cur = queue.popleft()
        if cur == d_id:
            path: list[str] = []
            node: str | None = d_id
            while node is not None:
                path.append(node)
                node = parent[node]
            return list(reversed(path))
        for nxt in out.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return []  # open chain — no route to D


def ingest_emitted_chain(
    case: Case,
    nodes_to_add: list,
    edges_to_add: list,
    node_evidence: list,
    current_turn: int,
    evidence_created_ids: list | None = None,
) -> list[str | None]:
    """Build the causal graph from a turn's LLM-emitted chain fragments (lazy
    backward expansion, methodology §5/S3). Pure: no I/O, no LLM.

    The sole source of the causal graph (the transitional bridge was removed in
    PR B2c). Each item is a duck-typed schema object:

    - ``nodes_to_add`` — ``statement``, ``node_type``, optional ``produces``
      (the node it directly causes: an existing id, ``'D'``, or ``'new_index_N'``
      into this same list) and ``and_group``.
    - ``edges_to_add`` — explicit ``cause``/``effect`` refs (+ ``and_group``,
      ``reasoning``) for convergence (S2) beyond a node's own ``produces``.
    - ``node_evidence`` — ``node_ref``, ``evidence_id``/``evidence_id_ref``,
      ``stance``, ``reasoning``, ``stance_confidence``. The evidence ref may be a
      real ``ev_...`` id or ``'new_index_N'`` referencing evidence created this
      turn (resolved via ``evidence_created_ids``).

    ``evidence_created_ids`` are the evidence ids added earlier this turn (the
    caller's ``metadata['evidence_added']``), against which ``new_index_N``
    evidence refs resolve — without it, same-turn rung evidence is dropped.

    Returns the created node ids in emission order (``None`` for any skipped
    node, so ``new_index_N`` indices stay aligned), so the caller can resolve
    ``new_index_N`` references (e.g. linking a hypothesis to its root node)
    against them. Best-effort and
    never raises: unresolvable refs, unknown evidence, and malformed nodes
    (empty statement, or a type other than root/intermediate — ``D`` is
    engine-seeded, never emitted) are skipped; ``D`` is seeded if a problem
    statement exists, otherwise ingestion is a no-op.
    """
    evidence_created_ids = evidence_created_ids or []
    problem = seed_problem_node(case)
    if problem is None:
        return []
    d_id = problem.node_id

    # Pass 1: create the nodes; record ids in order for new_index_N resolution.
    # A skipped node holds None so later indices still line up.
    created: list[str | None] = []
    for spec in nodes_to_add:
        statement = (getattr(spec, "statement", None) or "").strip()
        node_type = getattr(spec, "node_type", None)
        if not statement or node_type not in (
            NodeType.ROOT,
            NodeType.INTERMEDIATE,
        ):
            # Empty statement (CausalNode rejects it) or a non-{root,intermediate}
            # type (a second PROBLEM node would violate the one-D-per-case index).
            created.append(None)
            continue
        node = CausalNode(
            statement=statement[:500],
            node_type=node_type,
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

    def _resolve_ev(ref: str | None) -> str | None:
        # Evidence ref: a real ev_ id, or 'new_index_N' into this turn's evidence.
        if not ref:
            return None
        if ref.startswith("new_index_"):
            try:
                idx = int(ref[len("new_index_") :])
            except ValueError:
                return None
            return (
                evidence_created_ids[idx]
                if 0 <= idx < len(evidence_created_ids)
                else None
            )
        return ref

    for link in node_evidence:
        nid = _resolve(getattr(link, "node_ref", None))
        node = case.causal_nodes.get(nid) if nid else None
        ev_id = _resolve_ev(
            getattr(link, "evidence_id", None) or getattr(link, "evidence_id_ref", None)
        )
        stance = getattr(link, "stance", None)
        if node is None or ev_id not in existing_ev or stance is None:
            continue
        if any(el.evidence_id == ev_id for el in node.evidence_links):
            continue
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=ev_id,
                stance=stance,
                reasoning=getattr(link, "reasoning", None) or "node evidence",
                stance_confidence=getattr(link, "stance_confidence", 1.0),
            )
        )

    return created


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


def any_chain_root_validated(case: Case) -> bool:
    """§9.2: does some live hypothesis's chain ROOT node read VALIDATED? This is
    the chain-mode ``cause_state=IDENTIFIED`` signal. A REFUTED hypothesis is
    excluded (its root is no longer a standing cause), so a disconfirmed chain
    cannot keep grounding the case."""
    return any(
        h
        and h.state != HypothesisState.REFUTED
        and is_chain_root_validated(h, case.causal_nodes)
        for h in case.hypotheses.values()
    )


def _attach_engine_refutation(case: Case, node_id: str, reason: str) -> None:
    """Attach a DURABLE engine-authored REFUTES link (+ backing
    ``CAUSAL_ABSENCE_EVIDENCE`` row) to ``node_id`` — the Option-(c) mechanism
    that makes M6 evidence-driven. Without a persisted refuting fact,
    ``derive_node_states`` would re-validate the root next turn from the stale
    supporting evidence and resurrect the disconfirmed cause (the turn-28 bug).
    Idempotent: skips when the node already carries a refuting link. The backing
    row is ``CAUSAL_ABSENCE_EVIDENCE`` (the honest counterfactual category) so it
    does not inflate the flat ``CAUSAL_EVIDENCE`` grounding count.
    """
    node = case.causal_nodes.get(node_id)
    if node is None or any(
        link.stance == EvidenceStance.REFUTES for link in node.evidence_links
    ):
        return
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            summary=(
                "Counterfactual disconfirmation (M6): the cause was addressed or "
                "confirmed correct, yet the problem persisted."
            ),
            primary_purpose="failed-treatment disconfirmation",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="engine",
            collected_at_turn=case.current_turn,
            collected_at=datetime.now(timezone.utc),
        )
    )
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=ev_id,
            stance=EvidenceStance.REFUTES,
            reasoning=reason,
            linked_at_turn=case.current_turn,
        )
    )


def demote_disconfirmed_cause_via_evidence(case: Case) -> bool:
    """M6 for chain mode (Option c): on counterfactual disconfirmation of the
    grounded cause, refute the flat hypothesis AND attach a DURABLE engine
    refutation to its root, then retract the conclusion. Unlike the flat
    ``demote_disconfirmed_cause`` (which imperatively flips ``node_state``), the
    root's refutation is recorded as EVIDENCE so the subsequent
    ``derive_node_states`` — this turn and every later turn — keeps the root
    REFUTED instead of re-validating it from the now-stale supporting evidence.

    Same trigger as the flat path (shared helpers): a grounded
    (``cause_state=IDENTIFIED``) case whose representative cause hypothesis is
    REFUTED or net-refuted. Returns True if it acted.
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

    if hyp.state != HypothesisState.REFUTED:
        HypothesisManager().refute_hypothesis(hyp, case.current_turn, [], reason)
    if hyp.root_node_id:
        _attach_engine_refutation(case, hyp.root_node_id, reason)
    # Retract the conclusion so the disposition layer cannot keep treating the
    # cause as known; the cause_state itself is re-derived from the (now refuted)
    # root by the caller's derive + recompute.
    case.root_cause_conclusion = None
    p.root_cause_likelihood = 0.0
    return True


# ---------------------------------------------------------------------------
# Orphan-chain resolution (the invariant: "every chain explaining D is attached
# to exactly one hypothesis"). The divergence the prompt (step 2) does not fully
# prevent: the LLM emits a real root->D chain but leaves it unlinked, so the
# hypothesis keeps running flat while a parallel orphan chain describes the SAME
# cause (double-representation). This deterministic post-pass runs each turn
# AFTER chain-ingest.
# ---------------------------------------------------------------------------

# A root whose statement restates a hypothesis at/above STRONG, with no other
# hypothesis at/above AMBIGUOUS, is an UNAMBIGUOUS double-representation and is
# re-attached automatically (T1). A weaker or contested match is left for an
# LLM nudge (T2a) — a wrong auto-attach is itself an incorrect conclusion, so
# "when unsure, don't". The scoring + thresholds mirror the sim analyzer's
# ``_restatement_score`` / ``_RESTATEMENT_THRESHOLD``
# (fm-sre-simulator/scripts/analyze_chain_emission.py) so engine and harness
# agree on what "restates" means — keep them reconciled when either moves.
RESTATEMENT_STRONG = 0.6
RESTATEMENT_AMBIGUOUS = 0.4

# Function/filler words dropped before comparing two statements. This is the
# GENERAL base list; the sim analyzer may EXTEND it with scenario-specific noise
# words (e.g. a recurring service name) for its own runs — those deliberately
# stay out of the engine, which must not bake any one scenario's vocabulary in.
_RESTATEMENT_STOPWORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "of",
    "to",
    "in",
    "on",
    "at",
    "for",
    "and",
    "or",
    "that",
    "this",
    "it",
    "its",
    "by",
    "with",
    "as",
    "from",
    "has",
    "have",
    "had",
    "be",
    "been",
    "into",
    "not",
    "but",
    "which",
    "when",
    "then",
    "so",
    "new",
    "version",
    "service",
    "application",
}


def _content_tokens(text: str) -> set[str]:
    """Lowercased content tokens (filler words dropped) for comparing whether
    two statements describe the same cause."""
    raw = "".join(
        c.lower() if (c.isalnum() or c in ".-_:/") else " " for c in (text or "")
    )
    return {t for t in raw.split() if len(t) >= 2 and t not in _RESTATEMENT_STOPWORDS}


def restatement_score(a: str, b: str) -> float:
    """How strongly statement ``a`` restates ``b`` (0..1): the max of Jaccard and
    the two containments, so a specific elaboration largely covered by a more
    general statement (or vice versa) still scores high. Fuzzy by nature."""
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    return max(jaccard, inter / len(ta), inter / len(tb))


# A T1 auto-attach needs at least this many shared content tokens. The score
# alone is not enough: a 1–2 token statement fully contained in another yields
# containment 1.0 (a STRONG score) on a single coincidental word, which would
# auto-attach on flimsy evidence. Requiring a substantive overlap keeps the
# deterministic re-root honest ("when unsure, don't") without touching the
# shared thresholds.
_MIN_SHARED_TOKENS_FOR_REATTACH = 2


def _substantive_overlap(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` share enough content tokens that a STRONG score
    reflects real overlap, not a single-word containment artifact."""
    return (
        len(_content_tokens(a) & _content_tokens(b)) >= _MIN_SHARED_TOKENS_FOR_REATTACH
    )


def _referenced_node_ids(case: Case) -> set[str]:
    """Every node id that lies on some hypothesis path or is a hypothesis root —
    the single definition of "load-bearing" used by both the GC and the
    orphan-resolution post-pass."""
    referenced: set[str] = set()
    for h in case.hypotheses.values():
        if h.root_node_id:
            referenced.add(h.root_node_id)
        referenced.update(h.path or [])
    return referenced


def prune_abandoned_nodes(case: Case, abandoned_node_ids: list[str]) -> None:
    """Drop the nodes of a chain abandoned by a hypothesis re-root, but only the
    ones now dead — referenced by no hypothesis (as a root or on a path). The
    PROBLEM node D is never collected (it anchors every chain). Edges are pruned
    to those whose endpoints both survive, so a surviving node's connectivity is
    never severed. No-op for any node still load-bearing for another hypothesis.
    """
    referenced = _referenced_node_ids(case)
    for node_id in abandoned_node_ids:
        node = case.causal_nodes.get(node_id)
        if node is None or node.node_type == NodeType.PROBLEM:
            continue  # keep D; skip already-gone
        if node_id in referenced:
            continue  # still load-bearing for some hypothesis
        case.causal_nodes.pop(node_id, None)
    case.causal_edges[:] = [
        e
        for e in case.causal_edges
        if e.cause_node_id in case.causal_nodes
        and e.effect_node_id in case.causal_nodes
    ]


def _hypothesis_lacks_real_chain(hyp: "Hypothesis") -> bool:
    """True when the hypothesis is flat or carries only a degenerate stub (a
    2-node root->D path). Re-attaching only such a hypothesis avoids clobbering
    one that already owns a real multi-rung chain — that case is a genuine
    separate representation, left for an LLM nudge instead."""
    return not hyp.path or len(hyp.path) <= 2


def resolve_orphan_chains(case: Case) -> list[dict]:
    """Resolve emitted chains the LLM left unlinked (the invariant: every chain
    explaining D attaches to exactly one hypothesis). Run AFTER chain ingest.

    For each ORPHAN root (a ROOT node on no hypothesis path, anchoring a chain
    that reaches D), score its statement against every hypothesis:

    - **T1 — deterministic re-attach.** Exactly one hypothesis restates it
      ``>= RESTATEMENT_STRONG`` with no other ``>= RESTATEMENT_AMBIGUOUS``, and
      that hypothesis lacks a real chain of its own: re-root it onto the orphan
      chain and GC its abandoned stub. Mutates the graph in place.
    - **T2a — ambiguous.** The orphan restates a hypothesis but not
      unambiguously (best in ``[AMBIGUOUS, STRONG)``, or two-plus hypotheses
      ``>= AMBIGUOUS``, or the sole strong match already owns a real chain):
      returned for the caller to surface as a one-turn LLM nudge. NOT
      auto-resolved.
    - **benign.** Matches no hypothesis (best ``< AMBIGUOUS``): left as a
      standalone candidate root ("an unexplained candidate root is fine").

    Returns the ambiguous orphans (each ``{root_id, statement,
    candidate_hypotheses}``) for T2a; T1 re-attachments are applied in place.
    """
    if not any(n.node_type == NodeType.PROBLEM for n in case.causal_nodes.values()):
        return []

    # ``referenced`` is the set of load-bearing nodes; it only changes when a T1
    # re-attach below mutates a hypothesis path, so compute it once and refresh
    # it only after an actual re-attach (not every iteration). Snapshot the
    # orphan-root candidates up front; a prior re-attach may GC or adopt a later
    # candidate, so re-check existence/membership per root.
    referenced = _referenced_node_ids(case)
    orphan_root_ids = [
        nid
        for nid, n in case.causal_nodes.items()
        if n.node_type == NodeType.ROOT and nid not in referenced
    ]

    # Hypotheses already re-rooted in THIS pass — excluded from later scoring so
    # one orphan cannot re-attach a hypothesis a previous orphan already took
    # (no churn), and an already-taken hypothesis cannot inflate another orphan's
    # ambiguity count and wrongly downgrade its clean match to a nudge.
    adopted: set[str] = set()
    ambiguous: list[dict] = []
    for root_id in orphan_root_ids:
        node = case.causal_nodes.get(root_id)
        if node is None or root_id in referenced:
            continue  # GC'd or adopted onto a path by a prior re-attach
        path = chain_path_to_problem(root_id, case)
        if not path:
            continue  # open chain not yet anchored to D — leave it

        scored = [
            (s, h)
            for h in case.hypotheses.values()
            if h.hypothesis_id not in adopted
            and (s := restatement_score(node.statement, h.statement))
            >= RESTATEMENT_AMBIGUOUS
        ]
        if not scored:
            continue  # benign standalone candidate root

        # T1: exactly one hypothesis matches (so the sole match is also the only
        # one >= STRONG, since STRONG > AMBIGUOUS), it owns no real chain, and the
        # overlap is substantive (not a single-word containment artifact).
        if (
            len(scored) == 1
            and scored[0][0] >= RESTATEMENT_STRONG
            and _hypothesis_lacks_real_chain(scored[0][1])
            and _substantive_overlap(node.statement, scored[0][1].statement)
        ):
            hyp = scored[0][1]
            old_path = hyp.path or []
            hyp.root_node_id = root_id
            hyp.path = path
            prune_abandoned_nodes(case, old_path)
            adopted.add(hyp.hypothesis_id)
            referenced = _referenced_node_ids(case)  # graph changed — refresh
            continue

        # T2a: matched but ambiguous (or the strong match already owns a chain).
        ambiguous.append(
            {
                "root_id": root_id,
                "statement": node.statement,
                "candidate_hypotheses": [
                    h.statement
                    for _, h in sorted(scored, key=lambda sh: sh[0], reverse=True)
                ],
            }
        )
    return ambiguous
