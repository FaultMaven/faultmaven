"""Working Conclusion and Progress Metrics Generator (v4.0 - Milestone-Based)

Generates agent's current best understanding and tracks investigation progress.
Refactored for milestone-based architecture (no OODA/phase references).

Design Reference:
- docs/architecture/investigation-engine/investigation-data-models.md
- docs/architecture/investigation-engine/opportunistic-investigation-framework.md

Key Features:
- Works directly with Case model (no InvestigationState wrapper)
- Milestone-based progress tracking
- Momentum calculated from turn history
- Evidence completeness per hypothesis
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    ConfidenceLevel,
    Hypothesis,
    HypothesisStatus,
    InvestigationMomentum,
    WorkingConclusion,
)


@dataclass
class ProgressMetrics:
    """
    Progress metrics for the current investigation state.

    Used internally for progress calculation.
    Key fields are transferred to TurnProgress.
    """

    investigation_momentum: InvestigationMomentum
    """Overall investigation momentum indicator"""

    evidence_completeness: float
    """Average evidence completeness across active hypotheses (0.0-1.0)"""

    turns_since_last_progress: int
    """Number of turns since meaningful progress was made"""

    active_hypotheses_count: int
    """Number of active hypotheses being tested"""

    highest_hypothesis_likelihood: float
    """Highest likelihood among active hypotheses"""

    blocked_reasons: List[str]
    """Reasons why investigation is blocked or slow"""

    next_steps: List[str]
    """Suggested next actions for the investigation"""


def generate_working_conclusion(
    case: Case,
    current_turn: int,
) -> WorkingConclusion:
    """Generate working conclusion based on current case state.

    Called EVERY turn during INVESTIGATING status to maintain consistent
    context tracking and prevent "lost context" issues.

    Reference: Gap #7 - Working Conclusion Every Turn

    Args:
        case: Current case with hypotheses, evidence, and progress
        current_turn: Current conversation turn number

    Returns:
        WorkingConclusion representing agent's current understanding
    """
    # Get active hypotheses
    hypotheses = list(case.hypotheses.values())
    active_hypotheses = [
        h
        for h in hypotheses
        if h.status in [HypothesisStatus.ACTIVE, HypothesisStatus.VALIDATED]
    ]

    # Handle case with no hypotheses yet
    if not active_hypotheses:
        return _create_early_stage_conclusion(case, current_turn)

    # Find highest likelihood hypothesis
    best_hypothesis = max(active_hypotheses, key=lambda h: h.likelihood)

    # Calculate evidence completeness for best hypothesis
    evidence_completeness = _calculate_hypothesis_evidence_completeness(
        best_hypothesis, case
    )

    # Count supporting evidence
    supporting_count = len(best_hypothesis.supporting_evidence)
    total_evidence = len(case.evidence)

    # Generate caveats
    caveats = _generate_caveats(best_hypothesis, evidence_completeness, case)

    # Determine if can proceed with solution (≥70% likelihood)
    can_proceed = best_hypothesis.likelihood >= 0.70

    return WorkingConclusion(
        statement=best_hypothesis.statement,
        likelihood=best_hypothesis.likelihood,
        reasoning=f"Based on {supporting_count} supporting evidence items "
        f"with {evidence_completeness * 100:.0f}% evidence completeness.",
        supporting_evidence_ids=list(best_hypothesis.supporting_evidence),
        caveats=caveats,
        updated_at=datetime.now(timezone.utc),
    )


def calculate_progress_metrics(
    case: Case,
    current_turn: int,
) -> ProgressMetrics:
    """Calculate investigation progress metrics.

    Provides momentum indicator and diagnostic information
    for tracking investigation health.

    Args:
        case: Current case
        current_turn: Current conversation turn number

    Returns:
        ProgressMetrics with momentum and next steps
    """
    hypotheses = list(case.hypotheses.values())
    active_hypotheses = [
        h
        for h in hypotheses
        if h.status in [HypothesisStatus.ACTIVE, HypothesisStatus.VALIDATED]
    ]

    # Calculate evidence completeness across all active hypotheses
    evidence_completeness = _calculate_overall_evidence_completeness(
        active_hypotheses, case
    )

    # Determine investigation momentum
    momentum = _determine_investigation_momentum(case, current_turn)

    # Calculate turns since last progress
    turns_since_progress = case.turns_without_progress

    # Count hypotheses with sufficient evidence
    active_count = len(active_hypotheses)

    # Get highest hypothesis likelihood
    highest_likelihood = max((h.likelihood for h in active_hypotheses), default=0.0)

    # Generate next steps
    next_steps = _generate_next_steps(case, momentum, evidence_completeness)

    # Generate blocked reasons if momentum low
    blocked_reasons: List[str] = []
    if momentum in [InvestigationMomentum.LOW, InvestigationMomentum.BLOCKED]:
        blocked_reasons = _generate_blocked_reasons(
            case, evidence_completeness, active_count
        )

    return ProgressMetrics(
        investigation_momentum=momentum,
        evidence_completeness=evidence_completeness,
        turns_since_last_progress=turns_since_progress,
        active_hypotheses_count=active_count,
        highest_hypothesis_likelihood=highest_likelihood,
        blocked_reasons=blocked_reasons,
        next_steps=next_steps,
    )


# =============================================================================
# Helper Functions
# =============================================================================


def _get_confidence_level_from_value(likelihood: float) -> ConfidenceLevel:
    """Map likelihood value to confidence level enum."""
    if likelihood >= 0.90:
        return ConfidenceLevel.VERIFIED
    elif likelihood >= 0.70:
        return ConfidenceLevel.CONFIDENT
    elif likelihood >= 0.50:
        return ConfidenceLevel.PROBABLE
    else:
        return ConfidenceLevel.SPECULATION


def _calculate_hypothesis_evidence_completeness(
    hypothesis: Hypothesis, case: Case
) -> float:
    """Calculate evidence completeness for a single hypothesis."""
    # Check evidence requirements if defined
    if (
        hasattr(hypothesis, "evidence_requirements")
        and hypothesis.evidence_requirements
    ):
        required = len(hypothesis.evidence_requirements)
        obtained = len(hypothesis.supporting_evidence)
        return min(obtained / required, 1.0) if required > 0 else 1.0

    # Fallback: use supporting evidence count relative to typical needs
    # Typical investigation needs ~3-5 evidence items per hypothesis
    typical_required = 3
    obtained = len(hypothesis.supporting_evidence)
    return min(obtained / typical_required, 1.0)


def _calculate_overall_evidence_completeness(
    active_hypotheses: List[Hypothesis],
    case: Case,
) -> float:
    """Calculate average evidence completeness across all active hypotheses."""
    if not active_hypotheses:
        return 0.0

    completeness_scores = [
        _calculate_hypothesis_evidence_completeness(h, case) for h in active_hypotheses
    ]

    return sum(completeness_scores) / len(completeness_scores)


def _determine_investigation_momentum(
    case: Case,
    current_turn: int,
) -> InvestigationMomentum:
    """Determine investigation momentum based on recent progress.

    Momentum levels:
    - HIGH: Evidence flowing, milestones completing
    - MODERATE: Some progress, steady state
    - LOW: Little progress, confidence plateaued
    - BLOCKED: Stuck for multiple turns
    """
    # Check for blocked state (5+ turns without investigative progress)
    if case.turns_without_progress >= 5:
        return InvestigationMomentum.BLOCKED

    # Check recent turn history (last 3 turns)
    recent_turns = case.turn_history[-3:] if case.turn_history else []

    if len(recent_turns) < 2:
        return InvestigationMomentum.MODERATE  # Not enough data

    # Count recent progress indicators
    recent_milestones = sum(len(t.milestones_completed) for t in recent_turns)
    recent_evidence = sum(len(t.evidence_added) for t in recent_turns)
    recent_hypotheses = sum(len(t.hypotheses_validated) for t in recent_turns)

    total_progress = recent_milestones + recent_evidence + recent_hypotheses

    # Determine momentum based on progress
    if total_progress >= 4:
        return InvestigationMomentum.HIGH
    elif total_progress >= 1:
        return InvestigationMomentum.MODERATE
    elif case.turns_without_progress >= 3:
        return InvestigationMomentum.LOW
    else:
        return InvestigationMomentum.MODERATE


def _generate_caveats(
    hypothesis: Hypothesis,
    evidence_completeness: float,
    case: Case,
) -> List[str]:
    """Generate caveats based on evidence state and confidence."""
    caveats = []

    # Evidence completeness caveats
    if evidence_completeness < 0.50:
        caveats.append(
            f"Only {evidence_completeness * 100:.0f}% of required evidence collected"
        )
    elif evidence_completeness < 0.70:
        caveats.append(
            f"Evidence partially complete ({evidence_completeness * 100:.0f}%)"
        )

    # Likelihood level caveats
    if hypothesis.likelihood < 0.50:
        caveats.append("Low confidence - this is speculative")
    elif hypothesis.likelihood < 0.70:
        caveats.append("Moderate confidence - not yet validated")

    # Refuting evidence caveats
    if hasattr(hypothesis, "refuting_evidence") and hypothesis.refuting_evidence:
        caveats.append(
            f"{len(hypothesis.refuting_evidence)} evidence items contradict this hypothesis"
        )

    return caveats


def _generate_next_steps(
    case: Case,
    momentum: InvestigationMomentum,
    evidence_completeness: float,
) -> List[str]:
    """Generate next steps based on investigation state."""
    steps = []

    # Status-based guidance
    if case.status == CaseStatus.INQUIRY:
        steps.append("Confirm problem statement and decide to investigate")
        return steps

    if case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
        steps.append("Case closed - review documentation")
        return steps

    # Progress-based guidance
    progress = case.progress

    if not progress.symptom_verified:
        steps.append("Verify symptom with concrete evidence")
    elif not progress.scope_assessed:
        steps.append("Assess scope and affected systems")
    elif not progress.timeline_established:
        steps.append("Establish timeline of events")
    elif not progress.changes_identified:
        steps.append("Identify recent changes")
    elif not progress.root_cause_identified:
        if evidence_completeness < 0.70:
            steps.append("Collect more evidence to test hypotheses")
        else:
            steps.append("Validate hypotheses to identify root cause")
    elif not progress.solution_proposed:
        steps.append("Propose solution based on root cause")
    elif not progress.solution_verified:
        steps.append("Apply and verify solution effectiveness")

    # Momentum-based guidance
    if momentum == InvestigationMomentum.BLOCKED:
        steps.append("Address blockers or request user input")
    elif momentum == InvestigationMomentum.LOW:
        steps.append("Consider alternative approaches or hypotheses")

    return steps[:3]  # Limit to top 3


def _generate_blocked_reasons(
    case: Case,
    evidence_completeness: float,
    active_hypotheses_count: int,
) -> List[str]:
    """Generate reasons why investigation is blocked or progressing slowly."""
    reasons = []

    if case.turns_without_progress >= 3:
        reasons.append(
            f"No progress for {case.turns_without_progress} consecutive turns"
        )

    if evidence_completeness < 0.30:
        reasons.append(
            f"Evidence collection insufficient ({evidence_completeness * 100:.0f}%)"
        )

    if active_hypotheses_count == 0 and len(case.hypotheses) > 0:
        reasons.append("No active hypotheses remaining (all refuted or retired)")

    return reasons


def _create_early_stage_conclusion(
    case: Case,
    current_turn: int,
) -> WorkingConclusion:
    """Create working conclusion for early stages (before hypotheses)."""
    progress = case.progress
    stage = progress.current_stage if hasattr(progress, "current_stage") else None

    # Generate statement based on current stage
    if not progress.symptom_verified:
        statement = "Verifying symptom - problem understanding in progress"
    elif not progress.scope_assessed:
        statement = "Assessing scope and impact of the problem"
    elif not progress.timeline_established:
        statement = "Establishing timeline and temporal context"
    elif not progress.root_cause_identified:
        statement = "Investigating potential causes - awaiting hypothesis generation"
    else:
        statement = "Investigation in progress"

    return WorkingConclusion(
        statement=statement,
        likelihood=0.0,
        reasoning="Investigation in early stage - hypotheses not yet generated",
        supporting_evidence_ids=[],
        caveats=["No hypotheses generated yet"],
        updated_at=datetime.now(timezone.utc),
    )
