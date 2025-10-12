"""Investigation Phase Definitions and Transition Logic

This module defines the 7 investigation phases (0-6) and their relationships,
completion criteria, and transition rules in the OODA framework.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Phase Overview:
- Phase 0: Intake - Problem confirmation (Consultant mode, no OODA)
- Phase 1: Blast Radius - Scope assessment (OODA: Observe, Orient)
- Phase 2: Timeline - Temporal context (OODA: Observe, Orient)
- Phase 3: Hypothesis - Theory generation (OODA: Observe, Orient, Decide)
- Phase 4: Validation - Hypothesis testing (OODA: Full cycle)
- Phase 5: Solution - Fix implementation (OODA: Decide, Act, Orient)
- Phase 6: Document - Artifact generation (OODA: Orient only)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

from faultmaven.models.investigation import (
    InvestigationPhase,
    OODAStep,
    EngagementMode,
    InvestigationStrategy,
    PhaseOODAMapping,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Phase Definitions
# =============================================================================


@dataclass
class PhaseDefinition:
    """Complete definition of an investigation phase"""

    phase: InvestigationPhase
    name: str
    description: str
    engagement_mode: EngagementMode
    ooda_steps: List[OODAStep]
    intensity: str  # "none", "light", "medium", "full"
    expected_iterations: tuple[int, int]  # (min, max)
    primary_goal: str
    entry_criteria: List[str]
    completion_criteria: List[str]
    can_skip: bool  # Can this phase be skipped (e.g., urgent incidents)
    next_phase: Optional[InvestigationPhase]


# Phase definitions (immutable configuration)
PHASE_DEFINITIONS: Dict[InvestigationPhase, PhaseDefinition] = {
    InvestigationPhase.INTAKE: PhaseDefinition(
        phase=InvestigationPhase.INTAKE,
        name="Intake",
        description="Problem confirmation and consent for investigation",
        engagement_mode=EngagementMode.CONSULTANT,
        ooda_steps=[],  # No OODA in Phase 0
        intensity="none",
        expected_iterations=(0, 0),
        primary_goal="Confirm technical problem exists and obtain consent",
        entry_criteria=[
            "Session started",
            "User has query or concern",
        ],
        completion_criteria=[
            "Problem confirmed as technical issue",
            "ProblemConfirmation structure created",
            "User consented to Lead Investigator mode",
        ],
        can_skip=False,  # Always start here
        next_phase=InvestigationPhase.BLAST_RADIUS,
    ),
    InvestigationPhase.BLAST_RADIUS: PhaseDefinition(
        phase=InvestigationPhase.BLAST_RADIUS,
        name="Blast Radius",
        description="Assess impact scope and affected components",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.OBSERVE, OODAStep.ORIENT],
        intensity="light",
        expected_iterations=(1, 2),
        primary_goal="Define problem scope and create AnomalyFrame",
        entry_criteria=[
            "Lead Investigator mode active",
            "ProblemConfirmation exists",
        ],
        completion_criteria=[
            "AnomalyFrame created with scope definition",
            "Affected components identified",
            "Severity assessed",
            "Scope evidence collected (≥60% coverage)",
        ],
        can_skip=False,
        next_phase=InvestigationPhase.TIMELINE,
    ),
    InvestigationPhase.TIMELINE: PhaseDefinition(
        phase=InvestigationPhase.TIMELINE,
        name="Timeline",
        description="Establish temporal context and change history",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.OBSERVE, OODAStep.ORIENT],
        intensity="light",
        expected_iterations=(1, 2),
        primary_goal="Identify when problem started and what changed",
        entry_criteria=[
            "AnomalyFrame exists",
            "Blast radius defined",
        ],
        completion_criteria=[
            "Problem start time identified",
            "Recent changes catalogued",
            "Timeline evidence collected (≥50% coverage)",
            "Temporal correlation established",
        ],
        can_skip=False,
        next_phase=InvestigationPhase.HYPOTHESIS,
    ),
    InvestigationPhase.HYPOTHESIS: PhaseDefinition(
        phase=InvestigationPhase.HYPOTHESIS,
        name="Hypothesis",
        description="Formulate root cause theories",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE],
        intensity="medium",
        expected_iterations=(2, 3),
        primary_goal="Generate 2-4 plausible root cause hypotheses",
        entry_criteria=[
            "Timeline established",
            "Sufficient context collected",
        ],
        completion_criteria=[
            "2-4 hypotheses generated",
            "Hypotheses ranked by likelihood",
            "Each hypothesis has category and initial confidence",
            "Evidence gaps identified for testing",
        ],
        can_skip=True,  # Can skip to Solution for critical/high urgency
        next_phase=InvestigationPhase.VALIDATION,
    ),
    InvestigationPhase.VALIDATION: PhaseDefinition(
        phase=InvestigationPhase.VALIDATION,
        name="Validation",
        description="Systematic hypothesis testing with evidence",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE, OODAStep.ACT],
        intensity="full",
        expected_iterations=(3, 6),
        primary_goal="Test hypotheses and identify validated root cause",
        entry_criteria=[
            "At least 2 hypotheses exist",
            "Testing strategy defined",
        ],
        completion_criteria=[
            "At least one hypothesis validated (confidence ≥70%)",
            "Or all hypotheses refuted (need new theories)",
            "Sufficient evidence collected for conclusion",
            "Root cause identified with supporting evidence",
        ],
        can_skip=True,  # Can skip if mitigation needed urgently
        next_phase=InvestigationPhase.SOLUTION,
    ),
    InvestigationPhase.SOLUTION: PhaseDefinition(
        phase=InvestigationPhase.SOLUTION,
        name="Solution",
        description="Implement and verify solution",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.DECIDE, OODAStep.ACT, OODAStep.ORIENT],
        intensity="medium",
        expected_iterations=(2, 4),
        primary_goal="Apply fix and verify problem resolved",
        entry_criteria=[
            "Root cause identified OR mitigation strategy defined",
            "Solution approach determined",
        ],
        completion_criteria=[
            "Solution proposed to user",
            "User confirmed implementation",
            "Verification performed (symptoms resolved)",
            "Success criteria met",
        ],
        can_skip=False,
        next_phase=InvestigationPhase.DOCUMENT,
    ),
    InvestigationPhase.DOCUMENT: PhaseDefinition(
        phase=InvestigationPhase.DOCUMENT,
        name="Document",
        description="Generate artifacts and capture learnings",
        engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ooda_steps=[OODAStep.ORIENT],  # Synthesis mode only
        intensity="light",
        expected_iterations=(1, 1),
        primary_goal="Create case report and runbook for future reference",
        entry_criteria=[
            "Problem resolved or mitigated",
            "Investigation complete",
        ],
        completion_criteria=[
            "Case report offered/generated",
            "Runbook offered/generated (if user accepts)",
            "Key learnings documented",
        ],
        can_skip=True,  # User may decline documentation
        next_phase=None,  # Terminal phase
    ),
}


def get_phase_definition(phase: InvestigationPhase) -> PhaseDefinition:
    """Get the definition for a specific phase

    Args:
        phase: Investigation phase

    Returns:
        PhaseDefinition for the phase

    Raises:
        ValueError: If phase not found
    """
    if phase not in PHASE_DEFINITIONS:
        raise ValueError(f"Unknown investigation phase: {phase}")
    return PHASE_DEFINITIONS[phase]


# =============================================================================
# Phase Transition Logic
# =============================================================================


@dataclass
class PhaseTransitionRule:
    """Rule for transitioning between phases"""

    from_phase: InvestigationPhase
    to_phase: InvestigationPhase
    condition_check: Callable[[Any], bool]  # Function to check if transition allowed
    reason: str
    is_skip: bool = False  # True if skipping intermediate phases


class PhaseCompletionCriteria:
    """Evaluates phase completion criteria"""

    @staticmethod
    def check_phase_complete(
        phase: InvestigationPhase,
        investigation_state: Any,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if phase completion criteria are met

        Args:
            phase: Current investigation phase
            investigation_state: InvestigationState object

        Returns:
            Tuple of (is_complete, met_criteria, unmet_criteria)
        """
        definition = get_phase_definition(phase)
        met_criteria = []
        unmet_criteria = []

        if phase == InvestigationPhase.INTAKE:
            # Phase 0: Problem confirmation and consent
            if investigation_state.problem_confirmation is not None:
                met_criteria.append("Problem confirmed")
            else:
                unmet_criteria.append("Problem not yet confirmed")

            if investigation_state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR:
                met_criteria.append("User consented to investigation")
            else:
                unmet_criteria.append("Awaiting user consent for Lead Investigator mode")

        elif phase == InvestigationPhase.BLAST_RADIUS:
            # Phase 1: Scope assessment
            if investigation_state.ooda_engine.anomaly_frame is not None:
                met_criteria.append("AnomalyFrame created")
            else:
                unmet_criteria.append("AnomalyFrame not yet defined")

            # Check evidence coverage for SCOPE category
            scope_evidence_count = sum(
                1 for req_id in investigation_state.evidence.evidence_requests
                if "scope" in req_id.lower()  # Simplified check
            )
            if scope_evidence_count >= 2:
                met_criteria.append("Scope evidence collected")
            else:
                unmet_criteria.append("Need more scope evidence")

        elif phase == InvestigationPhase.TIMELINE:
            # Phase 2: Temporal context
            timeline_evidence_count = sum(
                1 for req_id in investigation_state.evidence.evidence_requests
                if "timeline" in req_id.lower() or "change" in req_id.lower()
            )
            if timeline_evidence_count >= 2:
                met_criteria.append("Timeline evidence collected")
            else:
                unmet_criteria.append("Need timeline/change evidence")

            # Check if anomaly frame has temporal info
            if (
                investigation_state.ooda_engine.anomaly_frame
                and investigation_state.ooda_engine.anomaly_frame.started_at
            ):
                met_criteria.append("Problem start time identified")
            else:
                unmet_criteria.append("Problem start time not identified")

        elif phase == InvestigationPhase.HYPOTHESIS:
            # Phase 3: Hypothesis generation
            hypothesis_count = len(investigation_state.ooda_engine.hypotheses)
            if hypothesis_count >= 2:
                met_criteria.append(f"{hypothesis_count} hypotheses generated")
            else:
                unmet_criteria.append(f"Need at least 2 hypotheses (have {hypothesis_count})")

            # Check if hypotheses are ranked
            ranked_count = sum(
                1 for h in investigation_state.ooda_engine.hypotheses if h.likelihood > 0
            )
            if ranked_count == hypothesis_count and hypothesis_count > 0:
                met_criteria.append("Hypotheses ranked")
            else:
                unmet_criteria.append("Hypotheses not yet ranked")

        elif phase == InvestigationPhase.VALIDATION:
            # Phase 4: Hypothesis testing
            validated_count = sum(
                1
                for h in investigation_state.ooda_engine.hypotheses
                if h.status.value == "validated" and h.likelihood >= 0.7
            )

            if validated_count >= 1:
                met_criteria.append(f"Hypothesis validated (confidence ≥70%)")
            else:
                # Check if all refuted (need new hypotheses)
                refuted_count = sum(
                    1 for h in investigation_state.ooda_engine.hypotheses if h.status.value == "refuted"
                )
                if refuted_count == len(investigation_state.ooda_engine.hypotheses):
                    met_criteria.append("All hypotheses refuted - need new theories")
                else:
                    unmet_criteria.append("No validated hypothesis yet")

            # Check iteration count (prevent infinite loops)
            if investigation_state.ooda_engine.current_iteration >= 6:
                met_criteria.append("Max iterations reached")

        elif phase == InvestigationPhase.SOLUTION:
            # Phase 5: Solution implementation
            # Check if solution was implemented and verified
            # This requires checking the conversation or state for implementation confirmation
            # Simplified: check if we're past minimum iterations
            if investigation_state.lifecycle.turns_in_current_phase >= 2:
                met_criteria.append("Solution phase progressed")
            else:
                unmet_criteria.append("Solution implementation in progress")

        elif phase == InvestigationPhase.DOCUMENT:
            # Phase 6: Documentation (always completes in 1 iteration)
            if investigation_state.ooda_engine.current_iteration >= 1:
                met_criteria.append("Documentation offered")
            else:
                unmet_criteria.append("Documentation generation pending")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria


