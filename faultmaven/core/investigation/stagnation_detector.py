"""Stagnation Detector for Investigation Engine

Detects investigation stalls and anchoring patterns to prevent getting stuck.

Stagnation Patterns Detected:
1. NO_PROGRESS - No milestones completed in N consecutive turns
2. HYPOTHESIS_ANCHORING - Same hypothesis category tested repeatedly without success
3. ACTION_LOOP - Same actions repeated without progress
4. HYPOTHESIS_DEADLOCK - All hypotheses inconclusive

Design Reference:
- docs/architecture/investigation-engine/error-handling-and-recovery.md Section 5

Usage:
    detector = StagnationDetector()
    stagnation_type = detector.detect_stagnation(case)
    if stagnation_type:
        breaker = StagnationBreaker()
        action = breaker.break_stagnation(case, stagnation_type)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Set

from faultmaven.modules.case.contracts import (
    Case,
    DegradedMode,
    DegradedModeType,
    HypothesisCategory,
    HypothesisStatus,
)

logger = logging.getLogger(__name__)


class StagnationType(str, Enum):
    """Types of investigation stagnation."""

    NO_PROGRESS = "no_progress"
    HYPOTHESIS_ANCHORING = "hypothesis_anchoring"
    ACTION_LOOP = "action_loop"
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"


@dataclass
class BreakoutAction:
    """Action to break out of stagnation."""

    action: str
    message: str
    prompt_injection: Optional[str] = None


class StagnationDetector:
    """
    Detects investigation stalls and anchoring patterns.

    Uses turn-based tracking (not phase-based) to identify when
    investigation is stuck and needs intervention.
    """

    def __init__(
        self,
        no_progress_threshold: int = 3,
        category_anchoring_threshold: int = 4,
        action_loop_threshold: int = 5,
    ):
        """
        Initialize stagnation detector with thresholds.

        Args:
            no_progress_threshold: Turns without progress before triggering (default 3)
            category_anchoring_threshold: Failed hypotheses in same category (default 4)
            action_loop_threshold: Turns with same actions repeated (default 5)
        """
        self.no_progress_threshold = no_progress_threshold
        self.category_anchoring_threshold = category_anchoring_threshold
        self.action_loop_threshold = action_loop_threshold

    def detect_stagnation(self, case: Case) -> Optional[StagnationType]:
        """
        Detect if investigation is stagnating.

        Checks patterns in order of severity:
        1. No progress (most common)
        2. Hypothesis anchoring (category fixation)
        3. Action loop (repetitive behavior)
        4. Hypothesis deadlock (all inconclusive)

        Args:
            case: Current investigation case

        Returns:
            StagnationType if stagnating, None otherwise
        """
        # Pattern 1: No milestones completed in N turns
        if case.turns_without_progress >= self.no_progress_threshold:
            logger.warning(
                f"Stagnation detected: no progress for {case.turns_without_progress} turns"
            )
            return StagnationType.NO_PROGRESS

        # Pattern 2: Hypothesis category anchoring
        if self._detect_category_anchoring(case):
            return StagnationType.HYPOTHESIS_ANCHORING

        # Pattern 3: Repeated action sequences
        if self._detect_action_loop(case):
            return StagnationType.ACTION_LOOP

        # Pattern 4: All hypotheses inconclusive
        if self._detect_hypothesis_deadlock(case):
            return StagnationType.HYPOTHESIS_DEADLOCK

        return None

    def _detect_category_anchoring(self, case: Case) -> bool:
        """
        Detect if agent is stuck testing same hypothesis category.

        Triggers if 4+ hypotheses in same category are REFUTED or INCONCLUSIVE.
        This indicates the agent is fixated on one explanation type.

        Args:
            case: Current investigation case

        Returns:
            True if category anchoring detected
        """
        category_counts: dict[str, int] = {}

        for hypothesis in case.hypotheses.values():
            if hypothesis.status in (
                HypothesisStatus.REFUTED,
                HypothesisStatus.INCONCLUSIVE,
            ):
                cat = (
                    hypothesis.category.value
                    if hasattr(hypothesis.category, "value")
                    else str(hypothesis.category)
                )
                category_counts[cat] = category_counts.get(cat, 0) + 1

        for category, count in category_counts.items():
            if count >= self.category_anchoring_threshold:
                logger.warning(
                    f"Category anchoring detected: {count} failed hypotheses in '{category}'"
                )
                return True

        return False

    def _detect_action_loop(self, case: Case) -> bool:
        """
        Detect if agent is repeating same actions.

        Triggers if same action sequence appears in 5+ consecutive turns.
        This indicates the agent is stuck in a behavioral loop.

        Args:
            case: Current investigation case

        Returns:
            True if action loop detected
        """
        if len(case.turn_history) < self.action_loop_threshold:
            return False

        recent_turns = case.turn_history[-self.action_loop_threshold :]
        action_sequences = [
            tuple(t.actions_taken) if t.actions_taken else () for t in recent_turns
        ]

        # Filter out empty sequences
        action_sequences = [seq for seq in action_sequences if seq]

        if len(action_sequences) >= 3:
            unique_sequences = set(action_sequences)
            if len(unique_sequences) == 1:
                logger.warning(
                    f"Action loop detected: same actions {action_sequences[0]} repeated in recent turns"
                )
                return True

        return False

    def _detect_hypothesis_deadlock(self, case: Case) -> bool:
        """
        Detect if all hypotheses are inconclusive.

        Triggers when 3+ hypotheses exist and all are INCONCLUSIVE.
        This indicates no hypothesis can be validated or refuted.

        Args:
            case: Current investigation case

        Returns:
            True if hypothesis deadlock detected
        """
        if not case.hypotheses:
            return False

        if len(case.hypotheses) < 3:
            return False

        all_inconclusive = all(
            h.status == HypothesisStatus.INCONCLUSIVE for h in case.hypotheses.values()
        )

        if all_inconclusive:
            logger.warning(
                f"Hypothesis deadlock detected: all {len(case.hypotheses)} hypotheses inconclusive"
            )
            return True

        return False

    def get_stagnation_summary(self, case: Case) -> dict:
        """
        Get summary of stagnation indicators.

        Args:
            case: Current investigation case

        Returns:
            Dictionary with stagnation metrics
        """
        # Count failed hypotheses by category
        category_failures: dict[str, int] = {}
        for hypothesis in case.hypotheses.values():
            if hypothesis.status in (
                HypothesisStatus.REFUTED,
                HypothesisStatus.INCONCLUSIVE,
            ):
                cat = (
                    hypothesis.category.value
                    if hasattr(hypothesis.category, "value")
                    else str(hypothesis.category)
                )
                category_failures[cat] = category_failures.get(cat, 0) + 1

        # Find max category failure count
        max_category_failures = (
            max(category_failures.values()) if category_failures else 0
        )

        # Count inconclusive hypotheses
        inconclusive_count = sum(
            1
            for h in case.hypotheses.values()
            if h.status == HypothesisStatus.INCONCLUSIVE
        )

        return {
            "turns_without_progress": case.turns_without_progress,
            "category_failure_counts": category_failures,
            "max_category_failures": max_category_failures,
            "inconclusive_hypotheses": inconclusive_count,
            "total_hypotheses": len(case.hypotheses),
            "is_stagnating": self.detect_stagnation(case) is not None,
        }


class StagnationBreaker:
    """
    Strategies to break out of stagnation.

    Provides recovery actions based on the type of stagnation detected.
    """

    # All hypothesis categories for suggesting alternatives
    ALL_CATEGORIES: List[str] = [
        "code",
        "config",
        "environment",
        "network",
        "data",
        "hardware",
        "external",
        "human",
    ]

    def break_stagnation(
        self, case: Case, stagnation_type: StagnationType
    ) -> BreakoutAction:
        """
        Determine action to break out of stagnation.

        Args:
            case: Current investigation case
            stagnation_type: Type of stagnation detected

        Returns:
            BreakoutAction with recommended action and prompt injection
        """
        if stagnation_type == StagnationType.NO_PROGRESS:
            return self._handle_no_progress(case)

        elif stagnation_type == StagnationType.HYPOTHESIS_ANCHORING:
            return self._handle_anchoring(case)

        elif stagnation_type == StagnationType.ACTION_LOOP:
            return self._handle_action_loop(case)

        elif stagnation_type == StagnationType.HYPOTHESIS_DEADLOCK:
            return self._handle_deadlock(case)

        return BreakoutAction(action="none", message="No action needed")

    def _handle_no_progress(self, case: Case) -> BreakoutAction:
        """
        Handle no progress in 3+ turns.

        Enters degraded mode and suggests user intervention.
        """
        # Update case with degraded mode
        if case.degraded_mode is None:
            case.degraded_mode = DegradedMode(
                mode_type=DegradedModeType.NO_PROGRESS,
                reason=f"No progress in {case.turns_without_progress} turns",
                entered_at=datetime.now(timezone.utc),
                attempted_actions=[
                    f"Processed {case.current_turn} turns",
                    f"Collected {len(case.evidence)} evidence items",
                    f"Generated {len(case.hypotheses)} hypotheses",
                ],
            )

        return BreakoutAction(
            action="enter_degraded_mode",
            message="Investigation not progressing. Offering alternative approaches.",
            prompt_injection="The investigation has not made progress in several turns. "
            "Ask the user for clarification or additional information. "
            "Consider offering to escalate or try a different approach.",
        )

    def _handle_anchoring(self, case: Case) -> BreakoutAction:
        """
        Handle hypothesis category anchoring.

        Forces exploration of different hypothesis categories.
        """
        anchored_category = self._find_anchored_category(case)
        alternatives = self._suggest_categories(anchored_category)

        return BreakoutAction(
            action="force_alternative_category",
            message=f"Tested many '{anchored_category}' hypotheses. Exploring other categories.",
            prompt_injection=f"IMPORTANT: Do NOT propose hypotheses in '{anchored_category}' category. "
            f"This category has been explored extensively without success. "
            f"Try different categories like: {alternatives}",
        )

    def _handle_action_loop(self, case: Case) -> BreakoutAction:
        """
        Handle repeated action sequences.

        Requests user input to break the loop.
        """
        return BreakoutAction(
            action="request_user_input",
            message="Investigation appears stuck in a loop. Requesting user guidance.",
            prompt_injection="The investigation is repeating the same actions without progress. "
            "Ask the user for additional context or suggest a completely different approach. "
            "Consider whether the problem statement needs refinement.",
        )

    def _handle_deadlock(self, case: Case) -> BreakoutAction:
        """
        Handle all hypotheses inconclusive.

        Retires all inconclusive hypotheses and starts fresh.
        """
        # Retire all inconclusive hypotheses
        retired_count = 0
        for hypothesis in case.hypotheses.values():
            if hypothesis.status == HypothesisStatus.INCONCLUSIVE:
                hypothesis.status = HypothesisStatus.RETIRED
                retired_count += 1

        return BreakoutAction(
            action="reset_hypotheses",
            message=f"All hypotheses inconclusive. Retired {retired_count} hypotheses for fresh start.",
            prompt_injection="All previous hypotheses were inconclusive and have been retired. "
            "Generate completely new hypotheses based on available evidence. "
            "Consider exploring different root cause categories or perspectives.",
        )

    def _find_anchored_category(self, case: Case) -> str:
        """Find the category with most failed hypotheses."""
        category_counts: dict[str, int] = {}

        for hypothesis in case.hypotheses.values():
            if hypothesis.status in (
                HypothesisStatus.REFUTED,
                HypothesisStatus.INCONCLUSIVE,
            ):
                cat = (
                    hypothesis.category.value
                    if hasattr(hypothesis.category, "value")
                    else str(hypothesis.category)
                )
                category_counts[cat] = category_counts.get(cat, 0) + 1

        if not category_counts:
            return "unknown"

        return max(category_counts, key=category_counts.get)

    def _suggest_categories(self, exclude_category: str) -> str:
        """Suggest alternative categories to explore."""
        alternatives = [
            cat
            for cat in self.ALL_CATEGORIES
            if cat.lower() != exclude_category.lower()
        ]
        return ", ".join(alternatives[:4])  # Top 4 alternatives
