"""Compliance Detection for Evidence-Driven Investigation Framework.

Post-LLM processing step that detects when a user's message indicates
compliance with a previously proposed action. When compliance is detected,
the appropriate stage-gate milestones are set, triggering stage transitions.

Design Reference:
- docs/architecture/investigation-engine/investigation-lifecycle-logic.md
- docs/architecture/investigation-engine/investigation-data-models.md
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from faultmaven.modules.case.domain.models import (
    ActionAttempt,
    Case,
    InvestigationActionType,
    InvestigationStage,
    ProposedAction,
)

logger = logging.getLogger(__name__)


def detect_compliance(
    case: Case,
    user_message: str,
    llm_response: object,
) -> Optional[ActionAttempt]:
    """
    Detect if the user's message shows compliance with a pending proposed action.

    This runs AFTER the LLM processes the turn but BEFORE milestone application.
    It checks if there's a pending ProposedAction and whether the user's submission
    indicates they executed (or attempted to execute) the proposed action.

    Compliance signals:
    - User submits data/output (submission_classification = submitted_data or mixed)
    - LLM's evidence classification matches the expected action type
    - User's message references the proposed action's commands or steps

    Args:
        case: Current case state
        user_message: The user's raw message this turn
        llm_response: The structured LLM response object

    Returns:
        ActionAttempt if compliance detected, None otherwise
    """
    # Find the most recent pending action
    pending_action = _get_pending_action(case)
    if not pending_action:
        return None

    # Check if the LLM classified this as submitted data (not just conversation)
    submission_type = _get_submission_type(llm_response)
    if submission_type == "user_text":
        # Pure conversation — no compliance
        return None

    # Check for evidence that indicates compliance
    evidence_added = _get_evidence_added(llm_response)
    has_technical_data = submission_type in ("submitted_data", "mixed")

    # Determine compliance confidence
    confidence = _calculate_compliance_confidence(
        pending_action, user_message, has_technical_data, evidence_added
    )

    if confidence < 0.3:
        return None

    # Create the action attempt record
    attempt = ActionAttempt(
        action_id=pending_action.action_id,
        user_message=user_message[:10000],
        submitted_at=datetime.now(timezone.utc),
        compliance_detected=confidence >= 0.5,
        compliance_confidence=confidence,
    )

    # If compliance is detected with sufficient confidence, update stage-gate milestones
    if attempt.compliance_detected:
        _apply_stage_gate_milestone(case, pending_action)
        pending_action.status = "accepted"
        logger.info(
            f"Compliance detected for action {pending_action.action_id} "
            f"(type={pending_action.action_type.value}, confidence={confidence:.2f}). "
            f"Stage-gate milestone set."
        )
    else:
        logger.debug(
            f"Possible compliance for action {pending_action.action_id} "
            f"but confidence too low ({confidence:.2f})"
        )

    case.action_attempts.append(attempt)
    return attempt


def _get_pending_action(case: Case) -> Optional[ProposedAction]:
    """Get the most recent pending action, if any."""
    for action in reversed(case.proposed_actions):
        if action.status == "pending":
            return action
    return None


def _get_submission_type(llm_response: object) -> str:
    """Extract submission classification type from LLM response."""
    state_updates = getattr(llm_response, "state_updates", None)
    if not state_updates:
        return "user_text"

    classification = getattr(state_updates, "submission_classification", None)
    if not classification:
        return "user_text"

    return getattr(classification, "type", "user_text")


def _get_evidence_added(llm_response: object) -> list:
    """Extract evidence_to_add from LLM response."""
    state_updates = getattr(llm_response, "state_updates", None)
    if not state_updates:
        return []

    evidence = getattr(state_updates, "evidence_to_add", None)
    return evidence or []


def _calculate_compliance_confidence(
    action: ProposedAction,
    user_message: str,
    has_technical_data: bool,
    evidence_added: list,
) -> float:
    """
    Calculate confidence that the user complied with the proposed action.

    Factors:
    - Did the user submit technical data? (strong signal)
    - Does the user's message reference the proposed commands/steps?
    - Did the LLM create evidence from this submission?
    """
    confidence = 0.0
    msg_lower = user_message.lower()

    # Technical data submission is a strong compliance signal
    if has_technical_data:
        confidence += 0.4

    # Evidence was created from the submission
    if evidence_added:
        confidence += 0.2

    # User message references specific commands from the proposed action
    if action.commands:
        for cmd in action.commands:
            # Check if fragments of the command appear in user's message
            cmd_fragments = cmd.lower().split()
            matches = sum(1 for frag in cmd_fragments if frag in msg_lower)
            if matches >= min(3, len(cmd_fragments)):
                confidence += 0.3
                break

    # User message contains output-like patterns (command output, logs, metrics)
    output_patterns = ["$", ">>>", "output:", "result:", "error:", "success"]
    if any(p in msg_lower for p in output_patterns):
        confidence += 0.1

    return min(confidence, 1.0)


def _apply_stage_gate_milestone(case: Case, action: ProposedAction) -> None:
    """
    Set the appropriate stage-gate milestone based on the action type
    and current investigation stage.
    """
    current_stage = case.progress.current_stage

    if action.action_type == InvestigationActionType.MITIGATION:
        if current_stage == InvestigationStage.DIAGNOSIS:
            # User accepted mitigation → DIAGNOSIS → MITIGATION
            case.progress.mitigation_accepted = True
            logger.info(f"Set mitigation_accepted=True for case {case.case_id}")
        elif current_stage == InvestigationStage.MITIGATION:
            # User verified mitigation → MITIGATION → DIAGNOSIS (return for RCA)
            case.progress.mitigation_verified = True
            logger.info(f"Set mitigation_verified=True for case {case.case_id}")

    elif action.action_type == InvestigationActionType.SOLUTION:
        if current_stage == InvestigationStage.DIAGNOSIS:
            # User accepted solution → DIAGNOSIS → TREATMENT
            case.progress.solution_accepted = True
            logger.info(f"Set solution_accepted=True for case {case.case_id}")
        # Note: solution_verified is set via User-Agent Handshake,
        # not by compliance detection. That's handled in
        # _check_automatic_transitions via ProposedTransition.