def can_transition(
    from_phase: InvestigationPhase,
    to_phase: InvestigationPhase,
    investigation_state: Any,
) -> tuple[bool, str]:
    """Check if transition between phases is allowed

    Args:
        from_phase: Current phase
        to_phase: Target phase
        investigation_state: InvestigationState object

    Returns:
        Tuple of (can_transition, reason)
    """
    from_def = get_phase_definition(from_phase)
    to_def = get_phase_definition(to_phase)

    # Check if current phase is complete
    is_complete, met, unmet = PhaseCompletionCriteria.check_phase_complete(
        from_phase, investigation_state
    )

    if not is_complete:
        return False, f"Current phase not complete: {', '.join(unmet)}"

    # Check if target phase is the natural next phase
    if from_def.next_phase == to_phase:
        return True, "Natural phase progression"

    # Check if we're skipping phases (allowed for urgent cases)
    if to_phase.value > from_phase.value:
        # Forward skip
        urgency = investigation_state.lifecycle.urgency_level
        if urgency in ["high", "critical"]:
            # Can skip Hypothesis and/or Validation for urgent cases
            if to_phase == InvestigationPhase.SOLUTION:
                return True, f"Skipping to solution due to {urgency} urgency"

        return False, "Cannot skip phases without high/critical urgency"

    # Backward transitions not allowed
    if to_phase.value < from_phase.value:
        return False, "Cannot transition backward to earlier phases"

    return False, "Invalid phase transition"


