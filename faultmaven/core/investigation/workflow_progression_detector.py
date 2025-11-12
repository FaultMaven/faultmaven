"""Workflow Progression Detector (v3.0)

Detects when agent should suggest progressing to next phase in investigation workflow.

PURPOSE: Agent is ready to move forward → detects trigger conditions → seeks user buy-in
vs. User manually changes status via UI (different flow)

Three scenarios:
1. Start Investigation (CONSULTING → INVESTIGATING)
2. Mark Complete (INVESTIGATING → RESOLVED)
3. Suggest Escalation (INVESTIGATING → CLOSED)
"""

from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    DegradedModeType,
)


def should_suggest_start_investigation(
    investigation_state: InvestigationState,
    conversation_turn: int,
) -> tuple[bool, List[str]]:
    """Detect if agent should suggest starting systematic investigation

    Triggers when problem is complex enough to need structured approach.

    Args:
        investigation_state: Current investigation state
        conversation_turn: Current turn number

    Returns:
        Tuple of (should_suggest, complexity_indicators)
        complexity_indicators: List of reasons why systematic approach needed
    """
    indicators = []

    # Indicator 1: Multi-turn conversation without resolution
    if conversation_turn >= 5:
        indicators.append("multi_turn_conversation")

    # Indicator 2: Multiple symptoms detected
    if hasattr(investigation_state, 'ooda_engine'):
        symptoms = getattr(investigation_state.ooda_engine, 'symptoms', [])
        if len(symptoms) >= 3:
            indicators.append("multiple_symptoms")

    # Indicator 3: Scope still unclear after several turns
    if conversation_turn >= 4 and not investigation_state.ooda_engine.anomaly_frame:
        indicators.append("unclear_scope")

    # Indicator 4: User explicitly requested investigation
    # (This would be set by intake_handler when detecting user intent)
    if getattr(investigation_state.lifecycle, 'user_requested_investigation', False):
        indicators.append("user_expressed_need")

    # Indicator 5: Problem confirmation exists but investigation not started
    if (
        investigation_state.problem_confirmation
        and investigation_state.lifecycle.current_phase == InvestigationPhase.INTAKE
        and conversation_turn >= 3
    ):
        indicators.append("complexity_detected")

    # Suggest if we have 2+ indicators
    should_suggest = len(indicators) >= 2

    return should_suggest, indicators


