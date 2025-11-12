"""Investigation Strategy Selector - Active Incident vs Post-Mortem

This module determines the appropriate investigation strategy based on context,
urgency, and user preferences. The strategy influences investigation depth,
thoroughness requirements, and phase progression.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Strategy Comparison:
┌─────────────────────┬────────────────────┬─────────────────────┐
│ Characteristic      │ Active Incident    │ Post-Mortem         │
├─────────────────────┼────────────────────┼─────────────────────┤
│ Primary Goal        │ Mitigation speed   │ Complete RCA        │
│ Hypothesis Testing  │ Satisficing        │ Exhaustive          │
│ Confidence Required │ 60-70%             │ ≥85%                │
│ Phase Skipping      │ Allowed            │ Discouraged         │
│ Evidence Depth      │ Sufficient         │ Comprehensive       │
│ Validation Rigor    │ Practical          │ Strict              │
│ Documentation       │ Optional           │ Required            │
└─────────────────────┴────────────────────┴─────────────────────┘

Transition Scenarios:
- Active → Post-Mortem: After mitigation, for thorough RCA
- Post-Mortem → Active: If new incident discovered during analysis
"""

import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta

from faultmaven.models.investigation import (
    InvestigationStrategy,
    InvestigationPhase,
    ProblemConfirmation,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Strategy Configuration
# =============================================================================


class StrategyConfig:
    """Configuration for each investigation strategy"""

    ACTIVE_INCIDENT = {
        "name": "Active Incident",
        "description": "Speed-optimized investigation for live issues",
        "primary_goal": "Rapid mitigation and service restoration",

        # Confidence thresholds
        "min_hypothesis_confidence": 0.60,  # Lower bar for mitigation
        "solution_confidence_threshold": 0.70,  # Proceed with 70% confidence

        # Phase behavior
        "allow_phase_skipping": True,
        "urgency_skip_threshold": "high",  # Skip phases at high/critical urgency
        "min_phases_required": 3,  # Can skip to Phase 0→1→4→5 minimum

        # OODA intensity
        "max_iterations_per_phase": {
            InvestigationPhase.BLAST_RADIUS: 2,
            InvestigationPhase.TIMELINE: 2,
            InvestigationPhase.HYPOTHESIS: 2,
            InvestigationPhase.VALIDATION: 4,  # Still need validation
            InvestigationPhase.SOLUTION: 3,
            InvestigationPhase.DOCUMENT: 1,
        },

        # Evidence requirements
        "evidence_sufficiency": "practical",  # Sufficient for decision
        "critical_evidence_required": True,
        "allow_inference": True,  # Can infer from partial evidence

        # Documentation
        "require_documentation": False,
        "offer_post_mortem": True,  # Offer to do thorough RCA after mitigation

        # Time constraints
        "phase_timeout_minutes": 10,  # Move on after 10 min per phase
        "total_timeout_minutes": 60,  # 1 hour max for incident
    }

    POST_MORTEM = {
        "name": "Post-Mortem",
        "description": "Depth-optimized investigation for thorough RCA",
        "primary_goal": "Complete root cause analysis and prevention",

        # Confidence thresholds
        "min_hypothesis_confidence": 0.85,  # High bar for conclusion
        "solution_confidence_threshold": 0.85,  # Must be confident

        # Phase behavior
        "allow_phase_skipping": False,
        "urgency_skip_threshold": None,  # Never skip phases
        "min_phases_required": 7,  # All phases 0-6 required

        # OODA intensity
        "max_iterations_per_phase": {
            InvestigationPhase.BLAST_RADIUS: 3,
            InvestigationPhase.TIMELINE: 3,
            InvestigationPhase.HYPOTHESIS: 4,
            InvestigationPhase.VALIDATION: 8,  # Thorough testing
            InvestigationPhase.SOLUTION: 4,
            InvestigationPhase.DOCUMENT: 2,
        },

        # Evidence requirements
        "evidence_sufficiency": "comprehensive",  # Need thorough evidence
        "critical_evidence_required": True,
        "allow_inference": False,  # Must have concrete evidence

        # Documentation
        "require_documentation": True,
        "offer_post_mortem": False,  # Already doing post-mortem

        # Time constraints
        "phase_timeout_minutes": None,  # No timeout - take time needed
        "total_timeout_minutes": None,  # No overall timeout
    }

    @classmethod
    def get_config(cls, strategy: InvestigationStrategy) -> Dict[str, Any]:
        """Get configuration for strategy

        Args:
            strategy: Investigation strategy

        Returns:
            Configuration dictionary
        """
        if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
            return cls.ACTIVE_INCIDENT
        elif strategy == InvestigationStrategy.POST_MORTEM:
            return cls.POST_MORTEM
        else:
            raise ValueError(f"Unknown investigation strategy: {strategy}")


# =============================================================================
# Strategy Selector
# =============================================================================


class InvestigationStrategySelector:
    """Selects and manages investigation strategy based on context

    Responsibilities:
    - Determine initial strategy from problem context
    - Handle strategy transitions during investigation
    - Provide strategy-specific configuration
    - Detect when strategy change is beneficial
    """

    def __init__(self):
        """Initialize strategy selector"""
        self.logger = logging.getLogger(__name__)

    def select_initial_strategy(
        self,
        problem_confirmation: Optional[ProblemConfirmation],
        urgency_level: str,
        user_preference: Optional[str] = None,
        time_since_incident: Optional[timedelta] = None,
    ) -> Tuple[InvestigationStrategy, str]:
        """Select initial investigation strategy

        Decision logic:
        1. User preference (if explicit) takes precedence
        2. Critical/High urgency → Active Incident
        3. Problem resolved >24h ago → Post-Mortem
        4. Default → Active Incident (most common)

        Args:
            problem_confirmation: Problem confirmation from Phase 0
            urgency_level: low, medium, high, critical
            user_preference: Explicit user strategy choice
            time_since_incident: How long ago problem started

        Returns:
            Tuple of (selected_strategy, selection_reason)
        """
        # User preference overrides all
        if user_preference:
            if user_preference.lower() in ["post_mortem", "postmortem", "rca"]:
                return (
                    InvestigationStrategy.POST_MORTEM,
                    "User explicitly requested thorough post-mortem analysis"
                )
            elif user_preference.lower() in ["active", "incident", "urgent"]:
                return (
                    InvestigationStrategy.ACTIVE_INCIDENT,
                    "User requested rapid incident response"
                )

        # Critical/High urgency → Active Incident
        if urgency_level in ["critical", "high"]:
            self.logger.info(f"Selecting ACTIVE_INCIDENT strategy due to {urgency_level} urgency")
            return (
                InvestigationStrategy.ACTIVE_INCIDENT,
                f"High urgency ({urgency_level}) requires rapid mitigation"
            )

        # Check severity from problem confirmation
        if problem_confirmation and problem_confirmation.severity in ["critical", "high"]:
            return (
                InvestigationStrategy.ACTIVE_INCIDENT,
                f"High severity ({problem_confirmation.severity}) incident requires speed"
            )

        # Historical analysis (>24 hours ago) → Post-Mortem
        if time_since_incident and time_since_incident > timedelta(hours=24):
            self.logger.info("Selecting POST_MORTEM strategy for historical analysis")
            return (
                InvestigationStrategy.POST_MORTEM,
                "Problem occurred >24h ago, performing thorough root cause analysis"
            )

        # Default: Active Incident (most common real-world case)
        return (
            InvestigationStrategy.ACTIVE_INCIDENT,
            "Default strategy for active troubleshooting"
        )

    def should_transition_strategy(
        self,
        current_strategy: InvestigationStrategy,
        investigation_state: Any,
    ) -> Tuple[bool, Optional[InvestigationStrategy], str]:
        """Determine if strategy should transition

        Transition scenarios:
        1. Active → Post-Mortem: Problem mitigated, now do thorough RCA
        2. Post-Mortem → Active: New critical issue discovered

        Args:
            current_strategy: Current investigation strategy
            investigation_state: Current investigation state

        Returns:
            Tuple of (should_transition, new_strategy, reason)
        """
        current_phase = investigation_state.lifecycle.current_phase

        # Scenario 1: Active → Post-Mortem after resolution
        if current_strategy == InvestigationStrategy.ACTIVE_INCIDENT:
            # Check if solution implemented and problem resolved
            if current_phase == InvestigationPhase.SOLUTION:
                # Check lifecycle status
                if investigation_state.lifecycle.case_status == "resolved":
                    return (
                        True,
                        InvestigationStrategy.POST_MORTEM,
                        "Problem resolved, recommend thorough post-mortem for prevention"
                    )

        # Scenario 2: Post-Mortem → Active if new critical issue found
        if current_strategy == InvestigationStrategy.POST_MORTEM:
            urgency = investigation_state.lifecycle.urgency_level
            if urgency in ["critical", "high"]:
                # Check if urgency escalated during investigation
                return (
                    True,
                    InvestigationStrategy.ACTIVE_INCIDENT,
                    "Urgency escalated to critical, switching to rapid mitigation"
                )

        return False, None, "No strategy transition needed"

    def get_strategy_config(self, strategy: InvestigationStrategy) -> Dict[str, Any]:
        """Get configuration for strategy

        Args:
            strategy: Investigation strategy

        Returns:
            Configuration dictionary
        """
        return StrategyConfig.get_config(strategy)

    def should_skip_phase(
        self,
        strategy: InvestigationStrategy,
        current_phase: InvestigationPhase,
        target_phase: InvestigationPhase,
        urgency_level: str,
    ) -> Tuple[bool, str]:
        """Determine if phase can be skipped under current strategy

        Args:
            strategy: Investigation strategy
            current_phase: Current phase
            target_phase: Target phase to skip to
            urgency_level: Current urgency level

        Returns:
            Tuple of (can_skip, reason)
        """
        config = self.get_strategy_config(strategy)

        # Post-Mortem never allows skipping
        if strategy == InvestigationStrategy.POST_MORTEM:
            return False, "Post-mortem requires all phases for thoroughness"

        # Active Incident allows skipping
        if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
            if not config["allow_phase_skipping"]:
                return False, "Phase skipping not allowed by strategy"

            # Check urgency threshold
            skip_threshold = config["urgency_skip_threshold"]
            if urgency_level not in ["high", "critical"]:
                return False, f"Urgency level {urgency_level} below threshold for skipping"

            # Validate phase progression
            # Can skip: Hypothesis (3) → Solution (5)
            # Can skip: Timeline (2) → Solution (5) for critical
            if current_phase == InvestigationPhase.HYPOTHESIS and target_phase == InvestigationPhase.SOLUTION:
                return True, "High urgency allows skipping validation for mitigation"

            if current_phase == InvestigationPhase.TIMELINE and target_phase == InvestigationPhase.SOLUTION:
                if urgency_level == "critical":
                    return True, "Critical urgency allows direct path to solution"

        return False, "Phase skip not allowed for this transition"

    def get_max_iterations_for_phase(
        self,
        strategy: InvestigationStrategy,
        phase: InvestigationPhase,
    ) -> int:
        """Get maximum iterations allowed for phase under strategy

        Args:
            strategy: Investigation strategy
            phase: Investigation phase

        Returns:
            Maximum iteration count
        """
        config = self.get_strategy_config(strategy)
        max_iterations = config["max_iterations_per_phase"]
        return max_iterations.get(phase, 5)  # Default 5 if not specified

    def get_confidence_threshold(
        self,
        strategy: InvestigationStrategy,
        context: str = "hypothesis",
    ) -> float:
        """Get confidence threshold for strategy

        Args:
            strategy: Investigation strategy
            context: "hypothesis" or "solution"

        Returns:
            Confidence threshold (0.0 to 1.0)
        """
        config = self.get_strategy_config(strategy)

        if context == "hypothesis":
            return config["min_hypothesis_confidence"]
        elif context == "solution":
            return config["solution_confidence_threshold"]
        else:
            return 0.7  # Default

    def should_offer_post_mortem(
        self,
        current_strategy: InvestigationStrategy,
        investigation_state: Any,
    ) -> Tuple[bool, str]:
        """Determine if post-mortem should be offered to user

        Args:
            current_strategy: Current investigation strategy
            investigation_state: Current investigation state

        Returns:
            Tuple of (should_offer, offer_message)
        """
        config = self.get_strategy_config(current_strategy)

        if not config["offer_post_mortem"]:
            return False, ""

        # Check if problem is resolved
        if investigation_state.lifecycle.case_status == "resolved":
            message = (
                "Your problem has been resolved. Would you like me to conduct a thorough "
                "post-mortem analysis to identify the root cause and prevent recurrence? "
                "This will involve deeper evidence gathering and comprehensive hypothesis testing."
            )
            return True, message

        return False, ""

    def get_strategy_behavior_summary(
        self,
        strategy: InvestigationStrategy,
    ) -> str:
        """Get human-readable summary of strategy behavior

        Args:
            strategy: Investigation strategy

        Returns:
            Behavior summary string
        """
        config = self.get_strategy_config(strategy)

        if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
            return (
                f"**{config['name']}**: Prioritizing rapid mitigation. "
                f"I'll focus on practical evidence gathering and proceed once we have "
                f"{int(config['min_hypothesis_confidence']*100)}%+ confidence in a root cause. "
                f"We can do a thorough post-mortem after the issue is resolved."
            )
        else:  # POST_MORTEM
            return (
                f"**{config['name']}**: Conducting thorough root cause analysis. "
                f"I'll systematically test all hypotheses and require "
                f"{int(config['min_hypothesis_confidence']*100)}%+ confidence before concluding. "
                f"This ensures we fully understand what happened and how to prevent it."
            )


# =============================================================================
# Utility Functions
# =============================================================================


def create_strategy_selector() -> InvestigationStrategySelector:
    """Factory function to create strategy selector

    Returns:
        Configured InvestigationStrategySelector instance
    """
    return InvestigationStrategySelector()


def get_strategy_from_string(strategy_str: str) -> Optional[InvestigationStrategy]:
    """Parse strategy from string

    Args:
        strategy_str: Strategy string (case-insensitive)

    Returns:
        InvestigationStrategy or None if invalid
    """
    strategy_str = strategy_str.lower().strip()

    if strategy_str in ["active_incident", "active", "incident", "urgent"]:
        return InvestigationStrategy.ACTIVE_INCIDENT
    elif strategy_str in ["post_mortem", "postmortem", "rca", "root_cause"]:
        return InvestigationStrategy.POST_MORTEM
    else:
        return None
