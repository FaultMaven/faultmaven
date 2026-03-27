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

from datetime import UTC, datetime
from typing import Any

from faultmaven.modules.case.domain.models import CaseStatus, KnowledgeResolution
from faultmaven.utils.serialization import to_json_compatible

# Allowed user-initiated case actions (via UI)
# Note: INQUIRY → RESOLVED is handled automatically by agent (fast-track), not user-selectable
ALLOWED_ACTIONS = {
    CaseStatus.INQUIRY: [
        CaseStatus.INVESTIGATING,  # Phase transition: "Start investigation"
        CaseStatus.CLOSED,  # Disposition: "Close without investigating"
        # RESOLVED excluded - fast-track is agent-controlled only
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
    # Disposition: INQUIRY → RESOLVED (Fast-Track)
    (
        CaseStatus.INQUIRY,
        CaseStatus.RESOLVED,
    ): "The issue was resolved using a known solution from the knowledge base.",
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
    def validate_action(
        old_status: CaseStatus, new_status: CaseStatus
    ) -> tuple[bool, str | None]:
        """
        Validate a case action.

        Returns:
            (is_valid, error_message)
        """
        # Cannot change dispositions
        if CaseActionManager.is_terminal_state(old_status):
            return (
                False,
                f"Cannot change status from disposition {old_status.value}. "
                f"To reopen, create a new case.",
            )

        # Check if action is allowed
        allowed = ALLOWED_ACTIONS.get(old_status, [])
        if new_status not in allowed:
            return (
                False,
                f"Invalid case action: {old_status.value} → {new_status.value}. "
                f"Allowed actions: {[s.value for s in allowed]}",
            )

        return (True, None)

    # Backward compatibility alias
    validate_transition = validate_action

    @staticmethod
    def get_agent_message(old_status: CaseStatus, new_status: CaseStatus) -> str | None:
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
        reason: str | None = None,
    ) -> dict[str, Any]:
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
            "changed_at": to_json_compatible(datetime.now(UTC)),
            "changed_by": user_id,
            "auto": auto,
            "reason": reason,
        }

    # Backward compatibility alias
    build_status_change_record = build_action_record

    @staticmethod
    def get_disposition_fields(new_status: CaseStatus, user_id: str) -> dict[str, Any]:
        """
        Get fields to update for disposition (terminal) states.

        Args:
            new_status: New disposition status
            user_id: User ID

        Returns:
            Dictionary of fields to update
        """
        now = datetime.now(UTC)

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

    @staticmethod
    def build_fast_track_record(
        knowledge_resolution: KnowledgeResolution,
        user_id: str,
    ) -> dict[str, Any]:
        """Build audit record for Fast-Track resolution (disposition from INQUIRY)."""
        now = datetime.now(UTC)
        return {
            "from_status": CaseStatus.INQUIRY.value,
            "to_status": CaseStatus.RESOLVED.value,
            "changed_at": to_json_compatible(now),
            "changed_by": user_id,
            "auto": True,
            "reason": f"Knowledge Base Resolution: {knowledge_resolution.match_type} ({knowledge_resolution.match_id})",
            "resolution_details": knowledge_resolution.model_dump(),
        }


# Backward compatibility alias
CaseStatusManager = CaseActionManager
