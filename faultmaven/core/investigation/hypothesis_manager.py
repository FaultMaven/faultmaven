"""Unified Hypothesis Manager - Complete Hypothesis Lifecycle Management

Consolidates all hypothesis management logic into ONE coherent system.

Merges:
- faultmaven/core/investigation/hypothesis_manager.py (OLD - HypothesisManager class)
- faultmaven/services/agentic/hypothesis/hypothesis_manager.py (NEW - evidence linking)

Design Reference:
- docs/architecture/investigation-engine/evidence-driven-investigation-framework.md
- docs/architecture/investigation-engine/investigation-data-models.md (§3 Hypothesis Lifecycle)

Hypothesis Lifecycle (matches HypothesisStatus enum):
- CAPTURED: Opportunistic hypothesis (not yet promoted to active testing)
- ACTIVE: Currently being tested (promoted from CAPTURED or systematic generation)
- VALIDATED: Confirmed by evidence (likelihood ≥0.70 + ≥2 supporting evidence)
- REFUTED: Disproved by evidence (likelihood ≤0.20 + ≥2 refuting evidence)
- INCONCLUSIVE: System-automated — likelihood 0.3–0.5 stagnant for 3+ turns
- RETIRED: System-automated (likelihood <0.30) or manual abandonment without disproof

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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.modules.case.contracts import (
    EvidenceStance,
    Hypothesis,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisStatus,
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
        pass

    @staticmethod
    def calculate_evidence_ratio(hypothesis: Hypothesis) -> float:
        """Compute supporting/(supporting+refuting) evidence ratio.

        Returns 0.0 when there is no linked evidence.
        """
        supporting = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )
        total = supporting + refuting
        return (supporting / total) if total > 0 else 0.0

    def create_hypothesis(
        self,
        statement: str,
        category: str,
        initial_likelihood: float,
        current_turn: int,
        generation_mode: HypothesisGenerationMode = HypothesisGenerationMode.SYSTEMATIC,
        status: HypothesisStatus = HypothesisStatus.ACTIVE,
        rationale: Optional[str] = None,
    ) -> Hypothesis:
        """Create new hypothesis"""
        hypothesis = Hypothesis(
            statement=statement,
            category=category,
            likelihood=initial_likelihood,
            initial_likelihood=initial_likelihood,
            status=status,
            generation_mode=generation_mode,
            generated_at_turn=current_turn,
            last_updated_turn=current_turn,
            last_progress_at_turn=current_turn,
            evidence_links=[],
            rationale=rationale or "Initial hypothesis",
        )

        logger.info(
            f"Created hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (category={category}, likelihood={initial_likelihood}, turn={current_turn})"
        )

        return hypothesis

    def create_validated_hypothesis(
        self,
        statement: str,
        category: str,
        likelihood: float,
        supporting_evidence_ids: List[str],
        current_turn: int,
    ) -> Hypothesis:
        """Create hypothesis already in VALIDATED state (Single-Shot Validation)."""
        if likelihood < 0.7:
            raise ValueError("Validated hypothesis requires likelihood >= 0.7")
        if len(supporting_evidence_ids) < 2:
            raise ValueError(
                "Validated hypothesis requires at least 2 supporting evidence items"
            )

        hypothesis = self.create_hypothesis(
            statement=statement,
            category=category,
            initial_likelihood=likelihood,
            current_turn=current_turn,
            status=HypothesisStatus.VALIDATED,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        )

        # Manually link evidence
        for ev_id in supporting_evidence_ids:
            self.link_evidence(hypothesis, ev_id, True, current_turn)

        logger.info(
            f"Created VALIDATED hypothesis {hypothesis.hypothesis_id}: "
            f"{statement[:50]}... (likelihood={likelihood:.2f})"
        )

        return hypothesis

    def link_evidence(
        self,
        hypothesis: Hypothesis,
        evidence_id: str,
        supports: bool,
        turn: int,
        reasoning: str = "Linked by agent",
        stance_confidence: float = 1.0,
        stance_override: Optional[EvidenceStance] = None,
    ) -> None:
        """Link evidence to hypothesis.

        Args:
            hypothesis: Hypothesis to link evidence to
            evidence_id: ID of evidence to link
            supports: True for SUPPORTS, False for REFUTES (ignored if stance_override set)
            turn: Current turn number
            reasoning: Explanation of why evidence is linked
            stance_confidence: Confidence in the stance (0.0-1.0)
            stance_override: Optional explicit stance (SUPPORTS, REFUTES, or NEUTRAL).
                           When set, the `supports` parameter is ignored.
        """
        if stance_override is not None:
            stance = stance_override
        else:
            stance = EvidenceStance.SUPPORTS if supports else EvidenceStance.REFUTES

        link = HypothesisEvidenceLink(
            hypothesis_id=hypothesis.hypothesis_id,
            evidence_id=evidence_id,
            stance=stance,
            reasoning=reasoning,
            stance_confidence=stance_confidence,
        )
        # evidence_links is now List[HypothesisEvidenceLink] (junction-table
        # backed). Upsert by evidence_id: replace an existing link if one
        # already exists for this evidence_id, else append.
        existing_idx = next(
            (
                i
                for i, existing in enumerate(hypothesis.evidence_links)
                if existing.evidence_id == evidence_id
            ),
            None,
        )
        if existing_idx is not None:
            hypothesis.evidence_links[existing_idx] = link
        else:
            hypothesis.evidence_links.append(link)

            stance_label = stance.value
            logger.info(
                f"Linked {stance_label} evidence to hypothesis: {evidence_id}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis": hypothesis.statement[:50],
                },
            )

        # Update likelihood after linking evidence
        # NEUTRAL links are stored for audit trail but do not affect confidence
        self.update_likelihood_from_evidence(hypothesis, turn)

    def update_likelihood_from_evidence(
        self,
        hypothesis: Hypothesis,
        turn: int,
    ) -> None:
        """Update hypothesis likelihood based on evidence accumulation

        Likelihood formula:
        - Start with initial_likelihood
        - Add 0.15 per supporting evidence
        - Subtract 0.20 per refuting evidence
        - Clamp to [0.0, 1.0]

        Args:
            hypothesis: Hypothesis to update
            turn: Current turn number
        """
        old_likelihood = hypothesis.likelihood

        supporting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )

        # Baseline is initial_likelihood. Accumulate deltas.
        new_likelihood = (
            hypothesis.initial_likelihood
            + (0.15 * supporting_count)
            - (0.20 * refuting_count)
        )
        new_likelihood = max(0.0, min(1.0, new_likelihood))

        hypothesis.likelihood = new_likelihood
        hypothesis.last_updated_turn = turn

        # Check if this represents progress
        if abs(new_likelihood - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = turn
            hypothesis.iterations_without_progress = 0
        else:
            hypothesis.iterations_without_progress += 1

        logger.info(
            f"Updated hypothesis likelihood: {old_likelihood:.2f} -> {new_likelihood:.2f}",
            extra={
                "hypothesis_id": hypothesis.hypothesis_id,
                "evidence_ratio": self.calculate_evidence_ratio(hypothesis),
            },
        )

        # Check if hypothesis should transition status
        self._check_status_transition(hypothesis, turn)

    def update_hypothesis_likelihood(
        self,
        hypothesis: Hypothesis,
        new_likelihood: float,
        current_turn: int,
        reason: str,
    ) -> Hypothesis:
        """Update hypothesis likelihood manually (for test results)

        Args:
            hypothesis: Hypothesis to update
            new_likelihood: New likelihood level (0.0 to 1.0)
            current_turn: Current conversation turn
            reason: Reason for likelihood change

        Returns:
            Updated hypothesis
        """
        old_likelihood = hypothesis.likelihood
        hypothesis.likelihood = max(0.0, min(1.0, new_likelihood))  # Clamp to [0, 1]
        hypothesis.last_updated_turn = current_turn
        # Check if this represents progress (consistent with update_likelihood_from_evidence)
        if abs(new_likelihood - old_likelihood) >= 0.05:  # 5% threshold
            hypothesis.last_progress_at_turn = current_turn
            hypothesis.iterations_without_progress = 0
            logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} likelihood updated: "
                f"{old_likelihood:.2f} → {new_likelihood:.2f} ({reason})"
            )
        else:
            hypothesis.iterations_without_progress += 1
            logger.debug(
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
        """Check if hypothesis should transition status.

        Transition criteria:
        - VALIDATED: likelihood >= 0.70 and at least 2 supporting evidence
        - REFUTED: likelihood <= 0.20 and at least 2 refuting evidence
        - INCONCLUSIVE: likelihood between 0.3-0.5 for 3+ turns with no evidence progress

        Args:
            hypothesis: Hypothesis to check
            turn: Current turn number
        """
        if hypothesis.status != HypothesisStatus.ACTIVE:
            # Only active hypotheses can be auto-transitioned
            return

        supporting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        )
        refuting_count = sum(
            1
            for link in hypothesis.evidence_links
            if link.stance == EvidenceStance.REFUTES
        )

        # Check for validation
        if hypothesis.likelihood >= 0.70 and supporting_count >= 2:
            hypothesis.status = HypothesisStatus.VALIDATED
            logger.info(
                f"Hypothesis VALIDATED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                    "supporting_evidence": supporting_count,
                },
            )

        # Check for refutation
        elif hypothesis.likelihood <= 0.20 and refuting_count >= 2:
            hypothesis.refutation_reason = (
                f"likelihood {hypothesis.likelihood:.2f} with "
                f"{refuting_count} refuting evidence items"
            )[:200]
            hypothesis.status = HypothesisStatus.REFUTED
            logger.info(
                f"Hypothesis REFUTED: {hypothesis.statement}",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                    "refuting_evidence": refuting_count,
                },
            )

        # Check for inconclusive: stagnant in gray zone (0.3-0.5) for 3+ turns
        elif (
            0.3 <= hypothesis.likelihood <= 0.5
            and hypothesis.iterations_without_progress >= 3
        ):
            hypothesis.status = HypothesisStatus.INCONCLUSIVE
            logger.info(
                f"Hypothesis INCONCLUSIVE: {hypothesis.statement} "
                f"(likelihood={hypothesis.likelihood:.2f}, stagnant for "
                f"{hypothesis.iterations_without_progress} turns)",
                extra={
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "likelihood": hypothesis.likelihood,
                },
            )

        # Check for retirement due to low confidence
        elif (
            hypothesis.likelihood < 0.3
            and hypothesis.status != HypothesisStatus.RETIRED
        ):
            hypothesis.status = HypothesisStatus.RETIRED
            hypothesis.rationale = "Low confidence after testing"  # Mapped retirement_reason to rationale or logging
            logger.info(
                f"Hypothesis {hypothesis.hypothesis_id} RETIRED (likelihood < 30%)"
            )

    def apply_likelihood_decay(
        self,
        hypothesis: Hypothesis,
        current_turn: int,
    ) -> Hypothesis:
        """Apply likelihood decay to stagnant hypothesis

        Likelihood decay formula: base * 0.85^iterations_without_progress
        """
        if (
            hypothesis.status == HypothesisStatus.ACTIVE
            and hypothesis.iterations_without_progress > 0
        ):
            old_likelihood = hypothesis.likelihood
            # Apply decay factor
            decay_factor = 0.85**hypothesis.iterations_without_progress
            hypothesis.likelihood = max(0.1, hypothesis.likelihood * decay_factor)

            logger.info(
                f"Applied likelihood decay to hypothesis {hypothesis.hypothesis_id}: "
                f"{old_likelihood:.2f} -> {hypothesis.likelihood:.2f} "
                f"(stagnant for {hypothesis.iterations_without_progress} turns)"
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
        # Set refutation_reason BEFORE status so the pair invariant is
        # satisfied if this object is ever re-validated.
        hypothesis.refutation_reason = (reason or "refuted by evidence")[:200]
        hypothesis.status = HypothesisStatus.REFUTED
        hypothesis.likelihood = 0.0
        # hypothesis.refuting_evidence.extend(refuting_evidence_ids) # Updated logic via link_evidence manually or loop
        existing_evidence_ids = {link.evidence_id for link in hypothesis.evidence_links}
        for ev_id in refuting_evidence_ids:
            if ev_id not in existing_evidence_ids:
                hypothesis.evidence_links.append(
                    HypothesisEvidenceLink(
                        hypothesis_id=hypothesis.hypothesis_id,
                        evidence_id=ev_id,
                        stance=EvidenceStance.REFUTES,
                        reasoning=reason,
                        stance_confidence=1.0,
                    )
                )
        hypothesis.last_updated_turn = current_turn

        logger.info(
            f"Hypothesis {hypothesis.hypothesis_id} REFUTED: {reason} "
            f"(evidence: {refuting_evidence_ids})"
        )

        return hypothesis

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

        logger.warning(
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
            h for h in hypotheses if h.status == HypothesisStatus.ACTIVE
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
        return any(h.status == HypothesisStatus.VALIDATED for h in hypotheses)


def create_hypothesis_manager() -> HypothesisManager:
    """Factory function for HypothesisManager

    Returns:
        New HypothesisManager instance
    """
    return HypothesisManager()


def rank_hypotheses_by_likelihood(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """Sort hypotheses by likelihood (descending)

    Args:
        hypotheses: List of hypotheses to sort

    Returns:
        Sorted list with highest likelihood first
    """
    return sorted(hypotheses, key=lambda h: h.likelihood, reverse=True)
