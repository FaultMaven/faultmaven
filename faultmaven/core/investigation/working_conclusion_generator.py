"""Working Conclusion and Progress Metrics Generator (v3.0)

Generates agent's current best understanding and tracks investigation progress.
Replaces stall detection system with continuous progress measurement.

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md (v3.0)
- docs/architecture/prompt-engineering-architecture.md (v3.0)
"""

from typing import List, Optional, Tuple
from faultmaven.models.investigation import (
    InvestigationState,
    WorkingConclusion,
    ProgressMetrics,
    ConfidenceLevel,
    InvestigationMomentum,
    Hypothesis,
    HypothesisStatus,
    InvestigationPhase,
)


def generate_working_conclusion(
    investigation_state: InvestigationState,
    current_turn: int,
) -> WorkingConclusion:
    """Generate working conclusion based on current investigation state

    Called EVERY turn to maintain agent's current best understanding.

    Args:
        investigation_state: Current investigation state
        current_turn: Current conversation turn

    Returns:
        WorkingConclusion with confidence level and evidence basis
    """
    # Get highest confidence hypothesis
    hypotheses = investigation_state.ooda_engine.hypotheses
    if not hypotheses:
        return _create_early_phase_conclusion(investigation_state, current_turn)

    # Find active or validated hypothesis with highest confidence
    active_hypotheses = [
        h for h in hypotheses
        if h.status in [HypothesisStatus.ACTIVE, HypothesisStatus.VALIDATED]
    ]

    if not active_hypotheses:
        # All refuted or retired
        return _create_refuted_conclusion(investigation_state, current_turn, hypotheses)

    # Get hypothesis with highest confidence
    best_hypothesis = max(active_hypotheses, key=lambda h: h.likelihood)

    # Calculate evidence completeness for this hypothesis
    evidence_completeness = _calculate_hypothesis_evidence_completeness(best_hypothesis)

    # Count supporting evidence
    supporting_count = len(best_hypothesis.supporting_evidence)
    total_evidence = len(investigation_state.evidence.evidence_provided)

    # Determine confidence level
    confidence = best_hypothesis.likelihood
    confidence_level = _get_confidence_level_from_value(confidence)

    # Generate caveats based on evidence state
    caveats = _generate_caveats(
        best_hypothesis,
        evidence_completeness,
        investigation_state,
    )

    # Get alternative explanations (other active hypotheses)
    alternatives = [
        h.statement
        for h in active_hypotheses
        if h.hypothesis_id != best_hypothesis.hypothesis_id and h.likelihood >= 0.30
    ][:3]  # Top 3 alternatives

    # Determine next evidence needed
    next_evidence = _determine_next_evidence_needed(best_hypothesis, investigation_state)

    # Check if can proceed with solution (≥70% confidence)
    can_proceed = confidence >= 0.70

    # Get last confidence change turn
    last_change_turn = _find_last_confidence_change_turn(investigation_state)

    return WorkingConclusion(
        statement=best_hypothesis.statement,
        confidence=confidence,
        confidence_level=confidence_level,
        supporting_evidence_count=supporting_count,
        total_evidence_count=total_evidence,
        evidence_completeness=evidence_completeness,
        caveats=caveats,
        alternative_explanations=alternatives,
        can_proceed_with_solution=can_proceed,
        next_evidence_needed=next_evidence,
        last_updated_turn=current_turn,
        last_confidence_change_turn=last_change_turn,
        generated_at_turn=current_turn,
    )


