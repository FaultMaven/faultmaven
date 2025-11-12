"""Investigation Coordinator for Multi-System Conflict Resolution

This module coordinates multiple intervention systems to prevent conflicts
when multiple systems want to intervene simultaneously:
- Anchoring prevention
- Phase completion
- Confidence decay
- Progress tracking (v3.0)

Implements priority order when multiple triggers fire (Refinement #4).

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md (Anchoring Prevention)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from faultmaven.models.case import UrgencyLevel
    from faultmaven.models.investigation import InvestigationState, InvestigationStrategy


class InterventionType(str, Enum):
    """Types of interventions"""
    ANCHORING_PREVENTION = "anchoring_prevention"
    PHASE_COMPLETION = "phase_completion"
    CONFIDENCE_DECAY = "confidence_decay"


@dataclass
class InterventionPlan:
    """Coordinated intervention plan"""
    intervention_type: InterventionType
    force_alternatives: bool = False
    advance_phase: bool = False
    reason: str = ""
    priority: int = 0  # Higher = more urgent


class InvestigationCoordinator:
    """Coordinates multiple intervention systems (Refinement #4, v3.0)

    Priority order when multiple systems trigger:
    1. Phase completion
    2. Anchoring prevention
    3. Progress metrics (via working conclusion - v3.0)

    Note: Stall detection removed in v3.0 - replaced with continuous progress tracking
    via ProgressMetrics.should_suggest_closure()
    """

    def __init__(self):
        """Initialize coordinator"""
        pass

    def check_interventions(
        self,
        state: "InvestigationState"
    ) -> Optional[InterventionPlan]:
        """Check all intervention systems and coordinate response (v3.0)

        Priority order:
        1. Phase completion → Normal advancement
        2. Anchoring prevention → Force alternatives
        3. Progress metrics checked separately via working_conclusion

        Args:
            state: Current investigation state

        Returns:
            Coordinated intervention plan, or None if no intervention needed
        """
        # Check all systems
        anchoring = self._detect_anchoring(state)
        phase_complete = self._check_phase_completion(state)

        # Priority 1: Normal phase completion
        if phase_complete:
            return InterventionPlan(
                intervention_type=InterventionType.PHASE_COMPLETION,
                advance_phase=True,
                reason="Phase objectives completed",
                priority=70
            )

        # Priority 2: Anchoring → force alternatives
        if anchoring:
            return InterventionPlan(
                intervention_type=InterventionType.ANCHORING_PREVENTION,
                force_alternatives=True,
                reason=f"Anchoring detected: {anchoring}",
                priority=60
            )

        return None  # No intervention needed

    def _detect_anchoring(self, state: "InvestigationState") -> Optional[str]:
        """Detect anchoring bias in hypothesis testing

        This integrates with existing anchoring prevention system.

        Args:
            state: Current investigation state

        Returns:
            Anchoring warning message if detected, None otherwise
        """
        hypotheses = state.ooda_engine.hypotheses
        iterations = state.ooda_engine.iterations

        if not hypotheses or not iterations:
            return None

        # Pattern 1: Same category tested 4+ times
        category_counts = {}
        for hyp in hypotheses:
            if hyp.status in ["testing", "validated"]:
                category_counts[hyp.category] = category_counts.get(hyp.category, 0) + 1

        if category_counts:
            max_category_count = max(category_counts.values())
            if max_category_count >= 4:
                dominant = max(category_counts, key=category_counts.get)
                return f"Tested '{dominant}' hypotheses 4 times without resolution"

        # Pattern 2: No progress in last 3 iterations
        recent = iterations[-3:] if len(iterations) >= 3 else iterations
        if recent and all(not iter.made_progress for iter in recent):
            return "No progress in last 3 OODA iterations"

        # Pattern 3: High-confidence hypothesis failing repeatedly
        for hyp in hypotheses:
            if hyp.likelihood > 0.8 and hyp.iterations_without_progress >= 3:
                return f"High-confidence hypothesis failing repeatedly"

        return None

    def _check_phase_completion(self, state: "InvestigationState") -> bool:
        """Check if current phase objectives are complete

        Simplified check - actual implementation would be in PhaseOrchestrator.

        Args:
            state: Current investigation state

        Returns:
            True if phase complete, False otherwise
        """
        from faultmaven.models.investigation import InvestigationPhase

        current_phase = state.lifecycle.current_phase

        # Simple heuristic - phase complete if:
        # 1. AnomalyFrame created (Phase 1)
        # 2. Root cause found with high confidence (Phase 4)
        # 3. Explicit phase_complete flag set

        if current_phase == InvestigationPhase.BLAST_RADIUS:
            # Phase 1 complete if AnomalyFrame created
            return state.ooda_engine.anomaly_frame is not None

        elif current_phase == InvestigationPhase.VALIDATION:
            # Phase 4 complete if high-confidence root cause found
            if state.ooda_engine.hypotheses:
                max_confidence = max([h.likelihood for h in state.ooda_engine.hypotheses])
                return max_confidence >= 0.7

        # For other phases, rely on explicit completion signal
        # (would be set by phase handler)
        return False

    def _get_urgency_level(self, state: "InvestigationState") -> "UrgencyLevel":
        """Get urgency level from state

        Args:
            state: Current investigation state

        Returns:
            UrgencyLevel (NORMAL if not set)
        """
        from faultmaven.models.case import UrgencyLevel

        # InvestigationState.lifecycle.urgency_level is a string
        # Convert to UrgencyLevel enum
        urgency_str = state.lifecycle.urgency_level.lower()

        if urgency_str == "critical":
            return UrgencyLevel.CRITICAL
        elif urgency_str == "high":
            return UrgencyLevel.HIGH
        else:
            return UrgencyLevel.NORMAL
