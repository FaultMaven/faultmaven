"""Evidence Milestone Validation

Validates that LLM milestone claims are supported by cited evidence.

The LLM is the sole authority for milestone advancement via structured output.
This module does NOT independently advance milestones. Instead, it validates
that when the LLM claims a milestone has been reached, it has cited sufficient
evidence IDs to justify that claim.

Design Decision (Issue A):
- LLM structured output is the sole authority for milestone advancement.
- The evidence processor is a validation layer, not a discovery layer.
- If the LLM claims a milestone without citing evidence, the claim is logged
  as a warning but still applied (the LLM may have reasoning not captured in
  evidence). Future iterations may reject unsupported claims.

Reference: workflow-design-review.md Issue A
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from faultmaven.modules.case.contracts import Case, EvidenceCategory

logger = logging.getLogger(__name__)


# Minimum evidence citations expected per milestone for validation
MILESTONE_EVIDENCE_EXPECTATIONS: Dict[str, Dict] = {
    "symptom_verified": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.SYMPTOM_EVIDENCE],
        "description": "At least 1 symptom evidence item confirming the reported symptom",
    },
    "root_cause_identified": {
        "min_evidence": 2,
        "expected_categories": [EvidenceCategory.CAUSAL_EVIDENCE],
        "description": "At least 2 causal evidence items supporting the root cause",
    },
    "solution_proposed": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.CAUSAL_EVIDENCE],
        "description": "At least 1 evidence item justifying the proposed solution",
    },
    # Stage-gate milestones are NOT evidence-validated — they are set by
    # the LLM in structured output when it detects user compliance (Framework §4.1).
    "solution_accepted": {
        "min_evidence": 0,
        "expected_categories": [],
        "description": "Stage-gate: LLM sets when user submits solution execution results",
    },
    "mitigation_accepted": {
        "min_evidence": 0,
        "expected_categories": [],
        "description": "Stage-gate: LLM sets when user submits mitigation execution results",
    },
}


@dataclass
class MilestoneValidationResult:
    """Result of validating a milestone claim against cited evidence."""

    milestone: str
    is_valid: bool
    cited_evidence_ids: List[str]
    cited_count: int
    expected_min: int
    expected_categories: List[str]
    actual_categories: List[str]
    warnings: List[str] = field(default_factory=list)


def validate_milestone_claims(
    case: Case,
    milestones_claimed: List[str],
    reasoning: Optional[object] = None,
) -> List[MilestoneValidationResult]:
    """
    Validate that LLM milestone claims are supported by evidence.

    This does NOT advance milestones. It checks whether the LLM's claims
    are justified by evidence in the current turn or by cited turn numbers.

    Validation Strategy:
    - Current-turn evidence: Check if evidence with correct category exists
      in current turn (no ID citation needed)
    - Historical evidence (optional): LLM can cite turn numbers via
      evidence_analyzed: ["turn_2"] to reference past evidence

    Args:
        case: Current case state
        milestones_claimed: Milestone names the LLM is claiming as completed
        reasoning: InternalReasoning object (evidence_analyzed optional)

    Returns:
        List of validation results, one per claimed milestone
    """
    results = []

    # Build lookup of ALL evidence by category (not just cited)
    # This allows category-based validation for current-turn evidence
    all_evidence_by_category: Dict[str, List[str]] = {}
    current_turn_evidence_by_category: Dict[str, List[str]] = {}

    for ev in case.evidence:
        cat = ev.category.value if ev.category else "unknown"
        all_evidence_by_category.setdefault(cat, []).append(ev.evidence_id)

        # Track current-turn evidence separately
        if ev.collected_at_turn == case.current_turn:
            current_turn_evidence_by_category.setdefault(cat, []).append(ev.evidence_id)

    # Extract cited turn numbers from reasoning (optional for historical references)
    cited_turns: List[int] = []
    if reasoning and hasattr(reasoning, "evidence_analyzed"):
        evidence_refs = reasoning.evidence_analyzed or []
        for ref in evidence_refs:
            if isinstance(ref, str) and ref.startswith("turn_"):
                try:
                    turn_num = int(ref.split("_")[1])
                    cited_turns.append(turn_num)
                except (IndexError, ValueError):
                    logger.warning(f"Invalid turn reference format: {ref}")

    for milestone in milestones_claimed:
        expectations = MILESTONE_EVIDENCE_EXPECTATIONS.get(milestone)
        if not expectations:
            results.append(
                MilestoneValidationResult(
                    milestone=milestone,
                    is_valid=True,
                    cited_evidence_ids=[],
                    cited_count=0,
                    expected_min=0,
                    expected_categories=[],
                    actual_categories=list(all_evidence_by_category.keys()),
                    warnings=[
                        f"No validation expectations defined for milestone '{milestone}'"
                    ],
                )
            )
            continue

        expected_min = expectations["min_evidence"]
        expected_cats = [c.value for c in expectations["expected_categories"]]

        # Count evidence in expected categories from:
        # 1. Current turn (primary validation path)
        # 2. Cited historical turns (optional, for referencing past evidence)
        relevant_evidence = []

        # Check current turn first (most common case)
        for cat in expected_cats:
            relevant_evidence.extend(current_turn_evidence_by_category.get(cat, []))

        # If not enough evidence from current turn, check cited historical turns
        if len(relevant_evidence) < expected_min and cited_turns:
            for ev in case.evidence:
                if ev.collected_at_turn in cited_turns:
                    cat = ev.category.value if ev.category else "unknown"
                    if cat in expected_cats and ev.evidence_id not in relevant_evidence:
                        relevant_evidence.append(ev.evidence_id)

        total_relevant = len(relevant_evidence)
        is_valid = total_relevant >= expected_min

        warnings = []
        if not is_valid:
            warnings.append(
                f"Milestone '{milestone}' claimed with {total_relevant} relevant evidence "
                f"(expected >= {expected_min}). "
                f"Expected categories: {expected_cats}. "
                f"Current turn evidence: {list(current_turn_evidence_by_category.keys())}. "
                f"Cited turns: {cited_turns}"
            )
            logger.warning(warnings[-1])
        else:
            logger.info(
                f"Milestone '{milestone}' validated: {total_relevant} relevant evidence "
                f"(expected >= {expected_min})"
            )

        # Check milestone justification in reasoning (optional but recommended)
        if reasoning and hasattr(reasoning, "milestone_justifications"):
            justifications = reasoning.milestone_justifications or {}
            if milestone not in justifications:
                # Don't fail validation, just log a warning
                logger.debug(
                    f"Milestone '{milestone}' claimed without justification in internal_reasoning"
                )

        results.append(
            MilestoneValidationResult(
                milestone=milestone,
                is_valid=is_valid,
                cited_evidence_ids=relevant_evidence,
                cited_count=total_relevant,
                expected_min=expected_min,
                expected_categories=expected_cats,
                actual_categories=list(current_turn_evidence_by_category.keys()),
                warnings=warnings,
            )
        )

    return results
