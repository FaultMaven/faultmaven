"""Case Action Manager — Handles case actions (phase transitions and dispositions).

Terminology (see Investigation Terminology Guide):
- Phase: Active work period (INQUIRY, INVESTIGATING)
- Disposition: Terminal resolution (RESOLVED, CLOSED)
- Case Action: Any phase transition or disposition change

Design Principle:
- Case actions are user requests to agent (not special logic)
- Case actions trigger agent messages
- Dispositions (RESOLVED, CLOSED) are terminal — no further actions allowed

Case Actions:
    INQUIRY ─────┬──────► INVESTIGATING ─────┬──────► RESOLVED (disposition)
                │                            │
                └───────────────────────────┴──────► CLOSED (disposition)
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from faultmaven.modules.case.domain.models import CaseStatus
from faultmaven.utils.serialization import to_json_compatible

# Allowed user-initiated case actions (via UI)
# v3: INQUIRY → RESOLVED removed. KB-driven cases route through INVESTIGATING
# via the same-turn milestone collapse path (see
# docs/architecture/investigation-engine/investigation-lifecycle-logic.md
# §1.2 INVESTIGATING → RESOLVED → KB-Resolution Path).
ALLOWED_ACTIONS = {
    CaseStatus.INQUIRY: [
        CaseStatus.INVESTIGATING,  # Phase transition: "Start investigation"
        CaseStatus.CLOSED,  # Disposition: "Close without investigating"
    ],
    CaseStatus.INVESTIGATING: [
        CaseStatus.RESOLVED,  # Disposition: "Mark as resolved"
        CaseStatus.CLOSED,  # Disposition: "Close as unresolved"
    ],
    # Dispositions — terminal, no further actions allowed
    CaseStatus.RESOLVED: [],
    CaseStatus.CLOSED: [],
}

# Backward compatibility alias
ALLOWED_TRANSITIONS = ALLOWED_ACTIONS


# Map: (old_status, new_status) → agent message
# These messages are sent to agent as if user typed them
CASE_ACTION_MESSAGES = {
    # Phase transition: INQUIRY → INVESTIGATING
    (
        CaseStatus.INQUIRY,
        CaseStatus.INVESTIGATING,
    ): "I want to start a formal investigation to find the root cause.",
    # Disposition: INQUIRY → CLOSED
    (
        CaseStatus.INQUIRY,
        CaseStatus.CLOSED,
    ): "Close this case. I don't need further investigation.",
    # Disposition: INVESTIGATING → RESOLVED
    (
        CaseStatus.INVESTIGATING,
        CaseStatus.RESOLVED,
    ): "The issue is resolved. Generate final documentation with root cause and solution.",
    # Disposition: INVESTIGATING → CLOSED
    (
        CaseStatus.INVESTIGATING,
        CaseStatus.CLOSED,
    ): "Close this case as unresolved. Summarize what we found so far.",
}

# Backward compatibility alias
STATUS_CHANGE_MESSAGES = CASE_ACTION_MESSAGES


class CaseActionManager:
    """
    Manages case actions (phase transitions and dispositions).

    Design: Case actions trigger agent messages (no special logic).
    """

    @staticmethod
    def is_terminal_state(status: CaseStatus) -> bool:
        """Check if status is a disposition (terminal, cannot be changed)."""
        return status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]

    @staticmethod
    def get_agent_message(
        old_status: CaseStatus, new_status: CaseStatus
    ) -> Optional[str]:
        """
        Get agent message for a case action.

        This message is sent to agent as if user typed it.
        """
        return CASE_ACTION_MESSAGES.get((old_status, new_status))

    @staticmethod
    def build_action_record(
        old_status: CaseStatus,
        new_status: CaseStatus,
        user_id: str,
        auto: bool = False,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build case action audit record.

        Args:
            old_status: Previous status
            new_status: New status
            user_id: User ID who initiated the action
            auto: True if system auto-triggered, False if user action
            reason: Optional reason for the action

        Returns:
            Case action record for audit trail
        """
        return {
            "from_status": old_status.value,
            "to_status": new_status.value,
            "changed_at": to_json_compatible(datetime.now(timezone.utc)),
            "changed_by": user_id,
            "auto": auto,
            "reason": reason,
        }

    # Backward compatibility alias
    build_status_change_record = build_action_record

    @staticmethod
    def get_disposition_fields(new_status: CaseStatus, user_id: str) -> Dict[str, Any]:
        """
        Get fields to update for disposition (terminal) states.

        Args:
            new_status: New disposition status
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

    # Backward compatibility alias
    get_terminal_state_fields = get_disposition_fields

    @staticmethod
    def get_allowed_actions(current_status: CaseStatus) -> list[CaseStatus]:
        """Get list of allowed case actions from current status."""
        return ALLOWED_ACTIONS.get(current_status, [])

    # Backward compatibility alias
    get_allowed_transitions = get_allowed_actions


# Backward compatibility alias
CaseStatusManager = CaseActionManager
