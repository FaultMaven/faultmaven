"""Case Action Manager — Handles case actions (phase transitions and dispositions).

Terminology (see Investigation Terminology Guide):
- Phase: Active work period (INQUIRY, INVESTIGATING)
- Disposition: Terminal resolution (RESOLVED, CLOSED)
- Case Action: Any phase transition or disposition change

Design Principle:
- Case actions are user requests to agent (not special logic)
- Case actions trigger agent messages
- Dispositions (RESOLVED, CLOSED) are terminal — no further actions allowed

User-selectable case actions (all dispositions):
    INQUIRY ─────────────────────────────────┬──────► CLOSED (disposition)
                                             │
    INVESTIGATING ─────┬──────► RESOLVED ────┘
                       │
                       └──────► CLOSED

INQUIRY → INVESTIGATING is a legal edge but NOT a user action: it is earned by
a confirmed problem statement and performed by the Gate 1 handshake. The full
legality graph is ``LEGAL_TRANSITIONS`` in ``modules/case/domain/models.py``.
"""

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from faultmaven.modules.case.domain.models import CaseState
from faultmaven.utils.serialization import to_json_compatible

#: What a user may PICK from the status menu. A strict subset of
#: ``LEGAL_TRANSITIONS`` (models.py), which is every edge the state machine
#: permits — the two are different questions and this module answers only the
#: second one.
#:
#: Every entry here is a DISPOSITION: a user decision carrying information the
#: engine cannot derive. Closing is the user's call and is always honourable;
#: "mark resolved" may be true of a fix applied outside the product entirely,
#: and where the case cannot support it ``assess_resolution_readiness`` pivots
#: to close with a readiness message rather than refusing.
#:
#: INQUIRY → INVESTIGATING is deliberately ABSENT, though it is legal. It is a
#: phase transition, not a disposition: it is earned by the case carrying a
#: confirmed problem statement, which the DB CHECK
#: ``cases_description_required_for_investigation`` makes structural. Offering
#: it in a menu promised something the engine could not honour on demand — and
#: the handler never transitioned anyway, it injected a synthetic user message
#: and fell through to the LLM. The user asks for an investigation the way the
#: design always had them ask: by saying so (see §1.2's natural flow), or by
#: the agent proposing one. Gate 1 then performs the edge.
#: Frozen for the same reason as ``LEGAL_TRANSITIONS``: a module-level dict of
#: lists is writable by any importer, and this one drives a user-facing menu.
USER_SELECTABLE_ACTIONS: Mapping[CaseState, tuple[CaseState, ...]] = MappingProxyType(
    {
        CaseState.INQUIRY: (
            CaseState.CLOSED,  # Disposition: "Close without investigating"
        ),
        CaseState.INVESTIGATING: (
            CaseState.RESOLVED,  # Disposition: "Mark as resolved"
            CaseState.CLOSED,  # Disposition: "Close as unresolved"
        ),
        # Dispositions — terminal, no further actions allowed
        CaseState.RESOLVED: (),
        CaseState.CLOSED: (),
    }
)


# Map: (old_state, new_state) → agent message
# These messages are sent to agent as if user typed them
CASE_ACTION_MESSAGES = {
    # Disposition: INQUIRY → CLOSED
    (
        CaseState.INQUIRY,
        CaseState.CLOSED,
    ): "Close this case. I don't need further investigation.",
    # Disposition: INVESTIGATING → RESOLVED
    (
        CaseState.INVESTIGATING,
        CaseState.RESOLVED,
    ): "The issue is resolved. Generate final documentation with root cause and solution.",
    # Disposition: INVESTIGATING → CLOSED
    (
        CaseState.INVESTIGATING,
        CaseState.CLOSED,
    ): "Close this case as unresolved. Summarize what we found so far.",
}

# Backward compatibility alias


class CaseActionManager:
    """
    Manages case actions (phase transitions and dispositions).

    Design: Case actions trigger agent messages (no special logic).
    """

    @staticmethod
    def is_terminal_state(state: CaseState) -> bool:
        """Check if state is a disposition (terminal, cannot be changed)."""
        return state in [CaseState.RESOLVED, CaseState.CLOSED]

    @staticmethod
    def get_agent_message(
        old_status: CaseState, new_status: CaseState
    ) -> Optional[str]:
        """
        Get agent message for a case action.

        This message is sent to agent as if user typed it.
        """
        return CASE_ACTION_MESSAGES.get((old_status, new_status))

    @staticmethod
    def build_action_record(
        old_status: CaseState,
        new_status: CaseState,
        user_id: str,
        auto: bool = False,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build case action audit record.

        Args:
            old_status: Previous state
            new_state: New state
            user_id: User ID who initiated the action
            auto: True if system auto-triggered, False if user action
            reason: Optional reason for the action

        Returns:
            Case action record for audit trail
        """
        return {
            "from_state": old_status.value,
            "to_state": new_status.value,
            "changed_at": to_json_compatible(datetime.now(timezone.utc)),
            "changed_by": user_id,
            "auto": auto,
            "reason": reason,
        }

    # Backward compatibility alias
    build_status_change_record = build_action_record

    @staticmethod
    def get_disposition_fields(new_status: CaseState, user_id: str) -> Dict[str, Any]:
        """
        Get fields to update for disposition (terminal) states.

        Args:
            new_status: New disposition state
            user_id: User ID

        Returns:
            Dictionary of fields to update
        """
        now = datetime.now(timezone.utc)

        if new_status == CaseState.RESOLVED:
            return {
                "resolved_at": now,
                "resolved_by": user_id,
            }
        elif new_status == CaseState.CLOSED:
            return {
                "closed_at": now,
                "closed_by": user_id,
            }

        return {}

    # Backward compatibility alias
    get_terminal_state_fields = get_disposition_fields

    @staticmethod
    def get_allowed_actions(current_status: CaseState) -> list[CaseState]:
        """The case actions a user may pick from the status menu.

        Selectability, not legality — see ``USER_SELECTABLE_ACTIONS``.
        """
        return list(USER_SELECTABLE_ACTIONS.get(current_status, ()))

    # Backward compatibility alias
    get_allowed_transitions = get_allowed_actions


# Backward compatibility alias
