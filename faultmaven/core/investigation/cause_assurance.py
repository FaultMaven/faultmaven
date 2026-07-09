"""Assurance grading for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: consumers include the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it back would close an import
cycle. Keeping it here (it needs nothing from ``causal_graph``, only the case
contracts) breaks that back-edge.

``grade_cause_assurance`` is the single source of truth: it classifies a case
into one of three mutually-exclusive assurance grades — the M2 confirmation
ladder — in one pass. The §7 harvest bar is ``CONFIRMED`` (counterfactual
confirmation, gone⇒gone). Validation method (empirical vs deductive) does NOT
raise the grade: both are mechanistic per M2/§7.1.1 — a deductive derivation
is itself assembled from LLM-mediated refutations plus an asserted-exhaustive
differential, so only the counterfactual outcome of actually removing the
cause clears the top bar.

The ``CauseAssuranceGrade`` enum lives in the domain layer
(``modules.case.contracts``) so it can be a persisted field on
``InvestigationProgress`` (the ``VerificationStatus`` precedent); this module
imports and re-exports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    CauseAssuranceGrade,
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceStance,
    NodeEvidenceLink,
    NodeState,
    NodeType,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode

__all__ = [
    "CauseAssuranceGrade",
    "conclusion_overclaims",
    "confirm_root_from_resolution_absence",
    "evidence_category_map",
    "grade_cause_assurance",
    "root_counterfactually_confirmed",
]


def evidence_category_map(case: "Case") -> dict:
    """The one ``evidence_id → category`` map every assurance/tally reader
    shares (this module and ``causal_graph`` import it from here), so the
    "dangling evidence_id is ignored, never assumed" discipline has a single
    owner — parallel hand-written comprehensions would let a future filter or
    key change reach some readers and not others, silently splitting the M2
    grade from node-state derivation."""
    return {e.evidence_id: e.category for e in case.evidence}


def _validated_roots(case: "Case") -> list["CausalNode"]:
    """The case's VALIDATED root nodes — the only harvest-relevant unit (§7 never
    harvests an intermediate rung or a candidate)."""
    return [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]


def root_counterfactually_confirmed(
    node: "CausalNode", evidence_category_by_id: dict
) -> bool:
    """M2 counterfactual confirmation, per node: a SUPPORTS evidence link backed
    by a ``causal_absence_evidence`` row — the cause was removed and the problem
    went with it (gone⇒gone). The confirmation must be LINKED to this node: a
    case-level absence row with no bearing on the root does not confirm it (the
    same bearing discipline as ``_node_evidence_tally``'s counterfactual-refute
    arm). A dangling ``evidence_id`` is ignored, never assumed."""
    return any(
        link.stance == EvidenceStance.SUPPORTS
        and evidence_category_by_id.get(link.evidence_id)
        == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        for link in node.evidence_links
    )


_CONFIRMATION_REASON = (
    "engine: user-confirmed resolution — the recorded causal-absence outcome "
    "bears on the sole standing validated root (M2 gone⇒gone)"
)


def confirm_root_from_resolution_absence(case: "Case") -> bool:
    """M2 confirm-side twin of the failed-fix refute stamp: make the
    ``CONFIRMED`` grade reachable from the live flow — called at RESOLVED
    **transition execution** (after the user's explicit confirmation), never
    on the mere appearance of an absence row.

    The prompt's verify-turn contract records the resolution-confirming
    ``causal_absence_evidence`` row as a STAND-ALONE audit row ("do NOT link
    it"), and no LLM path links absence evidence to a causal node — so without
    an engine stamp the counterfactual confirmation the grade requires would
    never exist, silently decommissioning the harvest gate. But the row alone
    is an LLM self-claim: a premature "pods are stable" absence row emitted
    mid-rollout (observed live in the gate sims) must not confirm anything.
    The trigger is therefore the RESOLVED handshake — the user's explicit
    consent — which is strictly stronger evidence than the row's existence.

    Deliberately conservative (NO INCORRECT CONCLUSION):

    - Only at resolution execution (caller: ``_execute_resolved_transition``).
    - Only when exactly ONE standing validated ROOT exists. With several (an
      unarbitrated MECE violation) the engine never guesses which cause the
      fix removed — the case stays MECHANISTIC pending arbitration.
    - Only absence rows with NO existing node link anywhere in the graph
      qualify: a REFUTES-linked absence row is a failed-fix disconfirmation
      (M6) and must never flip to confirmation; an already-linked row's
      bearing is already decided.
    - Idempotent: a root already counterfactually confirmed is left alone.

    Returns True if it attached a link. The caller re-persists the grade
    afterwards so the terminal blob reflects the confirmation.
    """
    validated_roots = [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]
    if len(validated_roots) != 1:
        return False
    root = validated_roots[0]
    cat_by_id = evidence_category_map(case)
    if root_counterfactually_confirmed(root, cat_by_id):
        return False
    linked_ids = {
        link.evidence_id
        for node in case.causal_nodes.values()
        for link in node.evidence_links
    }
    absence_row = next(
        (
            e
            for e in reversed(case.evidence)
            if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
            and e.evidence_id not in linked_ids
        ),
        None,
    )
    if absence_row is None:
        return False
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absence_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning=_CONFIRMATION_REASON,
            linked_at_turn=case.current_turn,
        )
    )
    return True


def conclusion_overclaims(rcc, grade: CauseAssuranceGrade) -> bool:
    """The M2 over-claim seam predicate — ONE definition shared by the WARNING
    in the per-turn recompute, the ``seam_overclaim`` flag in the DEBUG
    grounding trace, and the terminal re-grade, so the prod signal and the
    greppable trace can never disagree about the same turn."""
    return (
        rcc is not None
        and rcc.confidence_level == ConfidenceLevel.VERIFIED
        and grade != CauseAssuranceGrade.CONFIRMED
    )


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM must not turn an unverified cause into reusable
    knowledge, so only ``CONFIRMED`` — a validated root whose removal was
    observed to remove the problem — clears the bar.
    """
    validated_roots = _validated_roots(case)
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT
    evidence_category_by_id = evidence_category_map(case)
    if any(
        root_counterfactually_confirmed(r, evidence_category_by_id)
        for r in validated_roots
    ):
        return CauseAssuranceGrade.CONFIRMED
    return CauseAssuranceGrade.MECHANISTIC
