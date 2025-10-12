"""Hypothesis Manager - Hypothesis Lifecycle and Confidence Management

This module manages the lifecycle of root cause hypotheses throughout the
investigation, including confidence decay, anchoring prevention, and
hypothesis testing coordination.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Hypothesis Lifecycle:
- PENDING: Generated but not yet tested
- TESTING: Currently under evaluation
- VALIDATED: Confirmed by evidence (confidence ≥70%)
- REFUTED: Disproved by evidence
- RETIRED: Abandoned due to low confidence or anchoring

Anchoring Prevention:
- Detect when 4+ hypotheses in same category
- Detect when 3+ iterations without progress
- Trigger forced alternative generation
- Apply confidence decay to stagnant hypotheses

Confidence Decay:
- Base confidence × 0.85^iterations_without_progress
- Retire when confidence < 0.3
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.models.investigation import (
    Hypothesis,
    HypothesisStatus,
    HypothesisTest,
    InvestigationState,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Hypothesis Manager
# =============================================================================


class HypothesisManager:
    """Manages hypothesis lifecycle and confidence throughout investigation

    Responsibilities:
    - Create new hypotheses
    - Update hypothesis confidence based on evidence
    - Apply confidence decay for stagnant hypotheses
    - Detect and prevent anchoring bias
    - Retire low-confidence hypotheses
    - Track hypothesis testing
    """

    def __init__(self):
        """Initialize hypothesis manager"""
        self.logger = logging.getLogger(__name__)

    def create_hypothesis(
        self,
        statement: str,
        category: str,
        initial_likelihood: float,
        current_turn: int,
    ) -> Hypothesis:
        """Create a new hypothesis

        Args:
            statement: Hypothesis statement describing root cause
            category: Category (infrastructure, code, config, etc.)
            initial_likelihood: Initial confidence (0.0 to 1.0)
            current_turn: Current conversation turn

        Returns:
            New Hypothesis object
        """
        hypothesis = Hypothesis(
            statement=statement,
            category=category,
            likelihood=initial_likelihood,
            initial_likelihood=initial_likelihood,
            confidence_trajectory=[(current_turn, initial_likelihood)],
            status=HypothesisStatus.PENDING,
            created_at_turn=current_turn,
            last_updated_turn=current_turn,
            last_progress_at_turn=current_turn,
        )

        self.logger.info(
            f"Created hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (category={category}, likelihood={initial_likelihood})"
        )

        return hypothesis

    def update_hypothesis_confidence(
        self,
        hypothesis: Hypothesis,
        new_likelihood: float,
        current_turn: int,
        reason: str,
    ) -> Hypothesis:
        """Update hypothesis confidence based on new evidence

        Args:
            hypothesis: Hypothesis to update
            new_likelihood: New confidence level (0.0 to 1.0)
            current_turn: Current conversation turn
            reason: Reason for confidence change

        Returns:
            Updated hypothesis
        """
        old_likelihood = hypothesis.likelihood
        hypothesis.likelihood = max(0.0, min(1.0, new_likelihood))  # Clamp to [0, 1]
        hypothesis.last_updated_turn = current_turn
        hypothesis.confidence_trajectory.append((current_turn, hypothesis.likelihood))

        # Check if this represents progress
        if abs(new_likelihood - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = current_turn
            hypothesis.iterations_without_progress = 0
            self.logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} confidence updated: "
                f"{old_likelihood:.2f} → {new_likelihood:.2f} ({reason})"
            )
        else:
            hypothesis.iterations_without_progress += 1
            self.logger.debug(
                f"Hypothesis {hypothesis.hypothesis_id}: minimal change, "
                f"iterations_without_progress={hypothesis.iterations_without_progress}"
            )

        # Update status based on confidence
        if hypothesis.likelihood >= 0.7 and hypothesis.status == HypothesisStatus.TESTING:
            hypothesis.status = HypothesisStatus.VALIDATED
            self.logger.info(f"Hypothesis {hypothesis.hypothesis_id} VALIDATED (≥70% confidence)")

        elif hypothesis.likelihood < 0.3 and hypothesis.status != HypothesisStatus.RETIRED:
            hypothesis.status = HypothesisStatus.RETIRED
            hypothesis.retirement_reason = "Low confidence after testing"
            self.logger.info(f"Hypothesis {hypothesis.hypothesis_id} RETIRED (confidence < 30%)")

        return hypothesis

    def apply_confidence_decay(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
    ) -> Hypothesis:
        """Apply confidence decay to stagnant hypothesis

        Decay formula: base_confidence × 0.85^iterations_without_progress

        Args:
            hypothesis: Hypothesis to decay
            current_turn: Current conversation turn

        Returns:
            Updated hypothesis with decayed confidence
        """
        if hypothesis.iterations_without_progress < 2:
            return hypothesis  # No decay needed

        old_likelihood = hypothesis.likelihood
        hypothesis.likelihood = hypothesis.apply_confidence_decay(current_turn)

        self.logger.info(
            f"Applied confidence decay to {hypothesis.hypothesis_id}: "
            f"{old_likelihood:.2f} → {hypothesis.likelihood:.2f} "
            f"({hypothesis.iterations_without_progress} iterations without progress)"
        )

        if hypothesis.status == HypothesisStatus.RETIRED:
            self.logger.warning(
                f"Hypothesis {hypothesis.hypothesis_id} retired due to confidence decay"
            )

        return hypothesis

    def refute_hypothesis(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
        refuting_evidence_ids: List[str],
        reason: str,
    ) -> Hypothesis:
        """Mark hypothesis as refuted by evidence

        Args:
            hypothesis: Hypothesis to refute
            current_turn: Current conversation turn
            refuting_evidence_ids: IDs of evidence that refutes hypothesis
            reason: Explanation of why hypothesis refuted

        Returns:
            Refuted hypothesis
        """
        hypothesis.status = HypothesisStatus.REFUTED
        hypothesis.likelihood = 0.0
        hypothesis.refuting_evidence.extend(refuting_evidence_ids)
        hypothesis.retirement_reason = reason
        hypothesis.last_updated_turn = current_turn

        self.logger.info(
            f"Hypothesis {hypothesis.hypothesis_id} REFUTED: {reason} "
            f"(evidence: {refuting_evidence_ids})"
        )

        return hypothesis

    def record_hypothesis_test(
        self,
        hypothesis: Hypothesis,
        test_description: str,
        evidence_required: List[str],
        evidence_obtained: List[str],
        result: str,
        confidence_change: float,
        current_turn: int,
        ooda_iteration: int,
    ) -> HypothesisTest:
        """Record a hypothesis test execution

        Args:
            hypothesis: Hypothesis being tested
            test_description: What was tested
            evidence_required: Evidence request IDs needed
            evidence_obtained: Evidence provided IDs received
            result: supports, refutes, inconclusive
            confidence_change: Change in hypothesis likelihood
            current_turn: Current conversation turn
            ooda_iteration: Which OODA iteration

        Returns:
            HypothesisTest record
        """
        test = HypothesisTest(
            hypothesis_id=hypothesis.hypothesis_id,
            test_description=test_description,
            evidence_required=evidence_required,
            evidence_obtained=evidence_obtained,
            result=result,
            confidence_change=confidence_change,
            executed_at_turn=current_turn,
            ooda_iteration=ooda_iteration,
        )

        # Update hypothesis based on test result
        if result == "supports":
            new_likelihood = min(1.0, hypothesis.likelihood + abs(confidence_change))
            hypothesis.supporting_evidence.extend(evidence_obtained)
        elif result == "refutes":
            new_likelihood = max(0.0, hypothesis.likelihood - abs(confidence_change))
            hypothesis.refuting_evidence.extend(evidence_obtained)
        else:  # inconclusive
            new_likelihood = hypothesis.likelihood

        self.update_hypothesis_confidence(
            hypothesis,
            new_likelihood,
            current_turn,
            f"Test result: {result}",
        )

        self.logger.info(
            f"Recorded test for {hypothesis.hypothesis_id}: {result} "
            f"(confidence change: {confidence_change:+.2f})"
        )

        return test

    def detect_anchoring(
        self,
        hypotheses: List[Hypothesis],
        current_iteration: int,
    ) -> Tuple[bool, Optional[str], List[str]]:
        """Detect anchoring bias in hypothesis generation/testing

        Anchoring conditions:
        1. 4+ hypotheses in same category
        2. 3+ iterations without progress on any hypothesis
        3. Top hypothesis unchanged for 3+ iterations with <70% confidence

        Args:
            hypotheses: List of all hypotheses
            current_iteration: Current OODA iteration number

        Returns:
            Tuple of (is_anchored, reason, affected_hypothesis_ids)
        """
        active_hypotheses = [
            h
            for h in hypotheses
            if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]
        ]

        if not active_hypotheses:
            return False, None, []

        # Condition 1: Too many in same category
        category_counts: Dict[str, List[str]] = {}
        for h in active_hypotheses:
            if h.category not in category_counts:
                category_counts[h.category] = []
            category_counts[h.category].append(h.hypothesis_id)

        for category, hypothesis_ids in category_counts.items():
            if len(hypothesis_ids) >= 4:
                return (
                    True,
                    f"Anchoring: {len(hypothesis_ids)} hypotheses in '{category}' category",
                    hypothesis_ids,
                )

        # Condition 2: No progress for 3+ iterations
        stalled_hypotheses = [
            h.hypothesis_id
            for h in active_hypotheses
            if h.iterations_without_progress >= 3
        ]
        if len(stalled_hypotheses) >= 2:
            return (
                True,
                f"Anchoring: {len(stalled_hypotheses)} hypotheses stalled for 3+ iterations",
                stalled_hypotheses,
            )

        # Condition 3: Top hypothesis stagnant
        sorted_by_likelihood = sorted(
            active_hypotheses, key=lambda h: h.likelihood, reverse=True
        )
        if sorted_by_likelihood:
            top_hypothesis = sorted_by_likelihood[0]
            iterations_stagnant = top_hypothesis.iterations_without_progress

            if iterations_stagnant >= 3 and top_hypothesis.likelihood < 0.7:
                return (
                    True,
                    f"Anchoring: Top hypothesis stagnant for {iterations_stagnant} iterations "
                    f"with only {top_hypothesis.likelihood:.0%} confidence",
                    [top_hypothesis.hypothesis_id],
                )

        return False, None, []

    def force_alternative_generation(
        self,
        existing_hypotheses: List[Hypothesis],
        current_turn: int,
    ) -> Dict[str, Any]:
        """Force generation of alternative hypotheses to break anchoring

        Strategy:
        - Identify over-represented categories
        - Generate constraints for alternative generation
        - Retire some low-progress hypotheses

        Args:
            existing_hypotheses: Current hypothesis list
            current_turn: Current conversation turn

        Returns:
            Generation constraints and actions taken
        """
        # Identify over-represented categories
        category_counts: Dict[str, int] = {}
        for h in existing_hypotheses:
            if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]:
                category_counts[h.category] = category_counts.get(h.category, 0) + 1

        # Find dominant category
        dominant_category = max(category_counts, key=category_counts.get)
        dominant_count = category_counts[dominant_category]

        # Retire low-progress hypotheses in dominant category
        retired_count = 0
        for h in existing_hypotheses:
            if (
                h.category == dominant_category
                and h.iterations_without_progress >= 2
                and h.status == HypothesisStatus.TESTING
            ):
                h.status = HypothesisStatus.RETIRED
                h.retirement_reason = f"Anchoring prevention: retired to diversify from {dominant_category}"
                h.last_updated_turn = current_turn
                retired_count += 1

        self.logger.warning(
            f"Anchoring prevention triggered: retired {retired_count} hypotheses "
            f"from over-represented category '{dominant_category}'"
        )

        return {
            "action": "force_alternative_generation",
            "retired_count": retired_count,
            "dominant_category": dominant_category,
            "constraints": {
                "exclude_categories": [dominant_category],
                "require_diverse_categories": True,
                "min_new_hypotheses": 2,
            },
        }

    def get_testable_hypotheses(
        self,
        hypotheses: List[Hypothesis],
        max_count: int = 3,
    ) -> List[Hypothesis]:
        """Get list of hypotheses ready for testing, sorted by priority

        Priority:
        1. Highest likelihood
        2. Not yet tested (PENDING status)
        3. Has supporting evidence

        Args:
            hypotheses: All hypotheses
            max_count: Maximum number to return

        Returns:
            Sorted list of testable hypotheses
        """
        testable = [
            h
            for h in hypotheses
            if h.status in [HypothesisStatus.PENDING, HypothesisStatus.TESTING]
            and h.likelihood > 0.2  # Skip very low confidence
        ]

        # Sort by likelihood (descending)
        sorted_hypotheses = sorted(testable, key=lambda h: h.likelihood, reverse=True)

        return sorted_hypotheses[:max_count]

    def get_validated_hypothesis(
        self,
        hypotheses: List[Hypothesis],
    ) -> Optional[Hypothesis]:
        """Get the validated root cause hypothesis if any

        Args:
            hypotheses: All hypotheses

        Returns:
            Validated hypothesis with highest confidence, or None
        """
        validated = [
            h
            for h in hypotheses
            if h.status == HypothesisStatus.VALIDATED and h.likelihood >= 0.7
        ]

        if not validated:
            return None

        # Return highest confidence validated hypothesis
        return max(validated, key=lambda h: h.likelihood)

    def get_hypothesis_summary(
        self,
        hypotheses: List[Hypothesis],
    ) -> Dict[str, Any]:
        """Get summary statistics of hypothesis state

        Args:
            hypotheses: All hypotheses

        Returns:
            Summary dictionary
        """
        status_counts = {status: 0 for status in HypothesisStatus}
        for h in hypotheses:
            status_counts[h.status] += 1

        active_hypotheses = [
            h
            for h in hypotheses
            if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]
        ]

        return {
            "total_count": len(hypotheses),
            "active_count": len(active_hypotheses),
            "status_breakdown": {status.value: count for status, count in status_counts.items()},
            "max_confidence": max((h.likelihood for h in active_hypotheses), default=0.0),
            "avg_confidence": (
                sum(h.likelihood for h in active_hypotheses) / len(active_hypotheses)
                if active_hypotheses
                else 0.0
            ),
            "categories": list(set(h.category for h in active_hypotheses)),
        }


# =============================================================================
# Utility Functions
# =============================================================================


def create_hypothesis_manager() -> HypothesisManager:
    """Factory function to create hypothesis manager

    Returns:
        Configured HypothesisManager instance
    """
    return HypothesisManager()


def rank_hypotheses_by_likelihood(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """Sort hypotheses by likelihood descending

    Args:
        hypotheses: List of hypotheses

    Returns:
        Sorted list (highest likelihood first)
    """
    return sorted(hypotheses, key=lambda h: h.likelihood, reverse=True)
