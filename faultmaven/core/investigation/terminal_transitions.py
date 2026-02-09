"""Terminal Transition Handler

Implements the User-Agent Handshake pattern for terminal state transitions.

All terminal transitions (RESOLVED, CLOSED) require explicit user confirmation.
The agent proposes a transition via ProposedTransition in its response schema,
and the system holds it pending until the user confirms in the next turn.

Design Decision (Issue B):
- solution_verified is a collaborative state, not system-determined.
- The LLM's interpretation of "it works" can be wrong (user may mean
  "this command works" not "the whole system is fixed").
- Terminal transitions are irreversible, so false positives are costly.

Flow:
1. Agent detects resolution conditions and includes ProposedTransition in response
2. System stores pending_transition on the case (does NOT execute)
3. Agent's response message asks user to confirm
4. Next turn: if user confirms, system executes transition + sets solution_verified
5. If user declines, pending_transition is cleared

Reference: investigation-lifecycle-logic.md Section 1.4
"""

import logging
from datetime import UTC, datetime
from typing import Optional

from faultmaven.modules.case.contracts import Case, CaseStatus, CaseStatusTransition

logger = logging.getLogger(__name__)


def propose_transition(
    case: Case,
    to_status: str,
    reason: str,
    summary: str,
    evidence_ids: Optional[list] = None,
) -> None:
    """
    Store a pending transition proposal on the case.

    The transition is NOT executed. It is held pending until the user
    confirms in the next turn.

    Args:
        case: Case to propose transition for
        to_status: Target status ("resolved" or "closed")
        reason: Why the agent believes this transition is appropriate
        summary: Summary presented to user for confirmation
        evidence_ids: Evidence IDs supporting the proposal
    """
    case.pending_transition = {
        "to_status": to_status,
        "reason": reason,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
        "proposed_by": "agent",
    }
    logger.info(
        f"Transition proposed for case {case.case_id}: → {to_status} "
        f"(pending user confirmation). Reason: {reason}"
    )


def confirm_pending_transition(case: Case, user_id: str) -> bool:
    """
    Execute a pending transition after user confirmation.

    Returns True if transition was executed, False if no pending transition.

    Args:
        case: Case with pending_transition
        user_id: User confirming the transition
    """
    if not hasattr(case, "pending_transition") or not case.pending_transition:
        return False

    pending = case.pending_transition
    to_status = pending["to_status"]

    if to_status == "resolved":
        _execute_resolved_transition(case, user_id, pending["reason"])
    elif to_status == "closed":
        _execute_closed_transition(case, user_id, pending["reason"])
    else:
        logger.error(f"Unknown pending transition target: {to_status}")
        case.pending_transition = None
        return False

    # Clear pending transition after execution
    case.pending_transition = None
    return True


def cancel_pending_transition(case: Case) -> bool:
    """
    Cancel a pending transition (user declined).

    Returns True if a pending transition was cleared.
    """
    if hasattr(case, "pending_transition") and case.pending_transition:
        logger.info(
            f"Pending transition cancelled for case {case.case_id}: "
            f"→ {case.pending_transition['to_status']} (user declined)"
        )
        case.pending_transition = None
        return True
    return False


def _execute_resolved_transition(case: Case, user_id: str, reason: str):
    """Execute INVESTIGATING → RESOLVED after user confirmation."""
    if case.status != CaseStatus.INVESTIGATING:
        logger.error(
            f"Cannot resolve case {case.case_id}: status is {case.status}, "
            f"expected INVESTIGATING"
        )
        return

    logger.info(
        f"User {user_id} confirmed resolution for case {case.case_id}. "
        f"Executing INVESTIGATING → RESOLVED transition."
    )

    # Set solution_verified since user confirmed
    case.progress.solution_verified = True

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
            triggered_at=now,
            triggered_by=user_id,
            reason=f"User confirmed resolution: {reason}",
        )
    )
    logger.info(f"Case {case.case_id} transitioned to RESOLVED (terminal state)")


def _execute_closed_transition(case: Case, user_id: str, reason: str):
    """Execute → CLOSED after user confirmation."""
    from_status = case.status
    if from_status not in (CaseStatus.INVESTIGATING, CaseStatus.INQUIRY):
        logger.error(
            f"Cannot close case {case.case_id}: status is {from_status}, "
            f"expected INVESTIGATING or INQUIRY"
        )
        return

    logger.info(
        f"User {user_id} confirmed closure for case {case.case_id}. "
        f"Executing {from_status.value} → CLOSED transition."
    )

    now = datetime.now(UTC)
    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=now,
        closure_reason=reason,
    )
    case.status_history.append(
        CaseStatusTransition(
            from_status=from_status,
            to_status=CaseStatus.CLOSED,
            triggered_at=now,
            triggered_by=user_id,
            reason=f"User confirmed closure: {reason}",
        )
    )
    logger.info(f"Case {case.case_id} transitioned to CLOSED (terminal state)")


# ============================================================
# EXPLICIT USER-TRIGGERED TRANSITIONS (Non-Automatic)
# These are kept for backward compatibility with the manual
# status dropdown flow (Section 1.5 of lifecycle doc).
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
