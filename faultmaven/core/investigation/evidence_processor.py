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

from faultmaven.modules.case.contracts import (
    Case,
    EvidenceCategory,
)

logger = logging.getLogger(__name__)


# Minimum evidence citations expected per milestone for validation
MILESTONE_EVIDENCE_EXPECTATIONS: Dict[str, Dict] = {
    "symptom_verified": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.SYMPTOM_EVIDENCE],
        "description": "At least 1 symptom evidence item confirming the reported symptom",
    },
    "scope_assessed": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.SYMPTOM_EVIDENCE],
        "description": "At least 1 evidence item identifying impact scope",
    },
    "timeline_established": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.SYMPTOM_EVIDENCE],
        "description": "At least 1 evidence item with temporal data",
    },
    "changes_identified": {
        "min_evidence": 1,
        "expected_categories": [EvidenceCategory.SYMPTOM_EVIDENCE, EvidenceCategory.CAUSAL_EVIDENCE],
        "description": "At least 1 evidence item linking to recent changes",
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
    "solution_applied": {
        "min_evidence": 0,
        "expected_categories": [],
        "description": "User confirmation that solution was applied (no evidence required)",
    },
    "mitigation_applied": {
        "min_evidence": 0,
        "expected_categories": [],
        "description": "User confirmation that mitigation was applied (no evidence required)",
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
    Validate that LLM milestone claims are supported by cited evidence.

    This does NOT advance milestones. It checks whether the LLM's claims
    are justified by the evidence IDs cited in internal_reasoning.

    Args:
        case: Current case state
        milestones_claimed: Milestone names the LLM is claiming as completed
        reasoning: InternalReasoning object with evidence_analyzed IDs

    Returns:
        List of validation results, one per claimed milestone
    """
    results = []

    # Extract cited evidence IDs from reasoning
    cited_ids: List[str] = []
    if reasoning and hasattr(reasoning, "evidence_analyzed"):
        cited_ids = reasoning.evidence_analyzed or []

    # Build lookup of cited evidence by category
    cited_evidence_by_category: Dict[str, List[str]] = {}
    for ev_id in cited_ids:
        ev = next((e for e in case.evidence if e.evidence_id == ev_id), None)
        if ev:
            cat = ev.category.value if ev.category else "unknown"
            cited_evidence_by_category.setdefault(cat, []).append(ev_id)

    for milestone in milestones_claimed:
        expectations = MILESTONE_EVIDENCE_EXPECTATIONS.get(milestone)
        if not expectations:
            results.append(
                MilestoneValidationResult(
                    milestone=milestone,
                    is_valid=True,
                    cited_evidence_ids=cited_ids,
                    cited_count=len(cited_ids),
                    expected_min=0,
                    expected_categories=[],
                    actual_categories=list(cited_evidence_by_category.keys()),
                    warnings=[f"No validation expectations defined for milestone '{milestone}'"],
                )
            )
            continue

        expected_min = expectations["min_evidence"]
        expected_cats = [c.value for c in expectations["expected_categories"]]

        # Count evidence in expected categories
        relevant_evidence = []
        for cat in expected_cats:
            relevant_evidence.extend(cited_evidence_by_category.get(cat, []))

        # Also count UNCLASSIFIED evidence as potentially relevant
        # (user data that hasn't been promoted yet)
        unclassified = cited_evidence_by_category.get(
            EvidenceCategory.UNCLASSIFIED.value, []
        )

        total_relevant = len(relevant_evidence) + len(unclassified)
        is_valid = total_relevant >= expected_min

        warnings = []
        if not is_valid:
            warnings.append(
                f"Milestone '{milestone}' claimed with {total_relevant} relevant evidence "
                f"(expected >= {expected_min}). "
                f"Expected categories: {expected_cats}. "
                f"Cited: {cited_ids}"
            )
            logger.warning(warnings[-1])
        else:
            logger.info(
                f"Milestone '{milestone}' validated: {total_relevant} relevant evidence "
                f"(expected >= {expected_min})"
            )

        # Check milestone justification in reasoning
        if reasoning and hasattr(reasoning, "milestone_justifications"):
            justifications = reasoning.milestone_justifications or {}
            if milestone not in justifications:
                warnings.append(
                    f"Milestone '{milestone}' claimed without justification in internal_reasoning"
                )
                logger.warning(warnings[-1])

        results.append(
            MilestoneValidationResult(
                milestone=milestone,
                is_valid=is_valid,
                cited_evidence_ids=relevant_evidence,
                cited_count=total_relevant,
                expected_min=expected_min,
                expected_categories=expected_cats,
                actual_categories=list(cited_evidence_by_category.keys()),
                warnings=warnings,
            )
        )

    return results