def should_suggest_mark_complete(
    investigation_state: InvestigationState,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Detect if agent should suggest marking investigation complete

    Triggers when investigation successfully completed with validated root cause.

    Args:
        investigation_state: Current investigation state

    Returns:
        Tuple of (should_suggest, completion_details)
        completion_details: Dict with root_cause, solution, verification, etc.
    """
    # Must be in Phase 5 (Solution) - NOT Phase 6
    # Phase 6 is for documentation after case is already RESOLVED
    if investigation_state.lifecycle.current_phase != InvestigationPhase.SOLUTION:
        return False, None

    # Check working conclusion
    wc = investigation_state.lifecycle.working_conclusion
    if not wc:
        return False, None

    # Must have ≥70% confidence (validated)
    if wc.confidence < 0.70:
        return False, None

    # Check if can proceed with solution
    if not wc.can_proceed_with_solution:
        return False, None

    # Check solution verification (this happens in Phase 5)
    solution_verified = getattr(
        investigation_state.lifecycle,
        'solution_verified',
        False
    )
    if not solution_verified:
        return False, None

    # Get validated hypothesis for root cause
    validated_hypothesis = None
    for hyp in investigation_state.ooda_engine.hypotheses:
        if hyp.status.value == "validated" and hyp.likelihood >= 0.70:
            validated_hypothesis = hyp
            break

    if not validated_hypothesis:
        return False, None

    # All conditions met - suggest marking investigation complete
    # This triggers INVESTIGATING → RESOLVED transition
    completion_details = {
        "root_cause": validated_hypothesis.statement,
        "solution_summary": getattr(
            investigation_state.lifecycle,
            'solution_summary',
            "Solution applied and verified"
        ),
        "verification_details": getattr(
            investigation_state.lifecycle,
            'verification_details',
            "Solution verified successfully"
        ),
        "confidence_level": wc.confidence,
    }

    return True, completion_details


def should_suggest_escalation(
    investigation_state: InvestigationState,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Detect if agent should suggest escalation/closure due to limitations

    Triggers when investigation is blocked and cannot proceed.

    Args:
        investigation_state: Current investigation state

    Returns:
        Tuple of (should_suggest, escalation_details)
        escalation_details: Dict with limitation info, findings, recommendations
    """
    escalation_state = investigation_state.lifecycle.escalation_state

    # Condition 1: Degraded mode with 0% confidence cap (hypothesis space exhausted)
    if escalation_state.operating_in_degraded_mode:
        if escalation_state.degraded_mode_type == DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED:
            return True, {
                "limitation_type": "Hypothesis Space Exhausted",
                "limitation_explanation": escalation_state.degraded_mode_explanation or "All reasonable hypotheses have been tested without finding root cause",
                "findings_summary": _get_findings_summary(investigation_state),
                "confidence_level": investigation_state.lifecycle.working_conclusion.confidence if investigation_state.lifecycle.working_conclusion else 0.0,
                "next_steps_recommendations": [
                    "Escalate to senior engineer or specialist",
                    "Request access to additional diagnostic tools",
                    "Consider bringing in vendor support",
                ],
            }

        # Condition 2: In degraded mode for 6+ turns without progress
        if escalation_state.entered_at_turn:
            current_turn = investigation_state.metadata.current_turn
            turns_in_degraded = current_turn - escalation_state.entered_at_turn
            if turns_in_degraded >= 6:
                return True, {
                    "limitation_type": escalation_state.degraded_mode_type.value,
                    "limitation_explanation": escalation_state.degraded_mode_explanation or "Investigation has been operating with limitations for extended period",
                    "findings_summary": _get_findings_summary(investigation_state),
                    "confidence_level": investigation_state.lifecycle.working_conclusion.confidence if investigation_state.lifecycle.working_conclusion else 0.0,
                    "next_steps_recommendations": _get_escalation_recommendations(escalation_state.degraded_mode_type),
                }

    # Condition 3: Max loop-backs reached
    if investigation_state.lifecycle.loop_back_count >= 3:
        return True, {
            "limitation_type": "Maximum Loop-Backs Reached",
            "limitation_explanation": f"Investigation has looped back {investigation_state.lifecycle.loop_back_count} times, indicating fundamental issue with hypothesis or approach",
            "findings_summary": _get_findings_summary(investigation_state),
            "confidence_level": investigation_state.lifecycle.working_conclusion.confidence if investigation_state.lifecycle.working_conclusion else 0.0,
            "next_steps_recommendations": [
                "Re-frame the problem from a different perspective",
                "Escalate to get fresh eyes on the issue",
                "Consider whether this is actually multiple separate issues",
            ],
        }

    return False, None


def _get_findings_summary(investigation_state: InvestigationState) -> str:
    """Get summary of findings so far"""
    wc = investigation_state.lifecycle.working_conclusion
    if wc:
        return wc.statement

    # Fallback: summarize hypotheses
    hypotheses = investigation_state.ooda_engine.hypotheses
    if hypotheses:
        active_hyps = [h for h in hypotheses if h.status.value in ["active", "testing"]]
        if active_hyps:
            return f"Investigating {len(active_hyps)} active hypotheses: " + "; ".join(
                h.statement[:50] for h in active_hyps[:2]
            )

    return "Investigation in progress without validated findings"


def _get_escalation_recommendations(degraded_mode_type: DegradedModeType) -> List[str]:
    """Get escalation recommendations based on limitation type"""
    recommendations_map = {
        DegradedModeType.CRITICAL_EVIDENCE_MISSING: [
            "Request access to missing logs/metrics",
            "Escalate to team with access to required evidence",
            "Consider alternative diagnostic approaches",
        ],
        DegradedModeType.EXPERTISE_REQUIRED: [
            "Escalate to specialist with required expertise",
            "Request consultation from expert in this domain",
            "Document findings for specialist review",
        ],
        DegradedModeType.SYSTEMIC_ISSUE: [
            "Escalate to architecture/platform team",
            "Consider systemic redesign or refactoring",
            "Document issue for long-term planning",
        ],
        DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED: [
            "Escalate to senior engineer for fresh perspective",
            "Request peer review of investigation approach",
            "Consider reframing the problem definition",
        ],
        DegradedModeType.GENERAL_LIMITATION: [
            "Escalate for additional resources or tools",
            "Request guidance on how to proceed",
            "Document limitations for future reference",
        ],
    }

    return recommendations_map.get(
        degraded_mode_type,
        ["Escalate to senior engineer", "Request additional resources"]
    )
