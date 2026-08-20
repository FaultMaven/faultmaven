"""Turn Outcome Determination

Classifies turn outcome for observability and metrics tracking.

Reference: investigation-lifecycle-logic.md Section 3.2 (lines 822-852)
"""

import logging
from typing import List

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
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

    # Agent requested data — keyed to a NEW outstanding evidence need, not to
    # the prose that described it (see _new_data_request_raised).
    if _new_data_request_raised(case):
        logger.debug("Turn outcome: DATA_REQUESTED")
        return TurnOutcome.DATA_REQUESTED

    # Conversation only (no measurable progress)
    logger.debug("Turn outcome: CONVERSATION (no measurable progress)")
    return TurnOutcome.CONVERSATION


def _new_data_request_raised(case: Case) -> bool:
    """Whether a **new** outstanding data ask was raised this turn.

    ``DATA_REQUESTED`` is one of the arms ``_check_if_progress_made`` treats as
    progress, so every false positive here resets ``turns_without_progress`` and
    disarms the stall net. The former implementation (#1136) produced them two
    ways at once, and both fired on turns where nothing was asked:

    - it read ``case.turn_history[-1]``, which at call time is the **previous**
      turn's record — the current turn is appended much later in ``process_turn``
      — so an ask made on turn N-1 scored turn N as a data request; and
    - its keyword list contained a bare ``"?"``, so any question-shaped reply
      matched ("Is that consistent with what you are seeing?").

    Both are the measurement-by-token-collision class: the outcome was inferred
    from prose rather than from what the turn actually did. The demand side is
    already first-class — an ask the engine intends to track is an
    ``EvidenceNeed`` — so this reads that artifact instead.

    NEW is the load-bearing word, and it is what keeps this from over-correcting
    into the opposite failure (#1136 trap 1: an impatient user must not be pushed
    toward the insufficient-evidence ramp). Raising a genuinely new ask is
    investigative progress and resets the counter; repeating a standing ask is
    not. The need layer already draws that line for us — a re-ask matches an
    outstanding need and is recorded as a second mention rather than minting a
    new row (``evidence_need_linking.link_evidence_suggestions_to_needs``), so a
    case that keeps asking for the same thing now reads as the stall it is.

    SCOPE: this runs inside ``_apply_investigation_updates``, so it sees needs the
    model **declared** (``_apply_evidence_need_updates``) but not the engine's
    backfill of asks that appeared only in an EVIDENCE suggestion — that sweep
    runs later in ``process_turn``, after the counter has been updated. The gap is
    small (14 of 446 needs on the reference corpus are ``engine_inferred``) and it
    errs toward *under*-reporting progress, which is the conservative direction
    here: it can only make the net arm sooner, and arming still requires the turn
    floor, five consecutive such turns, and the work gate.
    """
    return any(
        need.created_at_turn == case.current_turn and need.is_outstanding
        for need in case.evidence_needs
    )


def _milestone_completed_this_turn(case: Case) -> bool:
    """Check if any milestone was completed in the current turn."""
    if not case.turn_history:
        return False

    last_turn = case.turn_history[-1]
    return len(last_turn.milestones_completed) > 0
