"""Turn Outcome Determination

Classifies turn outcome for observability and metrics tracking.

Reference: investigation-lifecycle-logic.md Section 3.2 (lines 822-852)
"""

import logging
from typing import List

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    HypothesisState,
    TurnOutcome,
)

logger = logging.getLogger(__name__)


def determine_turn_outcome(
    case: Case,
    progress_made: bool,
    milestones_completed: List[str],
    evidence_added: List[str],
    hypotheses_generated: int,
    solutions_proposed: int,
) -> TurnOutcome:
    """
    Determine turn outcome classification.

    Checked AFTER milestone detection and evidence processing.
    Used for LLM observability and metrics (not workflow control).

    Args:
        case: Current case state
        progress_made: Whether progress was made this turn
        milestones_completed: List of milestone names completed
        evidence_added: List of evidence IDs added
        hypotheses_generated: Count of hypotheses generated
        solutions_proposed: Count of solutions proposed

    Returns:
        TurnOutcome enum value

    Reference: investigation-lifecycle-logic.md lines 822-852
    """

    # Terminal transition
    if case.is_terminal:
        outcome = (
            TurnOutcome.CASE_RESOLVED
            if case.state == CaseState.RESOLVED
            else TurnOutcome.CONVERSATION
        )
        logger.debug(f"Turn outcome: {outcome} (terminal state: {case.state})")
        return outcome

    # Milestone completed
    if milestones_completed:
        logger.debug(
            f"Turn outcome: MILESTONE_COMPLETED (milestones: {milestones_completed})"
        )
        return TurnOutcome.MILESTONE_COMPLETED

    # Hypothesis tested/validated
    if any(h.tested_at == case.current_turn for h in case.hypotheses.values()):
        logger.debug("Turn outcome: HYPOTHESIS_TESTED")
        return TurnOutcome.HYPOTHESIS_TESTED

    # Evidence provided
    if evidence_added:
        logger.debug(f"Turn outcome: DATA_PROVIDED (evidence: {evidence_added})")
        return TurnOutcome.DATA_PROVIDED

    # Agent requested data
    # Check if agent's last response contains data requests
    if _agent_requested_data_this_turn(case):
        logger.debug("Turn outcome: DATA_REQUESTED")
        return TurnOutcome.DATA_REQUESTED

    # Conversation only (no measurable progress)
    logger.debug("Turn outcome: CONVERSATION (no measurable progress)")
    return TurnOutcome.CONVERSATION


def _agent_requested_data_this_turn(case: Case) -> bool:
    """
    Check if agent requested data from user this turn.

    Implementation: Check last turn for data request indicators.
    """
    if not case.turn_history:
        return False

    last_turn = case.turn_history[-1]
    # TurnProgress stores summary, not full response
    agent_response = (
        last_turn.agent_response_summary.lower()
        if last_turn.agent_response_summary
        else ""
    )

    # Data request indicators
    request_keywords = [
        "can you share",
        "could you provide",
        "please provide",
        "can you check",
        "could you check",
        "what do the logs",
        "what does",
        "show me",
        "upload",
        "attach",
        "?",  # Questions are often data requests
    ]

    return any(keyword in agent_response for keyword in request_keywords)


def _milestone_completed_this_turn(case: Case) -> bool:
    """Check if any milestone was completed in the current turn."""
    if not case.turn_history:
        return False

    last_turn = case.turn_history[-1]
    return len(last_turn.milestones_completed) > 0
