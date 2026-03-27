"""Investigation Routing Service

This module is responsible for determining the optimal investigation path
(MITIGATION_FIRST vs ROOT_CAUSE) based on urgency and temporal state.

Design Reference:
- docs/architecture/investigation-engine/investigation-lifecycle-logic.md
"""

import logging
from typing import Optional

from faultmaven.modules.case.contracts import (
    InvestigationPath,
    PathSelection,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)

logger = logging.getLogger(__name__)


def determine_investigation_path(verification: ProblemVerification) -> PathSelection:
    """
    Determine investigation path based on Urgency x Temporal matrix.

    Args:
        verification: Consolidated problem verification data

    Returns:
        PathSelection object with chosen path and rationale
    """
    temporal = verification.temporal_state
    urgency = verification.urgency_level

    logger.info(f"Determining path for Temporal:{temporal} Urgency:{urgency}")

    # Defaults in case fields are missing (fallback to user choice)
    if not temporal or urgency == UrgencyLevel.UNKNOWN:
        return PathSelection(
            path=InvestigationPath.USER_CHOICE,
            auto_selected=False,
            rationale="Ambiguous urgency or temporal state",
            alternate_path=None,
        )

    # AUTO: Ongoing + High Urgency -> MITIGATION_FIRST (then RCA)
    # Stop the bleeding before finding root cause
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

    # AUTO: Historical + Low/Medium -> ROOT_CAUSE (permanent solution)
    # Historical issues at low/medium urgency don't need mitigation path
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

    # USER_CHOICE: Historical + High/Critical
    # Even historical critical issues may benefit from mitigation-first approach
    if temporal == TemporalState.HISTORICAL and urgency in [
        UrgencyLevel.HIGH,
        UrgencyLevel.CRITICAL,
    ]:
        return PathSelection(
            path=InvestigationPath.USER_CHOICE,
            auto_selected=False,
            rationale=f"Historical {urgency.value} issue: User chooses (a) mitigation first or (b) root cause analysis",
            alternate_path=None,
        )

    # USER CHOICE: Ambiguous cases
    # - Ongoing + Low/Medium: User might want quick fix OR proper fix
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale=f"Ambiguous case ({temporal.value} + {urgency.value}): User chooses (a) mitigation first or (b) RCA",
        alternate_path=None,
    )