# =============================================================================
# Entry Phase Detection
# =============================================================================


def detect_entry_phase(
    problem_confirmation: Optional[Any],
    urgency_level: str,
    investigation_strategy: Optional[InvestigationStrategy],
) -> InvestigationPhase:
    """Detect which phase investigation should start at

    Most cases start at Phase 0 (Intake), but can start later if:
    - Problem already confirmed
    - Critical urgency (skip to later phases)

    Args:
        problem_confirmation: Existing ProblemConfirmation if any
        urgency_level: low, medium, high, critical
        investigation_strategy: Active incident or post-mortem

    Returns:
        Starting investigation phase
    """
    # Default: Start at Intake
    if problem_confirmation is None:
        return InvestigationPhase.INTAKE

    # Problem already confirmed, can start at Blast Radius
    if urgency_level == "critical":
        # Critical incidents: Skip hypothesis, go straight to solution after timeline
        logger.info("Critical urgency: Starting investigation at Timeline phase")
        return InvestigationPhase.TIMELINE

    if urgency_level == "high":
        # High urgency: Start at blast radius, may skip hypothesis later
        logger.info("High urgency: Starting investigation at Blast Radius phase")
        return InvestigationPhase.BLAST_RADIUS

    # Normal flow: Start at Blast Radius (problem already confirmed)
    return InvestigationPhase.BLAST_RADIUS


