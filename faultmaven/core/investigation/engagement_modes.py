"""Engagement Mode Manager - Consultant vs Lead Investigator

This module manages the dual engagement modes of FaultMaven's investigation system:
- Consultant Mode: Expert colleague providing guidance (Phase 0)
- Lead Investigator Mode: War room lead driving resolution (Phases 1-6)

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Mode Characteristics:
- Consultant: Reactive, follows user's lead, answers questions
- Lead Investigator: Proactive, guides methodology, requests evidence

Mode Transitions:
- Consultant → Lead Investigator: When problem confirmed + user consents
- Lead Investigator → Consultant: When case resolved or user pauses

Problem Signal Detection:
- Weak signals: "I have a question", "How do I...", "What is..."
- Strong signals: "It's broken", "Error occurred", "Not working"
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any

from faultmaven.models.investigation import (
    EngagementMode,
    InvestigationPhase,
    InvestigationStrategy,
    ProblemConfirmation,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Problem Signal Detection
# =============================================================================


class ProblemSignalStrength(str, Enum):
    """Strength of problem signals in user input"""

    NONE = "none"  # No problem detected
    WEAK = "weak"  # Might be a problem
    MODERATE = "moderate"  # Likely a problem
    STRONG = "strong"  # Definite problem


class ProblemSignalDetector:
    """Detects problem signals in user queries to determine engagement mode"""

    # Strong problem keywords (definite technical issue)
    STRONG_PROBLEM_KEYWORDS = [
        "error",
        "broken",
        "not working",
        "failing",
        "crashed",
        "down",
        "outage",
        "timeout",
        "exception",
        "bug",
        "issue",
        "problem",
        "failure",
        "can't",
        "cannot",
        "unable",
        "doesn't work",
    ]

    # Moderate problem keywords (likely issue)
    MODERATE_PROBLEM_KEYWORDS = [
        "slow",
        "latency",
        "performance",
        "strange",
        "weird",
        "unexpected",
        "wrong",
        "incorrect",
        "inconsistent",
    ]

    # Weak problem keywords (might be informational)
    WEAK_PROBLEM_KEYWORDS = [
        "concern",
        "worried",
        "question about",
        "wondering",
        "suspicious",
    ]

    # Non-problem keywords (informational queries)
    INFORMATIONAL_KEYWORDS = [
        "how do i",
        "how to",
        "what is",
        "explain",
        "documentation",
        "tutorial",
        "best practice",
        "recommend",
        "should i",
    ]

    # Critical urgency keywords (emergency situations)
    CRITICAL_URGENCY_KEYWORDS = [
        "production down",
        "all users affected",
        "complete outage",
        "revenue impacting",
        "data loss",
        "security breach",
        "system down",
        "total failure",
        "everything down",
        "cannot access",
        "all customers",
    ]

    # Temporal active keywords (happening now)
    TEMPORAL_ACTIVE_KEYWORDS = [
        "happening now",
        "currently",
        "right now",
        "just started",
        "in progress",
        "ongoing",
        "at this moment",
        "as we speak",
        "active",
        "live issue",
    ]

    # Scope total keywords (widespread impact)
    SCOPE_TOTAL_KEYWORDS = [
        "all users",
        "entire system",
        "complete failure",
        "everything",
        "global",
        "all regions",
        "whole platform",
        "every",
        "across the board",
        "company-wide",
    ]

    @classmethod
    def detect_signal_strength(
        cls, query: str
    ) -> Tuple[ProblemSignalStrength, List[str], Optional[str], Optional[str], Optional[str]]:
        """Detect problem signal strength and contextual hints in user query

        Args:
            query: User query text

        Returns:
            Tuple of (signal_strength, detected_keywords, urgency_hint, temporal_hint, scope_hint)
            - signal_strength: STRONG/MODERATE/WEAK/NONE
            - detected_keywords: List of matched keywords
            - urgency_hint: "critical"|"high"|"medium"|"low"|None
            - temporal_hint: "active"|"recent"|"historical"|None
            - scope_hint: "total"|"partial"|"isolated"|None
        """
        query_lower = query.lower()
        detected_keywords = []

        # Detect urgency level
        urgency_hint: Optional[str] = None
        if any(kw in query_lower for kw in cls.CRITICAL_URGENCY_KEYWORDS):
            urgency_hint = "critical"
        elif "production" in query_lower or "users" in query_lower:
            urgency_hint = "high"

        # Detect temporal context
        temporal_hint: Optional[str] = None
        if any(kw in query_lower for kw in cls.TEMPORAL_ACTIVE_KEYWORDS):
            temporal_hint = "active"
        elif any(word in query_lower for word in ["today", "this morning", "this hour"]):
            temporal_hint = "recent"
        elif any(word in query_lower for word in ["yesterday", "last week", "last month"]):
            temporal_hint = "historical"

        # Detect scope
        scope_hint: Optional[str] = None
        if any(kw in query_lower for kw in cls.SCOPE_TOTAL_KEYWORDS):
            scope_hint = "total"
        elif any(word in query_lower for word in ["some users", "one customer", "specific"]):
            scope_hint = "partial"
        elif any(word in query_lower for word in ["my machine", "local", "just me"]):
            scope_hint = "isolated"

        # Check for informational intent first
        for keyword in cls.INFORMATIONAL_KEYWORDS:
            if keyword in query_lower:
                detected_keywords.append(keyword)

        if detected_keywords:
            return ProblemSignalStrength.NONE, detected_keywords, urgency_hint, temporal_hint, scope_hint

        # Check for strong problem signals
        for keyword in cls.STRONG_PROBLEM_KEYWORDS:
            if keyword in query_lower:
                detected_keywords.append(keyword)

        if detected_keywords:
            return ProblemSignalStrength.STRONG, detected_keywords, urgency_hint, temporal_hint, scope_hint

        # Check for moderate signals
        for keyword in cls.MODERATE_PROBLEM_KEYWORDS:
            if keyword in query_lower:
                detected_keywords.append(keyword)

        if detected_keywords:
            return ProblemSignalStrength.MODERATE, detected_keywords, urgency_hint, temporal_hint, scope_hint

        # Check for weak signals
        for keyword in cls.WEAK_PROBLEM_KEYWORDS:
            if keyword in query_lower:
                detected_keywords.append(keyword)

        if detected_keywords:
            return ProblemSignalStrength.WEAK, detected_keywords, urgency_hint, temporal_hint, scope_hint

        return ProblemSignalStrength.NONE, [], urgency_hint, temporal_hint, scope_hint

    @classmethod
    def requires_problem_confirmation(cls, signal_strength: ProblemSignalStrength) -> bool:
        """Determine if problem confirmation needed

        Args:
            signal_strength: Detected signal strength

        Returns:
            True if confirmation needed
        """
        return signal_strength in [
            ProblemSignalStrength.MODERATE,
            ProblemSignalStrength.STRONG,
        ]


# =============================================================================
# Engagement Mode Manager
# =============================================================================


class EngagementModeManager:
    """Manages engagement mode transitions and behavior

    Responsibilities:
    - Detect problem signals in user input
    - Manage mode transitions (Consultant ↔ Lead Investigator)
    - Handle consent workflow for mode activation
    - Define mode-specific behavior
    """

    def __init__(self):
        """Initialize engagement mode manager"""
        self.signal_detector = ProblemSignalDetector()
        self.logger = logging.getLogger(__name__)

    def analyze_initial_query(self, query: str) -> Dict[str, Any]:
        """Analyze initial user query to determine engagement mode

        Args:
            query: User's initial query

        Returns:
            Analysis result with signal strength, recommended mode, and contextual hints
        """
        signal_strength, keywords, urgency_hint, temporal_hint, scope_hint = \
            self.signal_detector.detect_signal_strength(query)

        result = {
            "signal_strength": signal_strength.value,
            "detected_keywords": keywords,
            "urgency_hint": urgency_hint,
            "temporal_hint": temporal_hint,
            "scope_hint": scope_hint,
            "recommended_mode": EngagementMode.CONSULTANT,  # Default
            "requires_confirmation": False,
            "suggested_response_type": "answer",
        }

        # Determine recommended mode
        if signal_strength == ProblemSignalStrength.STRONG:
            result["recommended_mode"] = EngagementMode.CONSULTANT  # Start in Consultant
            result["requires_confirmation"] = True
            result["suggested_response_type"] = "problem_confirmation"
            self.logger.info(f"Strong problem signal detected: {keywords}")

        elif signal_strength == ProblemSignalStrength.MODERATE:
            result["recommended_mode"] = EngagementMode.CONSULTANT
            result["requires_confirmation"] = True
            result["suggested_response_type"] = "problem_confirmation"
            self.logger.info(f"Moderate problem signal detected: {keywords}")

        elif signal_strength == ProblemSignalStrength.WEAK:
            result["recommended_mode"] = EngagementMode.CONSULTANT
            result["requires_confirmation"] = False
            result["suggested_response_type"] = "clarification"

        else:  # NONE
            result["recommended_mode"] = EngagementMode.CONSULTANT
            result["requires_confirmation"] = False
            result["suggested_response_type"] = "answer"

        return result

    def create_problem_confirmation(
        self,
        query: str,
        conversation_context: Optional[str] = None,
    ) -> ProblemConfirmation:
        """Create ProblemConfirmation structure from user query with enhanced signal detection

        Args:
            query: User query describing problem
            conversation_context: Optional conversation history

        Returns:
            ProblemConfirmation object with urgency_signals metadata
        """
        # Enhanced signal detection with urgency/temporal/scope hints
        signal_strength, keywords, urgency_hint, temporal_hint, scope_hint = \
            self.signal_detector.detect_signal_strength(query)

        # More nuanced severity estimation using contextual hints
        if urgency_hint == "critical" or (signal_strength == ProblemSignalStrength.STRONG and scope_hint == "total"):
            severity = "critical"
        elif urgency_hint == "high" or signal_strength == ProblemSignalStrength.STRONG:
            severity = "high"
        elif signal_strength == ProblemSignalStrength.MODERATE:
            severity = "medium"
        else:
            severity = "low"

        # Determine investigation approach based on temporal hint
        if temporal_hint == "active":
            investigation_approach = "active_incident"
        else:
            investigation_approach = "systematic"

        # Build impact description with scope hint
        if scope_hint:
            impact = f"Scope: {scope_hint}"
        else:
            impact = "Unknown"

        confirmation = ProblemConfirmation(
            problem_statement=query[:200],  # Truncate long queries
            affected_components=[],  # To be filled by Phase 1 OODA
            severity=severity,
            impact=impact,
            investigation_approach=investigation_approach,
            estimated_evidence_needed=["symptoms", "timeline", "scope"],
            urgency_signals={
                "urgency_hint": urgency_hint,
                "temporal_hint": temporal_hint,
                "scope_hint": scope_hint,
                "source": "initial_query_keywords"
            }
        )

        return confirmation

    def can_transition_to_lead_investigator(
        self,
        problem_confirmation: Optional[ProblemConfirmation],
        user_consented: bool,
    ) -> Tuple[bool, str]:
        """Check if transition to Lead Investigator mode is allowed

        Args:
            problem_confirmation: ProblemConfirmation object if created
            user_consented: Whether user has consented to investigation

        Returns:
            Tuple of (can_transition, reason)
        """
        if problem_confirmation is None:
            return False, "No problem confirmation created yet"

        if not user_consented:
            return False, "User has not consented to investigation"

        return True, "Problem confirmed and user consented"

    def select_investigation_strategy(
        self,
        problem_confirmation: ProblemConfirmation,
        urgency_level: str,
        user_preference: Optional[str] = None,
    ) -> InvestigationStrategy:
        """Select investigation strategy (Active Incident vs Post-Mortem)

        Args:
            problem_confirmation: Problem confirmation structure
            urgency_level: low, medium, high, critical
            user_preference: Optional user-specified strategy

        Returns:
            Selected investigation strategy
        """
        # User preference takes precedence
        if user_preference:
            if user_preference.lower() == "post_mortem":
                return InvestigationStrategy.POST_MORTEM
            elif user_preference.lower() == "active_incident":
                return InvestigationStrategy.ACTIVE_INCIDENT

        # Critical/High urgency → Active Incident (speed priority)
        if urgency_level in ["critical", "high"]:
            self.logger.info(f"Selecting ACTIVE_INCIDENT strategy due to {urgency_level} urgency")
            return InvestigationStrategy.ACTIVE_INCIDENT

        # Check severity from problem confirmation
        if problem_confirmation.severity in ["critical", "high"]:
            return InvestigationStrategy.ACTIVE_INCIDENT

        # Default to Active Incident (most common case)
        # Post-Mortem typically selected explicitly for retrospective analysis
        return InvestigationStrategy.ACTIVE_INCIDENT

    def generate_mode_transition_prompt(
        self,
        from_mode: EngagementMode,
        to_mode: EngagementMode,
        problem_confirmation: Optional[ProblemConfirmation] = None,
    ) -> str:
        """Generate user-facing prompt for mode transition

        Args:
            from_mode: Current engagement mode
            to_mode: Target engagement mode
            problem_confirmation: Problem confirmation if transitioning to Lead Investigator

        Returns:
            User-facing transition prompt
        """
        if (
            from_mode == EngagementMode.CONSULTANT
            and to_mode == EngagementMode.LEAD_INVESTIGATOR
        ):
            if problem_confirmation:
                return (
                    f"I understand you're experiencing: {problem_confirmation.problem_statement}\n\n"
                    f"I can help investigate this systematically. This will involve:\n"
                    f"- Gathering evidence about the problem scope and timeline\n"
                    f"- Testing hypotheses to identify the root cause\n"
                    f"- Proposing solutions based on findings\n\n"
                    f"Would you like me to lead this investigation?"
                )

        elif (
            from_mode == EngagementMode.LEAD_INVESTIGATOR
            and to_mode == EngagementMode.CONSULTANT
        ):
            return (
                "Investigation complete. I'm here if you have any other questions "
                "or need help with something else."
            )

        return ""

    def get_mode_behavior_config(self, mode: EngagementMode) -> Dict[str, Any]:
        """Get behavior configuration for engagement mode

        Args:
            mode: Engagement mode

        Returns:
            Configuration dictionary with behavior settings
        """
        if mode == EngagementMode.CONSULTANT:
            return {
                "proactivity_level": "low",
                "evidence_requests": "minimal",
                "methodology_enforcement": "none",
                "response_style": "collaborative",
                "assumes_control": False,
                "phases_active": [InvestigationPhase.INTAKE],
                "ooda_active": False,
            }

        else:  # LEAD_INVESTIGATOR
            return {
                "proactivity_level": "high",
                "evidence_requests": "systematic",
                "methodology_enforcement": "strict",
                "response_style": "directive",
                "assumes_control": True,
                "phases_active": [
                    InvestigationPhase.BLAST_RADIUS,
                    InvestigationPhase.TIMELINE,
                    InvestigationPhase.HYPOTHESIS,
                    InvestigationPhase.VALIDATION,
                    InvestigationPhase.SOLUTION,
                    InvestigationPhase.DOCUMENT,
                ],
                "ooda_active": True,
            }

    def handle_mode_switch_request(
        self,
        current_mode: EngagementMode,
        investigation_state: Any,
        user_intent: str,
    ) -> Tuple[bool, EngagementMode, str]:
        """Handle explicit user request to change mode

        Args:
            current_mode: Current engagement mode
            investigation_state: Current investigation state
            user_intent: User's stated intent (pause, resume, stop, etc.)

        Returns:
            Tuple of (should_switch, new_mode, reason)
        """
        if user_intent == "pause_investigation":
            if current_mode == EngagementMode.LEAD_INVESTIGATOR:
                return True, EngagementMode.CONSULTANT, "User requested pause"

        elif user_intent == "resume_investigation":
            if current_mode == EngagementMode.CONSULTANT:
                # Check if we can resume
                if investigation_state.problem_confirmation:
                    return True, EngagementMode.LEAD_INVESTIGATOR, "User requested resume"

        elif user_intent == "stop_investigation":
            if current_mode == EngagementMode.LEAD_INVESTIGATOR:
                return True, EngagementMode.CONSULTANT, "User stopped investigation"

        return False, current_mode, "No mode change needed"


# =============================================================================
# Utility Functions
# =============================================================================


def create_engagement_mode_manager() -> EngagementModeManager:
    """Factory function to create engagement mode manager

    Returns:
        Configured EngagementModeManager instance
    """
    return EngagementModeManager()


def detect_problem_in_query(query: str) -> Tuple[bool, ProblemSignalStrength]:
    """Quick utility to detect if query contains problem signal

    Args:
        query: User query

    Returns:
        Tuple of (has_problem, signal_strength)
    """
    detector = ProblemSignalDetector()
    strength, _ = detector.detect_signal_strength(query)
    has_problem = strength in [
        ProblemSignalStrength.MODERATE,
        ProblemSignalStrength.STRONG,
    ]
    return has_problem, strength
