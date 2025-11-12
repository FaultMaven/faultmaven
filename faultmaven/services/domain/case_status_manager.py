"""Case Status Manager - Handles case status transitions

Design Principle:
- Status changes are user requests to agent (not special logic)
- Status transitions trigger agent messages
- Terminal states (RESOLVED, CLOSED) cannot be changed

Status Flow:
    CONSULTING ─────┬──────► INVESTIGATING ─────┬──────► RESOLVED (terminal)
                    │                            │
                    └───────────────────────────┴──────► CLOSED (terminal)
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from faultmaven.models.case import CaseStatus
from faultmaven.utils.serialization import to_json_compatible


# Allowed user transitions (via UI)
ALLOWED_TRANSITIONS = {
    CaseStatus.CONSULTING: [
        CaseStatus.INVESTIGATING,  # "Start investigation"
        CaseStatus.CLOSED,         # "Close without investigating"
    ],

    CaseStatus.INVESTIGATING: [
        CaseStatus.RESOLVED,       # "Mark as resolved"
        CaseStatus.CLOSED,         # "Close as unresolved"
    ],

    # Terminal states - no transitions allowed
    CaseStatus.RESOLVED: [],
    CaseStatus.CLOSED: [],
}


# Map: (old_status, new_status) → agent message
# These messages are sent to agent as if user typed them
STATUS_CHANGE_MESSAGES = {
    # CONSULTING → INVESTIGATING
    (CaseStatus.CONSULTING, CaseStatus.INVESTIGATING):
        "I want to start a formal investigation to find the root cause.",

    # CONSULTING → CLOSED
    (CaseStatus.CONSULTING, CaseStatus.CLOSED):
        "Close this case. I don't need further investigation.",

    # INVESTIGATING → RESOLVED
    (CaseStatus.INVESTIGATING, CaseStatus.RESOLVED):
        "The issue is resolved. Generate final documentation with root cause and solution.",

    # INVESTIGATING → CLOSED
    (CaseStatus.INVESTIGATING, CaseStatus.CLOSED):
        "Close this case as unresolved. Summarize what we found so far.",
}


class CaseStatusManager:
    """
    Manages case status transitions

    Design: Status changes trigger agent messages (no special logic)
    """

    @staticmethod
    def is_terminal_state(status: CaseStatus) -> bool:
        """Check if status is terminal (cannot be changed)"""
        return status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

    @staticmethod
    def validate_transition(
        old_status: CaseStatus,
        new_status: CaseStatus
    ) -> tuple[bool, Optional[str]]:
        """
        Validate status transition

        Returns:
            (is_valid, error_message)
        """
        # Cannot change terminal states
        if CaseStatusManager.is_terminal_state(old_status):
            return (
                False,
                f"Cannot change status from terminal state {old_status.value}. "
                f"To reopen, create a new case."
            )

        # Check if transition is allowed
        allowed = ALLOWED_TRANSITIONS.get(old_status, [])
        if new_status not in allowed:
            return (
                False,
                f"Invalid transition: {old_status.value} → {new_status.value}. "
                f"Allowed transitions: {[s.value for s in allowed]}"
            )

        return (True, None)

    @staticmethod
    def get_agent_message(
        old_status: CaseStatus,
        new_status: CaseStatus
    ) -> Optional[str]:
        """
        Get agent message for status transition

        This message is sent to agent as if user typed it
        """
        return STATUS_CHANGE_MESSAGES.get((old_status, new_status))

    @staticmethod
    def build_status_change_record(
        old_status: CaseStatus,
        new_status: CaseStatus,
        user_id: str,
        auto: bool = False,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build status change audit record

        Args:
            old_status: Previous status
            new_status: New status
            user_id: User ID who changed status
            auto: True if system auto-changed, False if user action
            reason: Optional reason for change

        Returns:
            Status change record for audit trail
        """
        return {
            "from_status": old_status.value,
            "to_status": new_status.value,
            "changed_at": to_json_compatible(datetime.now(timezone.utc)),
            "changed_by": user_id,
            "auto": auto,
            "reason": reason,
        }

    @staticmethod
    def get_terminal_state_fields(
        new_status: CaseStatus,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Get fields to update for terminal states

        Args:
            new_status: New terminal status
            user_id: User ID

        Returns:
            Dictionary of fields to update
        """
        now = datetime.now(timezone.utc)

        if new_status == CaseStatus.RESOLVED:
            return {
                "resolved_at": now,
                "resolved_by": user_id,
            }
        elif new_status == CaseStatus.CLOSED:
            return {
                "closed_at": now,
                "closed_by": user_id,
            }

        return {}

    @staticmethod
    def get_allowed_transitions(current_status: CaseStatus) -> list[CaseStatus]:
        """Get list of allowed transitions from current status"""
        return ALLOWED_TRANSITIONS.get(current_status, [])