def calculate_progress_metrics(
    investigation_state: InvestigationState,
    current_turn: int,
) -> ProgressMetrics:
    """Calculate investigation progress metrics

    Replaces binary "stalled/not-stalled" with continuous measurement.

    Args:
        investigation_state: Current investigation state
        current_turn: Current conversation turn

    Returns:
        ProgressMetrics with momentum and next steps
    """
    hypotheses = investigation_state.ooda_engine.hypotheses
    evidence_provided = investigation_state.evidence.evidence_provided
    evidence_blocked = investigation_state.evidence.critical_evidence_blocked

    # Calculate overall evidence completeness across all hypotheses
    evidence_completeness = _calculate_overall_evidence_completeness(
        hypotheses, evidence_provided
    )

    # Count evidence states
    evidence_blocked_count = len(evidence_blocked)
    evidence_pending_count = len(investigation_state.evidence.evidence_requests) - len(evidence_provided)

    # Determine investigation momentum
    momentum = _determine_investigation_momentum(investigation_state, current_turn)

    # Calculate turns since last progress
    turns_since_progress = _calculate_turns_since_progress(investigation_state, current_turn)

    # Count active hypotheses
    active_hypotheses = [
        h for h in hypotheses
        if h.status in [HypothesisStatus.ACTIVE, HypothesisStatus.VALIDATED]
    ]
    active_count = len(active_hypotheses)

    # Count hypotheses with sufficient evidence (≥70%)
    sufficient_evidence_count = sum(
        1 for h in active_hypotheses
        if _calculate_hypothesis_evidence_completeness(h) >= 0.70
    )

    # Get highest hypothesis confidence
    highest_confidence = max(
        (h.likelihood for h in active_hypotheses),
        default=0.0
    )

    # Generate next steps
    next_steps = _generate_next_steps(investigation_state, momentum, evidence_completeness)

    # Generate blocked reasons if momentum low
    blocked_reasons = []
    if momentum in [InvestigationMomentum.LOW, InvestigationMomentum.BLOCKED]:
        blocked_reasons = _generate_blocked_reasons(
            investigation_state,
            evidence_blocked_count,
            evidence_completeness,
            active_count,
        )

    return ProgressMetrics(
        evidence_completeness=evidence_completeness,
        evidence_blocked_count=evidence_blocked_count,
        evidence_pending_count=evidence_pending_count,
        investigation_momentum=momentum,
        turns_since_last_progress=turns_since_progress,
        active_hypotheses_count=active_count,
        hypotheses_with_sufficient_evidence=sufficient_evidence_count,
        highest_hypothesis_confidence=highest_confidence,
        next_steps=next_steps,
        blocked_reasons=blocked_reasons,
    )


# =============================================================================
# Helper Functions
# =============================================================================


def _get_confidence_level_from_value(confidence: float) -> ConfidenceLevel:
    """Map confidence value to human-readable level"""
    if confidence >= 0.90:
        return ConfidenceLevel.VERIFIED
    elif confidence >= 0.70:
        return ConfidenceLevel.CONFIDENT
    elif confidence >= 0.50:
        return ConfidenceLevel.PROBABLE
    else:
        return ConfidenceLevel.SPECULATION


def _calculate_hypothesis_evidence_completeness(hypothesis: Hypothesis) -> float:
    """Calculate evidence completeness for single hypothesis"""
    if not hypothesis.required_evidence:
        return 1.0  # No evidence required = complete

    obtained = len(hypothesis.supporting_evidence)
    required = len(hypothesis.required_evidence)

    return min(obtained / required, 1.0) if required > 0 else 0.0


def _calculate_overall_evidence_completeness(
    hypotheses: List[Hypothesis],
    evidence_provided: List[str],
) -> float:
    """Calculate average evidence completeness across all active hypotheses"""
    if not hypotheses:
        return 0.0

    active_hypotheses = [
        h for h in hypotheses
        if h.status in [HypothesisStatus.ACTIVE, HypothesisStatus.VALIDATED]
    ]

    if not active_hypotheses:
        return 0.0

    completeness_scores = [
        _calculate_hypothesis_evidence_completeness(h)
        for h in active_hypotheses
    ]

    return sum(completeness_scores) / len(completeness_scores)


