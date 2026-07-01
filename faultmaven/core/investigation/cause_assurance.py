"""Provenance-assurance grading for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: consumers include the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it back would close an import
cycle. Keeping it here (it needs nothing from ``causal_graph``, only the case
contracts) breaks that back-edge, and lets ``causal_graph`` import the one shared
"runbook provenance is causal grounding" primitive (``support_is_runbook_grounded``)
from here without a cycle.

``grade_cause_assurance`` is the single source of truth: it classifies a case
into one of three mutually-exclusive assurance grades in one pass. The boolean
views (``cause_is_runbook_grounded`` / ``cause_validation_is_fallback_only``) are
thin wrappers over it, so they can never disagree — the wrong-predicate footgun
(a caller inverting "fallback-only" and missing the no-root case) is removed by
making the three states explicit. The §7 harvest bar is ``GROUNDED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    EvidenceStance,
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode, NodeEvidenceLink


def support_is_runbook_grounded(link: "NodeEvidenceLink") -> bool:
    """Whether a link is a ``runbook``-provenance SUPPORTS — a deterministic,
    expert-authored predicate that fired against the submitted telemetry.

    This is the ONE place the "runbook provenance IS causal grounding" rule lives.
    It counts as causal grounding REGARDLESS of the LLM's ``Evidence.category``
    choice on the backing datum (#590 A2): the predicate firing is the causal
    signal, independent of how the datum was filed. Both the node-validation tally
    (``causal_graph._node_evidence_tally``) and the harvest grade
    (``grade_cause_assurance``) read this primitive so the rule can't drift.
    """
    return link.stance == EvidenceStance.SUPPORTS and link.provenance == "runbook"


class CauseAssuranceGrade(str, Enum):
    """The assurance behind a case's identified cause, as one of three mutually
    exclusive grades. ``GROUNDED`` is the §7 bar for auto-seeding reusable
    knowledge; the other two are held back, for different user-facing reasons."""

    NO_ROOT = "no_root"
    """No VALIDATED root at all — a pure LLM-authored RootCauseConclusion with zero
    causal graph (#590 A1). Not graph-identified; ask the user to identify a cause."""

    FALLBACK_ONLY = "fallback_only"
    """≥1 VALIDATED root, but every one rests only on lower-assurance (``None`` /
    ``llm_fallback``) support — never an authority-grounded one. Graph-identified
    but unverified; ask the user to verify it."""

    GROUNDED = "grounded"
    """≥1 VALIDATED root borne out by an AUTHORITY-GROUNDED support — a
    ``runbook``-provenance SUPPORTS, or a DEDUCTIVE derivation (proof-by-exclusion,
    §7.1.1). The only grade that may auto-seed reusable knowledge."""


def _validated_roots(case: "Case") -> list["CausalNode"]:
    """The case's VALIDATED root nodes — the only harvest-relevant unit (§7 never
    harvests an intermediate rung or a candidate). The single home for the
    validated-root filter, shared by the grade and the counter so they cannot
    disagree on what a validated root is."""
    return [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]


def _root_grounding(root: "CausalNode", evidence_ids: set[str]) -> tuple[bool, bool]:
    """The shared per-root grounding classifier: ``(is_deductive, is_runbook)``.

    The ONE place the two grounding arms are decided for a validated root, so the
    §7 grade (``grade_cause_assurance``) and the observability counter
    (``count_grounded_roots``) read identical logic — R1 drift-lock holds *by
    construction*, not by a mirrored copy. ``is_runbook`` requires a NON-DANGLING
    ``runbook``-provenance SUPPORTS (a link to deleted evidence never counts).
    """
    is_deductive = root.validation_method == ValidationMethod.DEDUCTIVE
    is_runbook = any(
        link.evidence_id in evidence_ids and support_is_runbook_grounded(link)
        for link in root.evidence_links
    )
    return is_deductive, is_runbook


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM with no firing runbook predicate must not turn an
    unverified cause into reusable knowledge, so only ``GROUNDED`` clears the bar.
    """
    evidence_ids = {e.evidence_id for e in case.evidence}
    validated_roots = _validated_roots(case)
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT

    for root in validated_roots:
        is_deductive, is_runbook = _root_grounding(root, evidence_ids)
        if is_deductive or is_runbook:
            return CauseAssuranceGrade.GROUNDED  # authority-grounded root
    return CauseAssuranceGrade.FALLBACK_ONLY


@dataclass(frozen=True)
class GroundedRootTally:
    """Per-case grounded-root counts, split by grounding arm (Phase 0b metric unit).

    The counted unit is the VALIDATED ROOT — §7 only ever harvests a validated root
    cause, never an intermediate rung or a candidate — tagged with the arm(s) that
    grounded it. Rolls up losslessly to the case grade:
    ``grade_cause_assurance(case) == GROUNDED`` **iff** ``grounded_roots >= 1``. The two
    are provably identical because both read the same primitives (drift-lock, §7-grade
    counting definition R1).
    """

    grounded_roots: int
    """Validated roots grounded by the runbook arm OR the deductive arm (set-union
    count — the primary number). MUST NOT be reported as ``runbook_arm + deductive_arm``:
    the arms are non-exclusive, so a root grounded by both is counted once here but
    appears in both arm tallies (R2 sum-safety)."""

    runbook_arm: int
    """Validated roots carrying >=1 non-dangling ``runbook``-provenance SUPPORTS link.
    Rising off 0 is the acceptance signal for Part A (retrieval-seeded differential)."""

    deductive_arm: int
    """Validated roots with ``validation_method == DEDUCTIVE``. Rising off 0 is the
    acceptance signal for #593 (deductive-arm wiring); a live 0 today is the unwired
    arm, not a metric gap — surface it plainly."""

    validated_roots: int
    """All validated roots, grounded or not — the per-case context for the
    FALLBACK_ONLY gap (identified but not authority-grounded)."""

    runbook_links_fired: int
    """Leading indicator: non-dangling ``runbook``-provenance SUPPORTS links on ANY
    node (root or intermediate), not just validated roots. Moves off 0 FIRST when Part
    A's differential loop goes live — before any root reaches VALIDATED — so it is the
    earliest proof the loop is alive. Diagnostic ONLY: never a grounding count, never
    summed into ``grounded_roots``. (Deductive has no partial analogue — a node is
    DEDUCTIVE or it is not — so this signal is runbook-arm-only by nature.)"""


def count_grounded_roots(case: "Case") -> GroundedRootTally:
    """Count a case's grounded root causes per arm, reusing the exact primitives
    ``grade_cause_assurance`` reads so the metric can never drift from the §7 gate.

    Shares ``grade_cause_assurance``'s exact primitives (``_validated_roots`` +
    ``_root_grounding``) rather than mirroring them, so it cannot drift from the §7
    gate; it counts every root and the arm(s) that grounded it instead of
    short-circuiting on the first grounded root, recovering the two facts the
    case-level grade discards: how many roots grounded, and via which arm.
    ``grade_cause_assurance(case) == GROUNDED`` iff ``grounded_roots >= 1``.
    """
    evidence_ids = {e.evidence_id for e in case.evidence}
    validated_roots = _validated_roots(case)

    runbook_arm = 0
    deductive_arm = 0
    grounded_roots = 0
    for root in validated_roots:
        is_deductive, is_runbook = _root_grounding(root, evidence_ids)
        if is_deductive:
            deductive_arm += 1
        if is_runbook:
            runbook_arm += 1
        if is_deductive or is_runbook:
            grounded_roots += 1

    # Leading indicator: runbook predicates that fired on ANY node (not just validated
    # roots), non-dangling. Same primitive, wider scope — see field docstring.
    runbook_links_fired = sum(
        1
        for node in case.causal_nodes.values()
        for link in node.evidence_links
        if link.evidence_id in evidence_ids and support_is_runbook_grounded(link)
    )

    return GroundedRootTally(
        grounded_roots=grounded_roots,
        runbook_arm=runbook_arm,
        deductive_arm=deductive_arm,
        validated_roots=len(validated_roots),
        runbook_links_fired=runbook_links_fired,
    )


def cause_is_runbook_grounded(case: "Case") -> bool:
    """Whether the cause clears the §7 harvest bar — an AUTHORITY-GROUNDED support
    (runbook predicate or deductive derivation) borne out by ≥1 VALIDATED root.

    Thin view over ``grade_cause_assurance``; equivalent to grade ``GROUNDED``.
    Holds BOTH the no-root (#590 A1) and the fallback-only cases.
    """
    return grade_cause_assurance(case) == CauseAssuranceGrade.GROUNDED


def cause_validation_is_fallback_only(case: "Case") -> bool:
    """Whether an IDENTIFIED cause (≥1 VALIDATED root) rests ONLY on lower-assurance
    validation — graph-identified but never authority-grounded.

    Thin view over ``grade_cause_assurance``; equivalent to grade ``FALLBACK_ONLY``.
    Deliberately False when there is no validated root (that is ``NO_ROOT``, not
    fallback-only) — do NOT invert it as a harvest bar; use
    ``cause_is_runbook_grounded`` (``GROUNDED``), which holds the no-root case too.
    """
    return grade_cause_assurance(case) == CauseAssuranceGrade.FALLBACK_ONLY
