from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.cause_assurance import (
    root_counterfactually_confirmed,
)
from faultmaven.core.investigation.lifecycle_metrics import (
    root_validation_blocked_restatement_total,
    root_validation_blocked_support_count_total,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    EvidenceCategory,
    NodeState,
    NodeType,
    ValidationMethod,
)

from .queries import (
    and_constraints_satisfied,
    deductively_validated,
    incoming_and_groups,
)
from .support import (
    BLOCK_REASON_COUNT,
    ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN,
    _causal_evidence_tokens,
    _independent_causal_support_count,
    _node_evidence_tally,
    _restating_root_ids,
    _support_block_reason,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


def derive_node_states(case: Case) -> bool:
    """Derive every causal node's ``node_state`` from its OWN rung evidence
    (§7.1) plus the M7 AND-gate. A root reaches VALIDATED only from real rung
    evidence — never from a fabricated EMPIRICAL grade. This is what
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

    - **REFUTED** — a counterfactual disconfirmation bears on it (a
      ``CAUSAL_ABSENCE_EVIDENCE`` REFUTES link at ``stance_confidence >=
      CAUSAL_STANCE_CONFIDENCE_MIN`` — §7.2 strongest grade, decisive; a
      self-hedged one is ordinary refuting evidence, the refute-side twin of
      the §7.1 SUPPORTS filter), OR its links net-refute it ``refutes >
      supports`` (strict; a correlational tie is INCONCLUSIVE, not a
      disproof). ``validation_method=NONE``, ``actionable=False``.
    - **VALIDATED** — not refuted, causally grounded, net-supporting
      (``supports > refutes``), every AND-set feeding it is fully VALIDATED
      (M7 proof, strict), AND — for a ROOT — its statement carries novel content
      beyond the case frame (``root_restates_case_frame``, §7.1 restatement
      guard: the symptom dressed as a cause holds at INCONCLUSIVE instead).
      Causal grounding (§7.1 / INV-29): a non-ROOT rung needs ≥1 causally-grounding
      SUPPORTS link (CAUSAL_EVIDENCE-backed, ``stance_confidence`` at/above
      ``CAUSAL_STANCE_CONFIDENCE_MIN``); a ROOT — the node that mints a
      conclusion — needs ``ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN`` INDEPENDENT
      such supports (distinct evidence rows that are not mutual restatements of
      each other), because every causal link is an LLM self-labeled claim and
      one self-certified datum must not conclude a case (#573/#656). A
      counterfactually CONFIRMED root (engine-stamped ``causal_absence``
      SUPPORTS, gone⇒gone — the M2 top grade, engine-only producer) satisfies
      the ROOT bar outright: confirmation dominates empirical counting, and a
      confirmed 1-support root recomputed post-RESOLVED must not demote.
      EMPIRICAL grade; a validated ROOT is marked ``actionable`` (M1).
      Method/actionable/reason are kept mutually consistent so the node
      satisfies its M1/M4/refutation model-validators on reload
      (``CausalNode(**...)``; ``validate_assignment`` is off in memory).
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
    evidence_by_id: dict[str, EvidenceCategory | None] = _evidence_category_map(case)
    restating_roots = _restating_root_ids(case)
    causal_tokens = _causal_evidence_tokens(case)

    changed_any = False
    # Fixpoint: a validated parent can unlock a child's AND-gate. Bound the loop
    # by node count + 1 (a strictly longer dependency chain cannot exist).
    for _ in range(len(nodes) + 1):
        changed_this_pass = False
        for node in nodes.values():
            if node.node_type == NodeType.PROBLEM:
                continue
            (
                supports,
                refutes,
                causal_support_ev_ids,
                raw_causal_links,
                counterfactual_refutes,
            ) = _node_evidence_tally(node, evidence_by_id)
            deductively_valid = (
                node.node_state == NodeState.VALIDATED
                and node.validation_method == ValidationMethod.DEDUCTIVE
            )
            # §7.1 causal-grounding bar (INV-29/#573): a ROOT needs
            # ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN independent causal supports
            # — or a counterfactual confirmation, which dominates counting —
            # while a non-ROOT rung keeps ≥1. The len() pre-check short-circuits
            # the pairwise independence work when the raw count can't reach the
            # bar anyway (independent count ≤ deduped link count).
            if node.node_type == NodeType.ROOT:
                causally_grounded = (
                    len(causal_support_ev_ids) >= ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN
                    and _independent_causal_support_count(
                        causal_support_ev_ids, causal_tokens
                    )
                    >= ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN
                ) or root_counterfactually_confirmed(node, evidence_by_id)
            else:
                causally_grounded = len(causal_support_ev_ids) >= 1
            # Everything the empirical bar requires EXCEPT the causal-grounding
            # bar and the restatement guard; computed separately so each block
            # branch below is provably "would have validated but for THIS bar"
            # (AND-gate blocks stay attributed to the AND-gate).
            generic_ok = supports > refutes and and_constraints_satisfied(
                node.node_id, nodes, edges
            )
            would_validate = causally_grounded and generic_ok
            # A counterfactual disconfirmation (§7.2/§7.3) refutes decisively; a
            # correlational tie/majority is the lesser ``refutes > supports`` bar.
            if counterfactual_refutes >= 1 or refutes > supports:
                target_state = NodeState.REFUTED
            elif deductively_valid:
                continue  # owned by the deductive lane — never demote here
            elif would_validate and (
                node.node_id not in restating_roots
                or node.node_state == NodeState.VALIDATED
            ):
                # The restatement guard is an ENTRY bar: it blocks the
                # transition INTO VALIDATED, but a root that already validated
                # is ruled by its EVIDENCE alone — otherwise a later sibling
                # emission whose wording overlaps would retract a correct,
                # evidence-backed conclusion (non-monotonic flap), and the
                # deductive lane (never re-derived here) would split from the
                # mint path. Pre-guard persisted conclusions are therefore
                # grandfathered — deliberately: closed cases never recompute
                # anyway, and their confidence is the grade-cap work on #656.
                target_state = NodeState.VALIDATED
            elif would_validate:
                # §7.1 restatement guard: supported and gate-satisfied, but the
                # "cause" restates the case frame — hold at INCONCLUSIVE (a
                # live candidate needing a real mechanism, never a validated
                # conclusion). The LLM refines the statement; the engine never
                # validates the symptom as its own cause.
                target_state = NodeState.INCONCLUSIVE
            elif supports or refutes:
                target_state = NodeState.INCONCLUSIVE
            else:
                target_state = NodeState.CANDIDATE

            # §7.1.1 guard #3 (engine-computable half): a COUNTERFACTUAL
            # (absence-based, §7.2 strongest) refutation is an ABSOLUTE exclusion —
            # drive ``belief`` to 0 so proof-by-exclusion (``deductively_validated``)
            # may count this sibling as excluded. A merely-correlational net-refute
            # keeps its belief above ``DEDUCTIVE_EXCLUSION_MAX_BELIEF``, so it BLOCKS
            # the deduction (graceful denial — noise-sensitive exclusion never fires
            # on a weakly-refuted sibling). Set independent of the state-change
            # short-circuit below: a node already correlationally REFUTED may become
            # counterfactually refuted on a later turn, and must then drop to 0. The
            # exhaustiveness half of the guards (guard #1) stays agent-asserted.
            if counterfactual_refutes >= 1 and node.belief != 0.0:
                node.belief = 0.0

            if target_state == node.node_state:
                continue

            # Calibration counter: ONE increment per BLOCK EVENT — the state
            # transition where a root that would otherwise have validated is
            # held by the restatement guard — never per fixpoint pass or per
            # re-derive of an already-held node. (A root already INCONCLUSIVE
            # from generic evidence whose AND-gate later unlocks is a slight
            # undercount; preferred over the unbounded overcount of counting
            # evaluations.)
            if (
                target_state == NodeState.INCONCLUSIVE
                and would_validate
                and node.node_id in restating_roots
            ):
                root_validation_blocked_restatement_total.inc()

            # INV-29 calibration counter, same block-event semantics: the
            # state transition where a ROOT with real causal-category support
            # that clears the generic bar is held ONLY by the grounding bar
            # (raw_causal_links >= 1 separates "blocked by this bar" from
            # "never causally supported at all"). Labeled with the SAME
            # block-reason classification the count-held set and the context
            # annotation read (_support_block_reason), so the metric measures
            # exactly the populations the interventions act on. Fires
            # regardless of the restatement verdict — a root failing BOTH
            # bars is attributed here (the restatement counter requires
            # would_validate, so the two never double-count one event).
            if (
                target_state == NodeState.INCONCLUSIVE
                and generic_ok
                and not causally_grounded
                and raw_causal_links >= 1
                and node.node_type == NodeType.ROOT
            ):
                reason = _support_block_reason(causal_support_ev_ids, causal_tokens)
                root_validation_blocked_support_count_total.labels(
                    reason=reason or BLOCK_REASON_COUNT
                ).inc()

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
# §7.1.1 — deductive-validation stamping (the caller of ``deductively_validated``)
# ---------------------------------------------------------------------------


def _survivor_or_sets(survivor_id: str, edges: list[CausalEdge]) -> list[list[str]]:
    """The OR-differential(s) ``survivor_id`` competes in.

    For each effect the survivor causes *as an OR-alternative* (its edge to that
    effect carries no ``and_group`` — an AND-member is not competing by exclusion),
    return that effect's set of OR-alternative direct causes (``incoming_and_groups``
    ``None`` group), which includes the survivor. A survivor may point at several
    effects (S2 convergence); each is a separate differential to try exclusion over.
    """
    or_sets: list[list[str]] = []
    effects = {
        e.effect_node_id
        for e in edges
        if e.cause_node_id == survivor_id and e.and_group is None
    }
    for eff in effects:
        or_group = incoming_and_groups(eff, edges).get(None, [])
        if survivor_id in or_group:
            or_sets.append(or_group)
    return or_sets


def validate_by_exclusion(case: Case, asserted_survivor_ids: set[str]) -> bool:
    """Stamp ``DEDUCTIVE`` on each asserted survivor whose OR-differential has
    collapsed to it (§7.1.1, proof by exclusion). Returns True if any node changed.

    ``asserted_survivor_ids`` carries the agent's exhaustiveness certification — the
    one guard (§7.1.1 #1) the engine cannot compute (F4 family-completeness is
    LLM-judgment, not an engine sweep). A survivor the LLM never asserted never
    enters this lane. For each asserted survivor the engine STILL independently
    requires (via ``deductively_validated``, ``exhaustive=True``) that ≥2
    alternatives existed and ALL but the survivor are ABSOLUTELY refuted (REFUTED +
    ``belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF`` — the counterfactual bar set in
    ``derive_node_states``), so a mis-asserted exhaustiveness cannot fabricate a
    validation on its own — the differential must genuinely have collapsed.

    Runs AFTER ``derive_node_states`` (siblings must have reached REFUTED first). A
    survivor already VALIDATED (empirically or deductively) or REFUTED is left alone
    — an assertion neither re-validates nor resurrects. Deductive validation is
    mechanistic grade only (§7.2): it validates the ROOT (unlocking treatment) but
    the case still needs a counterfactual confirmation to resolve, and harvest is
    RESOLVED-only — so the exhaustiveness assertion is backstopped downstream.

    The §7.1 restatement guard applies here too (graceful denial): a surviving
    "cause" whose statement restates the problem has excluded its alternatives
    without ever stating a mechanism — stamping it would conclude "the problem
    causes itself". The survivor stays un-stamped and the investigation stays
    open until the LLM states what the surviving cause actually IS.
    """
    if not asserted_survivor_ids:
        return False
    nodes = case.causal_nodes
    edges = case.causal_edges
    restating_roots = _restating_root_ids(case)
    changed = False
    for sid in asserted_survivor_ids:
        node = nodes.get(sid)
        if node is None or node.node_type != NodeType.ROOT:
            continue  # only a ROOT cause is validated by exclusion
        if node.node_state in (NodeState.VALIDATED, NodeState.REFUTED):
            continue  # already settled — assertion does not re-open it
        if sid in restating_roots:
            continue  # §7.1 restatement guard — no lane validates a restatement
        for or_set in _survivor_or_sets(sid, edges):
            if deductively_validated(sid, or_set, nodes, exhaustive=True):
                node.node_state = NodeState.VALIDATED
                node.validation_method = ValidationMethod.DEDUCTIVE
                node.actionable = True  # M1: a VALIDATED ROOT is actionable
                node.refutation_reason = None
                changed = True
                break
    return changed