def _determine_investigation_momentum(
    investigation_state: InvestigationState,
    current_turn: int,
) -> InvestigationMomentum:
    """Determine investigation momentum based on recent progress

    Momentum levels:
    - HIGH: Evidence flowing, confidence increasing
    - MODERATE: Some progress, steady state
    - LOW: Little progress, confidence plateaued
    - BLOCKED: Critical evidence unavailable
    """
    # Check for blocked state (critical evidence missing)
    blocked_count = len(investigation_state.evidence.critical_evidence_blocked)
    if blocked_count >= 2:
        return InvestigationMomentum.BLOCKED

    # Get confidence trajectory (last 3 turns)
    trajectory = investigation_state.ooda_engine.confidence_trajectory[-3:]
    if len(trajectory) < 2:
        return InvestigationMomentum.MODERATE  # Not enough data

    # Check if confidence is increasing
    recent_delta = trajectory[-1] - trajectory[0]

    # Check evidence collection rate
    iterations = investigation_state.ooda_engine.iterations[-3:]
    evidence_collected_recently = sum(
        it.new_evidence_collected for it in iterations
    )

    # Determine momentum
    if recent_delta > 0.10 and evidence_collected_recently >= 2:
        return InvestigationMomentum.HIGH
    elif recent_delta < -0.05 and evidence_collected_recently == 0:
        return InvestigationMomentum.LOW
    elif abs(recent_delta) < 0.05 and evidence_collected_recently == 0:
        return InvestigationMomentum.LOW
    else:
        return InvestigationMomentum.MODERATE


def _calculate_turns_since_progress(
    investigation_state: InvestigationState,
    current_turn: int,
) -> int:
    """Calculate turns since last meaningful progress

    Progress = evidence added OR confidence changed (>5%)
    """
    last_progress_turn = 0

    # Check confidence trajectory for changes
    trajectory = investigation_state.ooda_engine.confidence_trajectory
    for i in range(len(trajectory) - 1, 0, -1):
        if abs(trajectory[i] - trajectory[i-1]) > 0.05:
            last_progress_turn = max(last_progress_turn, current_turn - (len(trajectory) - i))
            break

    # Check iterations for evidence collection
    for iteration in reversed(investigation_state.ooda_engine.iterations):
        if iteration.new_evidence_collected > 0:
            turns_back = current_turn - iteration.completed_at_turn if iteration.completed_at_turn else 0
            last_progress_turn = max(last_progress_turn, current_turn - turns_back)
            break

    return current_turn - last_progress_turn if last_progress_turn > 0 else 0


def _find_last_confidence_change_turn(investigation_state: InvestigationState) -> int:
    """Find turn when confidence last changed significantly (>5%)"""
    trajectory = investigation_state.ooda_engine.confidence_trajectory
    current_turn = investigation_state.metadata.current_turn

    for i in range(len(trajectory) - 1, 0, -1):
        if abs(trajectory[i] - trajectory[i-1]) > 0.05:
            return current_turn - (len(trajectory) - i)

    return current_turn  # No change found, return current


def _generate_caveats(
    hypothesis: Hypothesis,
    evidence_completeness: float,
    investigation_state: InvestigationState,
) -> List[str]:
    """Generate caveats based on evidence state and confidence"""
    caveats = []

    # Evidence completeness caveats
    if evidence_completeness < 0.50:
        caveats.append(
            f"Only {evidence_completeness*100:.0f}% of required evidence collected"
        )
    elif evidence_completeness < 0.70:
        caveats.append(
            f"Evidence partially complete ({evidence_completeness*100:.0f}%)"
        )

    # Confidence level caveats
    if hypothesis.likelihood < 0.50:
        caveats.append("Low confidence - this is speculative")
    elif hypothesis.likelihood < 0.70:
        caveats.append("Moderate confidence - not yet validated")

    # Blocked evidence caveats
    blocked = investigation_state.evidence.critical_evidence_blocked
    if blocked:
        caveats.append(f"{len(blocked)} critical evidence requests blocked")

    # Refuting evidence caveats
    if hypothesis.refuting_evidence:
        caveats.append(
            f"{len(hypothesis.refuting_evidence)} evidence items contradict this hypothesis"
        )

    return caveats


def _determine_next_evidence_needed(
    hypothesis: Hypothesis,
    investigation_state: InvestigationState,
) -> List[str]:
    """Determine what evidence is needed next"""
    # Get missing required evidence
    obtained = set(hypothesis.supporting_evidence)
    required = set(hypothesis.required_evidence)
    missing = required - obtained

    # Prioritize critical evidence
    # (In real implementation, would check evidence priority from hypothesis data)
    return list(missing)[:3]  # Top 3 missing items


