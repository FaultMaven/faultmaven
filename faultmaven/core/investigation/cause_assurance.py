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

from enum import Enum
from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    EvidenceStance,
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, NodeEvidenceLink


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


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM with no firing runbook predicate must not turn an
    unverified cause into reusable knowledge, so only ``GROUNDED`` clears the bar.
    """
    evidence_ids = {e.evidence_id for e in case.evidence}
    validated_roots = [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT

    for root in validated_roots:
        if root.validation_method == ValidationMethod.DEDUCTIVE:
            return CauseAssuranceGrade.GROUNDED  # deductive proof-by-exclusion
        for link in root.evidence_links:
            if link.evidence_id not in evidence_ids:
                continue  # dangling reference (deleted evidence) — never counts
            if support_is_runbook_grounded(link):
                return CauseAssuranceGrade.GROUNDED  # authority-grounded predicate
    return CauseAssuranceGrade.FALLBACK_ONLY


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
