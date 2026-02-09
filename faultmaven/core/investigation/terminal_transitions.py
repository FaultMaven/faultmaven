"""Terminal Transition Handler

Implements automatic transitions to terminal states (RESOLVED/CLOSED).

Reference: investigation-lifecycle-logic.md Section 1.4
"""

import logging
from datetime import UTC, datetime

from faultmaven.modules.case.contracts import Case, CaseStatus, CaseStatusTransition

logger = logging.getLogger(__name__)


async def check_terminal_transitions(case: Case):
    """
    Check and execute automatic transitions to terminal states.

    INVESTIGATING → RESOLVED:
    - Trigger: solution_verified = True
    - Automatic: Yes (no user confirmation needed)
    - Terminal: Yes (irreversible)

    INVESTIGATING → CLOSED:
    - Trigger: User explicit action (force_close)
    - Automatic: No (requires user intent)
    - Terminal: Yes (irreversible)

    INQUIRY → CLOSED:
    - Trigger: User explicit action (close_from_inquiry)
    - Automatic: No (requires user intent)
    - Terminal: Yes (irreversible)

    INQUIRY → RESOLVED:
    - Trigger: Fast-track KB resolution + user confirmation
    - Automatic: After confirmation only
    - Terminal: Yes (irreversible)

    Reference: investigation-lifecycle-logic.md lines 290-388
    """

    # Only check if not already terminal
    if case.is_terminal:
        return

    # AUTOMATIC: INVESTIGATING → RESOLVED
    if case.status == CaseStatus.INVESTIGATING:
        if case.progress.solution_verified:
            logger.info(
                f"solution_verified = True detected for case {case.case_id} → "
                f"auto-transitioning INVESTIGATING → RESOLVED"
            )

            # Use Case.atomic_update() to set multiple fields atomically
            # This avoids Pydantic validation Catch-22 where:
            # - Cannot set status=RESOLVED if resolved_at is None
            # - Cannot set resolved_at if status != RESOLVED
            now = datetime.now(UTC)
            case.atomic_update(
                status=CaseStatus.RESOLVED,
                resolved_at=now,
                closed_at=now,
                closure_reason="resolved",
            )
            case.status_history.append(
                CaseStatusTransition(
                    from_status=CaseStatus.INVESTIGATING,
                    to_status=CaseStatus.RESOLVED,
                    triggered_at=datetime.now(UTC),
                    triggered_by="system",
                    reason="Solution verified - automatic transition",
                )
            )

            logger.info(
                f"Case {case.case_id} transitioned to RESOLVED (terminal state)"
            )
            # TERMINAL - no further transitions

    # Note: INVESTIGATING → CLOSED requires explicit user force_close() call
    # Note: INQUIRY → CLOSED requires explicit user close_from_inquiry() call
    # These are NOT automatic transitions


# ============================================================
# EXPLICIT USER-TRIGGERED TRANSITIONS (Non-Automatic)
# ============================================================


def force_close_investigation(case: Case, user_id: str, reason: str):
    """
    User explicitly abandons investigation without solution.

    Trigger: User action (not automatic)
    Terminal: Yes (irreversible)

    Args:
        case: Case in INVESTIGATING status
        user_id: User initiating closure
        reason: Closure reason ("abandoned" | "escalated" | "other")

    Reference: investigation-lifecycle-logic.md lines 344-364
    """
    if case.status != CaseStatus.INVESTIGATING:
        raise ValueError(
            f"Can only force-close from INVESTIGATING status, got {case.status}"
        )

    logger.info(
        f"User {user_id} force-closing case {case.case_id} from INVESTIGATING. "
        f"Reason: {reason}"
    )

    # Use Case.atomic_update() to avoid validation Catch-22
    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason=reason,  # "abandoned" | "escalated" | "other"
    )
    case.status_history.append(
        CaseStatusTransition(
            from_status=CaseStatus.INVESTIGATING,
            to_status=CaseStatus.CLOSED,
            triggered_at=datetime.now(UTC),
            triggered_by=user_id,
            reason=f"User force-closed: {reason}",
        )
    )

    logger.info(f"Case {case.case_id} force-closed → CLOSED (terminal state)")
    # TERMINAL - no further transitions


def close_from_inquiry(case: Case, user_id: str):
    """
    Close after inquiry without formal investigation.

    Trigger: User action (not automatic)
    Terminal: Yes (irreversible)

    Args:
        case: Case in INQUIRY status
        user_id: User initiating closure

    Reference: investigation-lifecycle-logic.md lines 367-387
    """
    if case.status != CaseStatus.INQUIRY:
        raise ValueError(
            f"Can only close-from-inquiry when in INQUIRY status, got {case.status}"
        )

    logger.info(
        f"User {user_id} closing case {case.case_id} from INQUIRY without investigation"
    )

    # Use Case.atomic_update() to avoid validation Catch-22
    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason="inquiry_only",
    )
    case.status_history.append(
        CaseStatusTransition(
            from_status=CaseStatus.INQUIRY,
            to_status=CaseStatus.CLOSED,
            triggered_at=datetime.now(UTC),
            triggered_by=user_id,
            reason="User closed after inquiry only",
        )
    )

    logger.info(f"Case {case.case_id} closed from inquiry → CLOSED (terminal state)")
    # TERMINAL - no further transitions
