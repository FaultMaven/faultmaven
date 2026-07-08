"""Assurance grading for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: consumers include the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it back would close an import
cycle. Keeping it here (it needs nothing from ``causal_graph``, only the case
contracts) breaks that back-edge.

``grade_cause_assurance`` is the single source of truth: it classifies a case
into one of three mutually-exclusive assurance grades in one pass. The §7
harvest bar is ``GROUNDED``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode


class CauseAssuranceGrade(str, Enum):
    """The assurance behind a case's identified cause, as one of three mutually
    exclusive grades. ``GROUNDED`` is the §7 bar for auto-seeding reusable
    knowledge; the other two are held back, for different user-facing reasons."""

    NO_ROOT = "no_root"
    """No VALIDATED root at all — a pure LLM-authored RootCauseConclusion with zero
    causal graph (#590 A1). Not graph-identified; ask the user to identify a cause."""

    FALLBACK_ONLY = "fallback_only"
    """≥1 VALIDATED root, but every one rests only on empirical (LLM-mediated)
    validation — never a deductive derivation. Graph-identified but unverified;
    ask the user to verify it."""

    GROUNDED = "grounded"
    """≥1 VALIDATED root borne out by a DEDUCTIVE derivation (proof-by-exclusion,
    §7.1.1). The only grade that may auto-seed reusable knowledge."""


def _validated_roots(case: "Case") -> list["CausalNode"]:
    """The case's VALIDATED root nodes — the only harvest-relevant unit (§7 never
    harvests an intermediate rung or a candidate)."""
    return [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM must not turn an unverified cause into reusable
    knowledge, so only ``GROUNDED`` — a deductive derivation — clears the bar.
    """
    validated_roots = _validated_roots(case)
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT

    for root in validated_roots:
        if root.validation_method == ValidationMethod.DEDUCTIVE:
            return CauseAssuranceGrade.GROUNDED
    return CauseAssuranceGrade.FALLBACK_ONLY
