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
    EvidenceCategory,
    EvidenceStance,
    NodeState,
    NodeType,
    ValidationMethod,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case


def cause_validation_is_fallback_only(case: "Case") -> bool:
    """Whether the case's cause-identification rests ONLY on lower-assurance
    validation — i.e. NO validated root was borne out by an authority-grounded
    (``runbook``-provenance) support.

    A support link's ``provenance`` records who authored the predicate that the
    link encodes, and only one grade is SOUND:

    - ``"runbook"`` — an expert-authored predicate that fired against submitted
      telemetry. The ONLY authority-grounded (sound) tier.
    - ``"llm_fallback"`` — a predicate the LLM authored for itself when no runbook
      covered the cause; re-checking the model's own predicate is lower-assurance
      (it authors both the test and what it cites).
    - ``None`` — a link the LLM asserted directly (the emitted-chain path never
      sets provenance), or a legacy/unlabeled link. Either way it is NOT
      authority-grounded, so it is lower-assurance — the same grade as
      ``llm_fallback`` for this check.

    Returns True when the cause IS identified (at least one VALIDATED root) but no
    validated root has a ``runbook``-grounded support — its validation rests on
    the LLM's own say-so. A root validated DEDUCTIVELY (proof-by-exclusion,
    §7.1.1) is a methodology derivation, not LLM-authored, so it counts as sound.
    A single runbook-grounded (or deductive) support makes the identification
    sound.

    Downstream consumers treat a fallback-only identification as held / needing
    confirmation (e.g. it does not auto-qualify a case for runbook harvesting), so
    a confidently-wrong LLM with no firing runbook predicate cannot turn an
    unverified cause into reusable knowledge (§7).
    """
    evidence_by_id: dict[str, EvidenceCategory | None] = {
        e.evidence_id: e.category for e in case.evidence
    }
    validated_roots = [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]
    if not validated_roots:
        return False  # no identified cause — "fallback-only" does not apply

    for root in validated_roots:
        if root.validation_method == ValidationMethod.DEDUCTIVE:
            return False  # deductive proof is sound, not a fallback grade
        for link in root.evidence_links:
            if link.evidence_id not in evidence_by_id:
                continue  # dangling reference (deleted evidence) — never counts
            if (
                link.stance == EvidenceStance.SUPPORTS
                and evidence_by_id[link.evidence_id] == EvidenceCategory.CAUSAL_EVIDENCE
                and link.provenance == "runbook"
            ):
                return False  # an authority-grounded support → not fallback-only
    return True
