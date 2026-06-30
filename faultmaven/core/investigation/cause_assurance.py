"""Provenance-assurance check for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: the only consumer is the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it from ``terminal_transitions``
would close an import cycle. Keeping it here (it needs nothing from
``causal_graph``, only the case contracts) breaks that back-edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    EvidenceStance,
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


def _has_validated_root(case: "Case") -> bool:
    """Whether the case's causal graph has any VALIDATED ROOT at all (i.e. a cause
    is graph-identified, not merely an LLM-authored RootCauseConclusion prose)."""
    return any(
        n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
        for n in case.causal_nodes.values()
    )


def cause_is_runbook_grounded(case: "Case") -> bool:
    """Whether the cause is borne out by an AUTHORITY-GROUNDED support — the bar
    for auto-seeding reusable knowledge (KB harvest, §7).

    True iff at least one VALIDATED root is validated either:

    - by a ``runbook``-provenance SUPPORTS link — a deterministic, expert-authored
      predicate fired against submitted telemetry. This counts REGARDLESS of how
      the LLM categorized the backing datum: a runbook predicate firing IS causal
      grounding, independent of the orthogonal ``Evidence.category`` choice (#590
      A2 — otherwise a sound signal is silently dropped when the LLM files the
      datum as e.g. SYMPTOM_EVIDENCE); or
    - DEDUCTIVELY (proof-by-exclusion, §7.1.1) — a methodology derivation, sound.

    Returns False when there is NO validated root at all — a pure LLM-authored
    RootCauseConclusion with zero causal graph (#590 A1, the §7 harvest hole) — and
    when every validated root rests only on lower-assurance (``None`` /
    ``llm_fallback``) support. Both are HELD from harvest: a confidently-wrong LLM
    with no firing runbook predicate must not turn an unverified cause into
    reusable knowledge.
    """
    evidence_by_id = {e.evidence_id for e in case.evidence}
    validated_roots = [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]
    if not validated_roots:
        return False  # pure LLM-authored RCC, no graph — not authority-grounded

    for root in validated_roots:
        if root.validation_method == ValidationMethod.DEDUCTIVE:
            return True  # deductive proof-by-exclusion is sound
        for link in root.evidence_links:
            if link.evidence_id not in evidence_by_id:
                continue  # dangling reference (deleted evidence) — never counts
            if link.stance == EvidenceStance.SUPPORTS and link.provenance == "runbook":
                return True  # authority-grounded (runbook predicate, any category)
    return False


def cause_validation_is_fallback_only(case: "Case") -> bool:
    """Whether an IDENTIFIED cause (≥1 VALIDATED root) rests ONLY on lower-assurance
    validation — every validated root is borne out by ``None`` / ``llm_fallback``
    support, never an authority-grounded (``runbook`` / deductive) one.

    This is the "fallback-only AMONG validated roots" view, used to label / hold a
    graph-identified-but-unverified cause. It is deliberately False when there is
    NO validated root: that case is not "fallback-only", it is simply not
    graph-identified at all — and the harvest bar is ``cause_is_runbook_grounded``
    (which holds BOTH the no-root and the fallback-only cases), not this function.
    """
    return _has_validated_root(case) and not cause_is_runbook_grounded(case)