# =============================================================================
# Phase Utilities
# =============================================================================


def get_phase_name(phase: InvestigationPhase) -> str:
    """Get human-readable phase name

    Args:
        phase: Investigation phase

    Returns:
        Phase name (e.g., "Blast Radius")
    """
    return get_phase_definition(phase).name


def get_ooda_steps_for_phase(phase: InvestigationPhase) -> List[OODAStep]:
    """Get active OODA steps for a phase

    Args:
        phase: Investigation phase

    Returns:
        List of active OODA steps
    """
    return get_phase_definition(phase).ooda_steps


def should_advance_phase(
    phase: InvestigationPhase,
    investigation_state: Any,
    max_stall_turns: int = 5,
) -> tuple[bool, str]:
    """Determine if phase should advance (stall detection)

    Args:
        phase: Current investigation phase
        investigation_state: InvestigationState object
        max_stall_turns: Max turns in phase without completion

    Returns:
        Tuple of (should_advance, reason)
    """
    # Check completion criteria
    is_complete, met, unmet = PhaseCompletionCriteria.check_phase_complete(
        phase, investigation_state
    )

    if is_complete:
        return True, "Phase completion criteria met"

    # Check for stall (too many turns without progress)
    if investigation_state.lifecycle.turns_in_current_phase >= max_stall_turns:
        definition = get_phase_definition(phase)
        if definition.can_skip:
            return True, f"Phase stalled after {max_stall_turns} turns, advancing"

    return False, "Phase still in progress"
