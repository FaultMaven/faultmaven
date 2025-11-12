"""Unified Hypothesis Manager - Complete Hypothesis Lifecycle Management

Consolidates all hypothesis management logic into ONE coherent system.

Merges:
- faultmaven/core/investigation/hypothesis_manager.py (OLD - HypothesisManager class)
- faultmaven/services/agentic/hypothesis/hypothesis_manager.py (NEW - evidence linking)

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md

Hypothesis Lifecycle (Unified):
- CAPTURED: Opportunistic hypothesis from early phases (Phases 0-2)
- ACTIVE: Currently being tested (promoted from CAPTURED or systematic generation)
- VALIDATED: Confirmed by evidence (confidence ≥70% + ≥2 supporting evidence)
- REFUTED: Disproved by evidence (confidence ≤20% + ≥2 refuting evidence)
- RETIRED: Abandoned due to low confidence or anchoring
- SUPERSEDED: Better hypothesis found

Confidence Management:
- Evidence-ratio based: initial + (0.15 × supporting) - (0.20 × refuting)
- Confidence decay for stagnation: base × 0.85^iterations_without_progress
- Auto-transition to VALIDATED/REFUTED based on thresholds

Anchoring Prevention:
- Detect: 4+ hypotheses in same category
- Detect: 3+ iterations without progress
- Detect: Top hypothesis stagnant for 3+ iterations
- Action: Retire low-progress hypotheses, force alternative generation
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationState

from faultmaven.models.investigation import (
    Hypothesis,
    HypothesisStatus,
    HypothesisGenerationMode,
    HypothesisTest,
    InvestigationPhase,
)

logger = logging.getLogger(__name__)


class HypothesisManager:
    """Unified hypothesis lifecycle and confidence management

    Responsibilities:
    - Create new hypotheses (CAPTURED or ACTIVE)
    - Update confidence based on evidence
    - Apply confidence decay for stagnation
    - Detect and prevent anchoring bias
    - Track hypothesis testing
    - Auto-transition status (VALIDATED/REFUTED)
    - Evidence linking and ratio calculation
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
        generation_mode: HypothesisGenerationMode = HypothesisGenerationMode.SYSTEMATIC,
        captured_in_phase: InvestigationPhase = InvestigationPhase.HYPOTHESIS,
        status: HypothesisStatus = HypothesisStatus.ACTIVE,
        triggering_observation: Optional[str] = None,
    ) -> Hypothesis:
        """Create a new hypothesis

        Supports both opportunistic (CAPTURED) and systematic (ACTIVE) creation.

        Args:
            statement: Hypothesis statement describing root cause
            category: Category (infrastructure, code, config, etc.)
            initial_likelihood: Initial confidence (0.0 to 1.0)
            current_turn: Current conversation turn
            generation_mode: How hypothesis was generated
            captured_in_phase: Phase where hypothesis was captured/generated
            status: Initial status (CAPTURED for opportunistic, ACTIVE for systematic)
            triggering_observation: What triggered this hypothesis (for opportunistic)

        Returns:
            New Hypothesis object
        """
        hypothesis = Hypothesis(
            statement=statement,
            category=category,
            likelihood=initial_likelihood,
            initial_likelihood=initial_likelihood,
            confidence_trajectory=[(current_turn, initial_likelihood)],
            status=status,
            generation_mode=generation_mode,
            captured_in_phase=captured_in_phase,
            captured_at_turn=current_turn,
            promoted_to_active_at_turn=current_turn if status == HypothesisStatus.ACTIVE else None,
            triggering_observation=triggering_observation,
            created_at_turn=current_turn,
            last_updated_turn=current_turn,
            last_progress_at_turn=current_turn,
        )

        self.logger.info(
            f"Created hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (category={category}, likelihood={initial_likelihood}, "
            f"mode={generation_mode.value}, status={status.value})"
        )

        return hypothesis

    def link_evidence(
        self,
        hypothesis: Hypothesis,
        evidence_id: str,
        supports: bool,
        turn: int,
    ) -> None:
        """Link evidence to hypothesis (supporting or refuting)

        Args:
            hypothesis: Hypothesis to update
            evidence_id: Evidence identifier
            supports: True if evidence supports, False if refutes
            turn: Current turn number
        """
        if supports:
            if evidence_id not in hypothesis.supporting_evidence:
                hypothesis.supporting_evidence.append(evidence_id)
                self.logger.info(
                    f"Linked supporting evidence to hypothesis: {evidence_id}",
                    extra={
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis": hypothesis.statement[:50],
                    },
                )
        else:
            if evidence_id not in hypothesis.refuting_evidence:
                hypothesis.refuting_evidence.append(evidence_id)
                self.logger.info(
                    f"Linked refuting evidence to hypothesis: {evidence_id}",
                    extra={
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis": hypothesis.statement[:50],
                    },
                )

        # Update confidence after linking evidence
        self.update_confidence_from_evidence(hypothesis, turn)

    def update_confidence_from_evidence(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Update hypothesis confidence based on evidence accumulation

        Confidence formula:
        - Start with initial_likelihood
        - Add 0.15 per supporting evidence
        - Subtract 0.20 per refuting evidence
        - Clamp to [0.0, 1.0]

        Args:
            hypothesis: Hypothesis to update
            turn: Current turn number
        """
        from faultmaven.services.agentic.hypothesis.opportunistic_capture import (
            calculate_evidence_ratio,
        )

        supporting_count = len(hypothesis.supporting_evidence)
        refuting_count = len(hypothesis.refuting_evidence)

        # Calculate new confidence
        new_confidence = hypothesis.initial_likelihood
        new_confidence += supporting_count * 0.15
        new_confidence -= refuting_count * 0.20

        # Clamp to valid range
        new_confidence = max(0.0, min(1.0, new_confidence))

        # Update hypothesis
        old_confidence = hypothesis.likelihood
        hypothesis.likelihood = new_confidence
        hypothesis.last_updated_turn = turn
        hypothesis.confidence_trajectory.append((turn, new_confidence))

        # Check if this represents progress
        if abs(new_confidence - old_confidence) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = turn
            hypothesis.iterations_without_progress = 0
        else:
            hypothesis.iterations_without_progress += 1

        self.logger.info(
            f"Updated hypothesis confidence: {old_confidence:.2f} → {new_confidence:.2f}",
            extra={
                "hypothesis_id": hypothesis.hypothesis_id,
                "supporting": supporting_count,
                "refuting": refuting_count,
                "evidence_ratio": calculate_evidence_ratio(hypothesis),
            },
        )

        # Check if hypothesis should transition status
        self._check_status_transition(hypothesis, turn)

    def update_hypothesis_confidence(
        self,
        hypothesis: Hypothesis,
        new_likelihood: float,
        current_turn: int,
        reason: str,
    ) -> Hypothesis:
        """Update hypothesis confidence manually (for test results)

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

        # Check status transition
        self._check_status_transition(hypothesis, current_turn)

        return hypothesis

    def _check_status_transition(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Check if hypothesis should transition to VALIDATED or REFUTED

        Transition criteria:
        - VALIDATED: confidence ≥ 0.70 and at least 2 supporting evidence
        - REFUTED: confidence ≤ 0.20 and at least 2 refuting evidence

        Args:
            hypothesis: Hypothesis to check
            turn: Current turn number
        """
        if hypothesis.status != HypothesisStatus.ACTIVE:
            # Only active hypotheses can be auto-transitioned
            return

        supporting_count = len(hypothesis.supporting_evidence)
        refuting_count = len(hypothesis.refuting_evidence)

        # Check for validation
        if hypothesis.likelihood >= 0.70 and supporting_count >= 2:
            hypothesis.status = HypothesisStatus.VALIDATED
            self.logger.info(
                f"Hypothesis VALIDATED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "confidence": hypothesis.likelihood,
                    "supporting_evidence": supporting_count,
                },
            )

        # Check for refutation
        elif hypothesis.likelihood <= 0.20 and refuting_count >= 2:
            hypothesis.status = HypothesisStatus.REFUTED
            hypothesis.retirement_reason = "Refuted by evidence"
            self.logger.info(
                f"Hypothesis REFUTED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "confidence": hypothesis.likelihood,
                    "refuting_evidence": refuting_count,
                },
            )

        # Check for retirement due to low confidence
        elif hypothesis.likelihood < 0.3 and hypothesis.status != HypothesisStatus.RETIRED:
            hypothesis.status = HypothesisStatus.RETIRED
            hypothesis.retirement_reason = "Low confidence after testing"
            self.logger.info(f"Hypothesis {hypothesis.hypothesis_id} RETIRED (confidence < 30%)")

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
                f"Anchoring: {len(stalled_hypotheses)} hypotheses without progress for 3+ iterations",
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
                and h.status == HypothesisStatus.ACTIVE
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
        2. Active status (ready for testing)
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
            if h.status == HypothesisStatus.ACTIVE
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
            Summary dictionary with status counts, generation modes, and confidence stats
        """
        summary = {
            "total": len(hypotheses),
            "captured": sum(1 for h in hypotheses if h.status == HypothesisStatus.CAPTURED),
            "active": sum(1 for h in hypotheses if h.status == HypothesisStatus.ACTIVE),
            "validated": sum(1 for h in hypotheses if h.status == HypothesisStatus.VALIDATED),
            "refuted": sum(1 for h in hypotheses if h.status == HypothesisStatus.REFUTED),
            "retired": sum(1 for h in hypotheses if h.status == HypothesisStatus.RETIRED),
            "superseded": sum(1 for h in hypotheses if h.status == HypothesisStatus.SUPERSEDED),
        }

        # Count by generation mode
        summary["opportunistic"] = sum(
            1 for h in hypotheses
            if h.generation_mode == HypothesisGenerationMode.OPPORTUNISTIC
        )
        summary["systematic"] = sum(
            1 for h in hypotheses
            if h.generation_mode == HypothesisGenerationMode.SYSTEMATIC
        )
        summary["forced_alternative"] = sum(
            1 for h in hypotheses
            if h.generation_mode == HypothesisGenerationMode.FORCED_ALTERNATIVE
        )

        # Active hypotheses statistics
        active_hypotheses = [h for h in hypotheses if h.status == HypothesisStatus.ACTIVE]

        summary["max_confidence"] = max((h.likelihood for h in active_hypotheses), default=0.0)
        summary["avg_confidence"] = (
            sum(h.likelihood for h in active_hypotheses) / len(active_hypotheses)
            if active_hypotheses
            else 0.0
        )
        summary["categories"] = list(set(h.category for h in active_hypotheses))

        return summary

    def get_best_hypothesis(
        self,
        hypotheses: List[Hypothesis],
    ) -> Optional[Hypothesis]:
        """Get the hypothesis with highest confidence

        Args:
            hypotheses: All hypotheses

        Returns:
            Hypothesis with highest likelihood, or None if no active hypotheses
        """
        active_hypotheses = [
            h for h in hypotheses
            if h.status == HypothesisStatus.ACTIVE
        ]

        if not active_hypotheses:
            return None

        return max(active_hypotheses, key=lambda h: h.likelihood)

    def get_hypotheses_by_category(
        self,
        hypotheses: List[Hypothesis],
        category: str,
    ) -> List[Hypothesis]:
        """Get all hypotheses for a specific category

        Args:
            hypotheses: All hypotheses
            category: Category name

        Returns:
            List of hypotheses in category
        """
        return [h for h in hypotheses if h.category == category]

    def has_validated_hypothesis(
        self,
        hypotheses: List[Hypothesis],
    ) -> bool:
        """Check if investigation has at least one validated hypothesis

        Args:
            hypotheses: All hypotheses

        Returns:
            True if at least one hypothesis is validated
        """
        return any(
            h.status == HypothesisStatus.VALIDATED
            for h in hypotheses
        )


def create_hypothesis_manager() -> HypothesisManager:
    """Factory function for HypothesisManager

    Returns:
        New HypothesisManager instance
    """
    return HypothesisManager()


def rank_hypotheses_by_likelihood(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """Sort hypotheses by confidence (descending)

    Args:
        hypotheses: List of hypotheses to sort

    Returns:
        Sorted list with highest confidence first
    """
    return sorted(hypotheses, key=lambda h: h.likelihood, reverse=True)
