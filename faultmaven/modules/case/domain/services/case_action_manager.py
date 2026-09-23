"""Case Action Manager — Handles case actions (phase transitions and dispositions).

Terminology (see Investigation Terminology Guide):
- Phase: Active work period (INQUIRY, INVESTIGATING)
- Disposition: Terminal resolution (RESOLVED, CLOSED)
- Case Action: Any phase transition or disposition change

Design Principle:
- Dispositions (RESOLVED, CLOSED) are terminal — no further actions allowed
- The menu carries only the UNCONDITIONAL decision; everything conditional is
  earned from case content and offered by the engine through a handshake

User-selectable case actions:
    INQUIRY ───────────────► CLOSED
    INVESTIGATING ─────────► CLOSED

Two legal edges are deliberately absent, for one reason: a menu cannot honour
an edge whose precondition is a fact about the case.

- INQUIRY → INVESTIGATING is earned by a confirmed problem statement and
  performed by the Gate 1 handshake (#1608).
- INVESTIGATING → RESOLVED is earned by a confirmed root-cause elimination —
  a qualifying ``causal_absence_evidence`` row — and offered by the engine
  when it sees the case reach it (INV-43), or when the user says so in
  conversation. The readiness check that used to run AFTER the user picked
  "Mark as resolved" now decides whether the offer is made at all.

CLOSED stays selectable from both phases because closing is the one decision
that needs no precondition: it is always honourable, and the user is the only
one who can make it.

The full legality graph is ``LEGAL_TRANSITIONS`` in
``modules/case/domain/models.py``.
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
#: The one entry per phase is CLOSED, and that is the whole rule: a menu may
#: offer only what needs no precondition. Closing is always honourable — the
#: user is the only one who can decide to stop, and no case content can make
#: that decision wrong.
#:
#: TWO legal edges are deliberately ABSENT, for the same reason.
#:
#: INQUIRY → INVESTIGATING (#1608) is earned by a confirmed problem statement,
#: which the DB CHECK ``cases_description_required_for_investigation`` makes
#: structural. Offering it promised something the engine could not honour on
#: demand — and the handler never transitioned anyway, it injected a synthetic
#: user message and fell through to the LLM. Gate 1 performs the edge.
#:
#: INVESTIGATING → RESOLVED is earned by a qualifying ``causal_absence_evidence``
#: row — the cause confirmed eliminated, ``assess_resolution_readiness`` READY.
#: It was listed here until the engine learned to see that bar for itself
#: (INV-43), and the listing was never the gate people took it for: this dict
#: is consulted with no case content whatsoever, so ``valid_next_states``
#: advertised ``resolved`` on EVERY investigating case, including ones the
#: readiness gate would have refused. What made the menu look gated was one
#: client reconciling it against ``disposition_eligibility`` — a convention, not
#: a rule, and one the legacy fallback did not follow. The check now decides
#: whether the offer is made at all rather than arguing with a pick already
#: made, which also retires the branch that could confirm a ``needs_info``
#: proposal without re-reading readiness.
#:
#: A user who believes the case is resolved says so; the engine checks, then
#: either proposes the handshake or asks for what is missing. Exactly the shape
#: Gate 1 already had.
#:
#: Frozen for the same reason as ``LEGAL_TRANSITIONS``: a module-level dict of
#: lists is writable by any importer, and this one drives a user-facing menu.
USER_SELECTABLE_ACTIONS: Mapping[CaseState, tuple[CaseState, ...]] = MappingProxyType(
    {
        CaseState.INQUIRY: (
            CaseState.CLOSED,  # Disposition: "Close without investigating"
        ),
        CaseState.INVESTIGATING: (
            CaseState.CLOSED,  # Disposition: "Close as unresolved"
        ),
        # Dispositions — terminal, no further actions allowed
        CaseState.RESOLVED: (),
        CaseState.CLOSED: (),
    }
)


#: Why each earned edge is not requestable, for the refusal message. Keyed by
#: the TARGET state, and it decides only the WORDING — whether a state is
#: refused at all is derived from ``USER_SELECTABLE_ACTIONS`` below, so a state
#: absent from this dict is still refused, just less helpfully.
_EARNED_EDGE_REASON: Mapping[CaseState, str] = MappingProxyType(
    {
        CaseState.INVESTIGATING: (
            "reached by confirming the problem statement (Gate 1), not by "
            "requesting the state"
        ),
        CaseState.RESOLVED: (
            "reached by confirming the resolution the agent proposes once the "
            "root cause is confirmed eliminated, not by requesting the state. "
            "Tell the agent the issue is resolved and it will check, then "
            "either propose the transition or ask for what is missing"
        ),
    }
)


def earned_edge_refusal(from_state: CaseState, to_state: str) -> Optional[str]:
    """The message refusing a ``status_transition`` a user may not pick, or None.

    DERIVED from ``USER_SELECTABLE_ACTIONS``, which is the point. This rule was
    hand-enumerated at four sites — two in the engine, two at the service
    boundary — each restating "this state is not selectable" as a literal
    comparison against a ``CaseState`` member. The dict and the refusals were
    then independent facts that could disagree: re-adding RESOLVED to the menu
    left every refusal in place, and removing a refusal left the menu alone.
    Neither direction failed a test, because nothing connected them.

    One derivation means one edit. A third removal needs no new guard, and a
    re-addition stops being refused — which is what makes the refusal tests the
    guard on the dict that they were previously only assumed to be.

    Terminal states are NOT refused here even though they select nothing: a
    terminal case is short-circuited to Q&A upstream (INV-09/INV-10), and that
    routing owns the answer. Returning a refusal would change it.
    """
    if from_state in (CaseState.RESOLVED, CaseState.CLOSED):
        return None
    if to_state in {s.value for s in USER_SELECTABLE_ACTIONS.get(from_state, ())}:
        return None
    try:
        target = CaseState(to_state)
    except ValueError:
        return f"{to_state!r} is not a case state."
    reason = _EARNED_EDGE_REASON.get(
        target, "not something a user selects; the engine performs it"
    )
    return (
        f"{target.value.upper()} is not a user-selectable case action. It is {reason}."
    )


class CaseActionManager:
    """
    Manages case actions (phase transitions and dispositions).

    Design: a case action is a user request the engine answers with a
    proposal, never a command it executes. The synthetic
    ``CASE_ACTION_MESSAGES`` a pick used to be turned into went with the
    resolve branch that was its only reader — a disposition the user picks
    says what they want, and the engine composes the reply itself.
    """

    @staticmethod
    def is_terminal_state(state: CaseState) -> bool:
        """Check if state is a disposition (terminal, cannot be changed)."""
        return state in [CaseState.RESOLVED, CaseState.CLOSED]

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