def _generate_next_steps(
    investigation_state: InvestigationState,
    momentum: InvestigationMomentum,
    evidence_completeness: float,
) -> List[str]:
    """Generate next steps based on investigation state"""
    steps = []
    phase = investigation_state.lifecycle.current_phase

    if phase == InvestigationPhase.INTAKE:
        steps.append("Obtain user consent to begin investigation")
    elif phase == InvestigationPhase.BLAST_RADIUS:
        steps.append("Assess impact scope and affected systems")
    elif phase == InvestigationPhase.TIMELINE:
        steps.append("Establish when symptoms first appeared")
    elif phase == InvestigationPhase.HYPOTHESIS:
        steps.append("Generate systematic hypotheses for root cause")
    elif phase == InvestigationPhase.VALIDATION:
        if momentum == InvestigationMomentum.BLOCKED:
            steps.append("Address blocked evidence requests")
        elif evidence_completeness < 0.70:
            steps.append("Collect remaining evidence for hypothesis validation")
        else:
            steps.append("Complete hypothesis testing to reach 70% confidence")
    elif phase == InvestigationPhase.SOLUTION:
        steps.append("Apply and verify solution")
    elif phase == InvestigationPhase.DOCUMENT:
        steps.append("Generate investigation artifacts")

    return steps


def _generate_blocked_reasons(
    investigation_state: InvestigationState,
    evidence_blocked_count: int,
    evidence_completeness: float,
    active_hypotheses_count: int,
) -> List[str]:
    """Generate reasons why investigation is blocked or progressing slowly"""
    reasons = []

    if evidence_blocked_count >= 2:
        reasons.append(
            f"{evidence_blocked_count} critical evidence requests blocked by user"
        )

    if evidence_completeness < 0.30:
        reasons.append(
            f"Evidence collection insufficient ({evidence_completeness*100:.0f}%)"
        )

    if active_hypotheses_count == 0:
        reasons.append("No active hypotheses remaining (all refuted or retired)")

    # Check confidence plateau
    trajectory = investigation_state.ooda_engine.confidence_trajectory[-3:]
    if len(trajectory) >= 3:
        if max(trajectory) - min(trajectory) < 0.05:
            reasons.append("Confidence plateaued - no significant changes in 3 turns")

    return reasons


def _create_early_phase_conclusion(
    investigation_state: InvestigationState,
    current_turn: int,
) -> WorkingConclusion:
    """Create working conclusion for early phases (before hypotheses)"""
    phase = investigation_state.lifecycle.current_phase

    statements = {
        InvestigationPhase.INTAKE: "Problem intake and assessment in progress",
        InvestigationPhase.BLAST_RADIUS: "Assessing impact scope and blast radius",
        InvestigationPhase.TIMELINE: "Establishing timeline and temporal context",
    }

    statement = statements.get(phase, "Investigation in early phase")

    return WorkingConclusion(
        statement=statement,
        confidence=0.0,
        confidence_level=ConfidenceLevel.SPECULATION,
        supporting_evidence_count=0,
        total_evidence_count=len(investigation_state.evidence.evidence_provided),
        evidence_completeness=0.0,
        caveats=["Investigation in early phase - hypotheses not yet generated"],
        alternative_explanations=[],
        can_proceed_with_solution=False,
        next_evidence_needed=[],
        last_updated_turn=current_turn,
        last_confidence_change_turn=current_turn,
        generated_at_turn=current_turn,
    )


def _create_refuted_conclusion(
    investigation_state: InvestigationState,
    current_turn: int,
    hypotheses: List[Hypothesis],
) -> WorkingConclusion:
    """Create working conclusion when all hypotheses refuted"""
    refuted_count = sum(1 for h in hypotheses if h.status == HypothesisStatus.REFUTED)

    return WorkingConclusion(
        statement=f"All {refuted_count} hypotheses refuted - investigation requires loop-back",
        confidence=0.0,
        confidence_level=ConfidenceLevel.SPECULATION,
        supporting_evidence_count=0,
        total_evidence_count=len(investigation_state.evidence.evidence_provided),
        evidence_completeness=0.0,
        caveats=[
            f"All {refuted_count} hypotheses have been refuted by evidence",
            "Need to generate new hypotheses with different approach",
        ],
        alternative_explanations=[],
        can_proceed_with_solution=False,
        next_evidence_needed=["Need new hypothesis generation approach"],
        last_updated_turn=current_turn,
        last_confidence_change_turn=current_turn,
        generated_at_turn=current_turn,
    )
