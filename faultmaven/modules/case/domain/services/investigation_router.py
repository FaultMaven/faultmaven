"""Investigation Routing Service

This module is responsible for determining the optimal investigation path
(MITIGATION_FIRST vs ROOT_CAUSE) based on urgency and temporal state.

The router returns a *recommendation*; the user confirms or overrides via
Gate 2 (see PathSelection.user_confirmed). The router never returns
USER_CHOICE — ambiguous cases default to ROOT_CAUSE with auto_selected=False
and an honest rationale, and the user can switch to MITIGATION_FIRST in
Gate 2 if they have out-of-band context (e.g., mitigation already applied
elsewhere, telemetry doesn't capture the full impact).

Note: The rca_infeasible advisory signal on ProblemVerification does NOT affect
path selection. It influences post-mitigation agent behavior only (see §2.4 of
investigation-lifecycle-logic.md). Path selection remains purely urgency × temporal.

Design Reference:
- docs/architecture/investigation-engine/investigation-lifecycle-logic.md
"""

import logging

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
    Determine investigation path recommendation from the Urgency × Temporal matrix.

    Returns a PathSelection populated with the recommended path,
    ``auto_selected`` indicating whether the matrix matched a row (True) or
    the router fell back to the safe default (False), and a rationale string
    surfaced to the user in Gate 2.

    Matrix outcomes:
    - ONGOING + CRITICAL/HIGH   -> MITIGATION_FIRST (auto)
    - ONGOING + MEDIUM/LOW      -> ROOT_CAUSE (auto)
    - HISTORICAL + any urgency  -> ROOT_CAUSE (auto)
    - missing/UNKNOWN signals   -> ROOT_CAUSE (default, not auto-selected)

    All outcomes are recommendations — Gate 2 requires explicit user
    confirmation before the path commits (see PathSelection.user_confirmed).

    Args:
        verification: Consolidated problem verification data

    Returns:
        PathSelection with path, auto_selected, rationale, alternate_path.
        user_confirmed defaults to False — caller must obtain Gate 2 confirmation.
    """
    temporal = verification.temporal_state
    urgency = verification.urgency_level

    logger.info(f"Determining path for Temporal:{temporal} Urgency:{urgency}")

    # Ambiguous: missing temporal or unknown urgency.
    # Default to ROOT_CAUSE (safer for non-emergency); user overrides via Gate 2
    # if they have context the data doesn't capture.
    if not temporal or urgency == UrgencyLevel.UNKNOWN:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=False,
            rationale="Urgency or temporal signal missing — defaulting to root-cause "
            "analysis. Switch to mitigation-first if you have active impact "
            "the data doesn't capture.",
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # AUTO: Ongoing + High/Critical Urgency -> MITIGATION_FIRST (then RCA)
    # Stop the bleeding before finding root cause.
    if temporal == TemporalState.ONGOING and urgency in [
        UrgencyLevel.CRITICAL,
        UrgencyLevel.HIGH,
    ]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=(
                f"Ongoing {urgency.value} impact — recommend mitigating first, "
                "RCA after stabilization."
            ),
            alternate_path=InvestigationPath.ROOT_CAUSE,
        )

    # AUTO: Ongoing + Low/Medium -> ROOT_CAUSE
    # Active issue but not urgent — can afford a thorough investigation.
    if temporal == TemporalState.ONGOING and urgency in [
        UrgencyLevel.LOW,
        UrgencyLevel.MEDIUM,
    ]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=(
                f"Ongoing but {urgency.value} urgency — recommend root-cause "
                "analysis for a permanent fix."
            ),
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # AUTO: Historical + any urgency -> ROOT_CAUSE
    # Past issue — immediate impact has subsided, focus on permanent fix.
    if temporal == TemporalState.HISTORICAL:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=(
                f"Historical {urgency.value} issue — recommend root-cause analysis "
                "since immediate impact has subsided."
            ),
            alternate_path=InvestigationPath.MITIGATION_FIRST,
        )

    # Defensive fallback. The matrix above covers every combination of
    # (TemporalState, UrgencyLevel) modulo the missing-signal branch, so this
    # is unreachable today; if a new enum value is added to either field the
    # router falls back to ROOT_CAUSE rather than crashing.
    logger.warning(
        f"Path matrix did not match (temporal={temporal}, urgency={urgency}); "
        "defaulting to ROOT_CAUSE"
    )
    return PathSelection(
        path=InvestigationPath.ROOT_CAUSE,
        auto_selected=False,
        rationale=(
            f"Unmatched combination ({temporal.value} + {urgency.value}) — "
            "defaulting to root-cause analysis."
        ),
        alternate_path=InvestigationPath.MITIGATION_FIRST,
    )
