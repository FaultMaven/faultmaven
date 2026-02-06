"""Path Selection Timeline (3 Phases)

Implements the 3-phase path selection timeline:
1. Preliminary Assessment (INQUIRY)
2. Formal Path Selection (INVESTIGATING)
3. Path Execution (INVESTIGATING)

Reference: investigation-lifecycle-logic.md Section 2.0
"""

import logging
from typing import Optional

from faultmaven.modules.case.contracts import (
    Case,
    InvestigationPath,
    PathSelection,
    PreliminaryUrgency,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

logger = logging.getLogger(__name__)


# ============================================================
# Phase 1: Preliminary Assessment (INQUIRY)
# ============================================================


def assess_preliminary_urgency(case: Case, llm_provider) -> PreliminaryUrgency:
    """
    Early urgency assessment during INQUIRY.
    Provides early warning but does NOT determine path yet.

    Called: During first turn when problem_confirmation is created

    Args:
        case: Case in INQUIRY status
        llm_provider: LLM provider for semantic urgency assessment

    Returns:
        PreliminaryUrgency with level, temporal state, impact assessment

    Reference: investigation-lifecycle-logic.md lines 610-682
    """
    if not case.inquiry or not case.inquiry.problem_confirmation:
        logger.warning("Cannot assess preliminary urgency without problem_confirmation")
        return PreliminaryUrgency(
            level=UrgencyLevel.UNKNOWN,
            is_ongoing=False,
            impact_assessment="Insufficient information to assess urgency",
            mitigation_hint=None,
        )

    # Use LLM for semantic urgency detection (not keyword-based)
    # This would call llm_provider.assess_urgency() which uses the urgency signal examples
    # from the specification

    # For now, return basic assessment
    # TODO: Implement LLM-based semantic urgency detection

    return PreliminaryUrgency(
        level=(
            case.inquiry.problem_confirmation.severity_guess
            if hasattr(case.inquiry.problem_confirmation, "severity_guess")
            else UrgencyLevel.UNKNOWN
        ),
        is_ongoing=True,  # Default assumption during INQUIRY
        impact_assessment="Preliminary assessment based on initial problem description",
        mitigation_hint=None,
    )


# ============================================================
# Phase 2: Formal Path Selection (INVESTIGATING)
# ============================================================


def select_investigation_path(case: Case) -> PathSelection:
    """
    Formal path selection after symptom verification complete.

    Called: Automatically when symptom_verified transitions False → True
    Precondition: case.problem_verification with temporal_state and urgency_level set

    Args:
        case: Case in INVESTIGATING status with symptom_verified = True

    Returns:
        PathSelection with path, rationale, auto_selected flag

    Reference: investigation-lifecycle-logic.md lines 684-735
    """
    if not case.progress.symptom_verified:
        raise ValueError("Cannot select path before symptom verification")

    if not case.problem_verification:
        raise ValueError("Cannot select path without problem_verification")

    logger.info(
        f"Selecting investigation path for case {case.case_id}: "
        f"temporal={case.problem_verification.temporal_state}, "
        f"urgency={case.problem_verification.urgency_level}"
    )

    return determine_investigation_path(case.problem_verification)


def determine_investigation_path(
    problem_verification: ProblemVerification,
) -> PathSelection:
    """
    Determine investigation path after verification complete.

    Path Selection Matrix (temporal_state × urgency_level):
    - ONGOING + CRITICAL → MITIGATION_FIRST (auto)
    - ONGOING + HIGH → MITIGATION_FIRST (auto)
    - HISTORICAL + LOW/MEDIUM → ROOT_CAUSE (auto)
    - All other combinations → USER_CHOICE

    Args:
        problem_verification: Verified problem with temporal_state and urgency_level

    Returns:
        PathSelection

    Reference: investigation-lifecycle-logic.md lines 736-748, Section 2.1
    """
    temporal = problem_verification.temporal_state
    urgency = problem_verification.urgency_level

    # AUTO: Ongoing + High Urgency → MITIGATION_FIRST (then RCA)
    if temporal == TemporalState.ONGOING and urgency in [
        UrgencyLevel.CRITICAL,
        UrgencyLevel.HIGH,
    ]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=f"Ongoing {urgency.value} issue requires immediate mitigation, RCA after impact stopped",
            alternate_path=InvestigationPath.ROOT_CAUSE,
        )

    # AUTO: Historical + Low Urgency → ROOT_CAUSE (permanent solution)
    if temporal == TemporalState.HISTORICAL and urgency in [
        UrgencyLevel.LOW,
        UrgencyLevel.MEDIUM,
    ]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=f"Historical {urgency.value} issue allows thorough investigation with permanent solution",
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # USER CHOICE: Ambiguous cases - let user decide between paths
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale=f"Ambiguous case ({temporal.value} + {urgency.value}): User chooses (a) mitigation first or (b) RCA",
        alternate_path=None,
    )


# ============================================================
# Phase 3: Path Execution (INVESTIGATING)
# ============================================================


async def execute_path_behavior(case: Case):
    """
    Execute path-specific behavior immediately after selection.

    For MITIGATION_FIRST: Apply mitigation if correlation strong enough
    For ROOT_CAUSE: Continue to hypothesis formulation

    Args:
        case: Case with path_selection determined

    Reference: investigation-lifecycle-logic.md lines 705-726
    """
    if not case.path_selection:
        logger.warning(f"No path selection for case {case.case_id}, skipping execution")
        return

    logger.info(
        f"Executing path behavior for case {case.case_id}: "
        f"path={case.path_selection.path}"
    )

    if case.path_selection.path == InvestigationPath.MITIGATION_FIRST:
        # Check if we have strong correlation to apply mitigation
        if (
            case.problem_verification
            and case.problem_verification.correlation_confidence >= 0.7
        ):
            logger.info(
                f"Strong correlation detected (confidence: {case.problem_verification.correlation_confidence}) "
                f"→ applying mitigation"
            )
            # Mitigation application happens through normal LLM flow
            # Agent will suggest mitigation in next response
            # This function just marks the intent
            # await apply_mitigation(case)  # Would be implemented separately
            # case.progress.mitigation_applied = True
        else:
            logger.info(
                f"Weak correlation (confidence: {case.problem_verification.correlation_confidence if case.problem_verification else 0}) "
                f"→ gathering more evidence before mitigation"
            )

    elif case.path_selection.path == InvestigationPath.ROOT_CAUSE:
        logger.info("ROOT_CAUSE path → proceeding to hypothesis formulation")
        # Continue to normal hypothesis generation flow

    elif case.path_selection.path == InvestigationPath.USER_CHOICE:
        logger.info("USER_CHOICE path → waiting for user to select path")
        # Agent will present options to user
