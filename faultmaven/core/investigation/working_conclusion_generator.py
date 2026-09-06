"""Working Conclusion and Progress Metrics Generator (v4.0 - Milestone-Based)

Generates agent's current best understanding and tracks investigation progress.
Refactored for milestone-based architecture (no OODA/phase references).

Design Reference:
- docs/architecture/investigation-engine/investigation-data-models.md
- docs/architecture/investigation-engine/evidence-driven-investigation-framework.md

Key Features:
- Works directly with Case model (no InvestigationState wrapper)
- Milestone-based progress tracking
- Momentum calculated from turn history
- Evidence completeness per hypothesis
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    ConfidenceLevel,
    Hypothesis,
    HypothesisState,
    InvestigationMomentum,
    WorkingConclusion,
)
from faultmaven.modules.case.domain.models import CauseState

#: Link count at or above which a hypothesis stops reading as thinly
#: supported. An editorial band, NOT a claim about what a case requires.
_WELL_SUPPORTED_LINK_COUNT = 3

#: Mean support density below which the next step is "collect more evidence"
#: rather than "validate what you have". Pre-dates the ``evidence_completeness``
#: rename and is kept at its original value: the density is a MEAN across
#: active hypotheses, so testing it against 1.0 would let one thinly linked
#: hypothesis dictate the next step for a well-supported pool.
_COLLECT_MORE_EVIDENCE_DENSITY = 0.70

#: Mean support density below which thin evidence is reported as a reason the
#: investigation is stalling. Also pre-dates the rename.
_BLOCKED_SUPPORT_DENSITY = 0.30


@dataclass
class ProgressMetrics:
    """
    Progress metrics for the current investigation state.

    Used internally for progress calculation.
    Key fields are transferred to TurnProgress.
    """

    investigation_momentum: InvestigationMomentum
    """Overall investigation momentum indicator"""

    support_density: float
    """Mean supporting-link density across active hypotheses (0.0-1.0).
    NOT evidence completeness — see ``_supporting_link_density``."""

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
        if h.state in [HypothesisState.ACTIVE, HypothesisState.VALIDATED]
    ]

    # Handle case with no hypotheses yet
    if not active_hypotheses:
        # #987: the hypothesis pool is not the only place the engine knows a
        # cause. When a RootCauseConclusion stands, mirror IT rather than
        # reporting "awaiting hypothesis generation" — the two are parallel
        # truth surfaces, and a working conclusion that contradicts a standing
        # conclusion is the divergence, not a harmless placeholder. Observed
        # live: a case whose LLM had authored a correct RCC still carried the
        # early-stage placeholder because every hypothesis had decayed to
        # RETIRED, and the resolution recap then rendered the placeholder.
        rcc_mirror = _conclusion_from_root_cause(case)
        if rcc_mirror is not None:
            return rcc_mirror
        return _create_early_stage_conclusion(case, current_turn)

    # Find highest likelihood hypothesis
    best_hypothesis = max(active_hypotheses, key=lambda h: h.likelihood)

    # Count supporting evidence
    supporting_count = len(best_hypothesis.supporting_evidence)
    total_evidence = len(case.evidence)

    # Generate caveats
    caveats = _generate_caveats(best_hypothesis)

    # Determine if can proceed with solution (≥70% likelihood)
    can_proceed = best_hypothesis.likelihood >= 0.70

    return WorkingConclusion(
        statement=best_hypothesis.statement,
        likelihood=best_hypothesis.likelihood,
        reasoning=(
            f"Based on {supporting_count} supporting evidence "
            f"item{'' if supporting_count == 1 else 's'} linked to this "
            f"hypothesis (of {total_evidence} on the case)."
        ),
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
        if h.state in [HypothesisState.ACTIVE, HypothesisState.VALIDATED]
    ]

    # Calculate evidence completeness across all active hypotheses
    support_density = _overall_support_density(active_hypotheses)

    # Determine investigation momentum
    momentum = _determine_investigation_momentum(case, current_turn)

    # Calculate turns since last progress
    turns_since_progress = case.turns_without_progress

    # Count hypotheses with sufficient evidence
    active_count = len(active_hypotheses)

    # Get highest hypothesis likelihood
    highest_likelihood = max((h.likelihood for h in active_hypotheses), default=0.0)

    # Generate next steps
    next_steps = _generate_next_steps(case, momentum, support_density)

    # Generate blocked reasons if momentum low
    blocked_reasons: List[str] = []
    if momentum in [InvestigationMomentum.LOW, InvestigationMomentum.BLOCKED]:
        blocked_reasons = _generate_blocked_reasons(case, support_density, active_count)

    return ProgressMetrics(
        investigation_momentum=momentum,
        support_density=support_density,
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


def _supporting_link_density(hypothesis: Hypothesis) -> float:
    """SUPPORTS links on this hypothesis over ``_WELL_SUPPORTED_LINK_COUNT``,
    capped at 1.0.

    A link COUNT expressed as a ratio so callers can band it. It is NOT
    evidence completeness and must never be rendered as a percentage of
    required evidence: the engine has no per-hypothesis requirement model here
    (the demand side lives in ``EvidenceNeed``, which this module never reads).

    Was ``_calculate_hypothesis_evidence_completeness``, which preferred a
    ``hypothesis.evidence_requirements`` field that **exists nowhere in the
    codebase** — so that branch was unreachable and the value was ALWAYS
    ``links / 3``. Every renderer still printed it as "N% evidence
    completeness", including into the LLM prompt on every turn via the working
    conclusion's ``reasoning``: 2 links read "67% complete", 14 links read
    "100%", and neither figure meant anything (fm#1122).
    """
    return min(len(hypothesis.supporting_evidence) / _WELL_SUPPORTED_LINK_COUNT, 1.0)


def _overall_support_density(active_hypotheses: List[Hypothesis]) -> float:
    """Mean supporting-link density across active hypotheses.

    ``0.0`` for an empty list is "nothing to average", NOT "nothing is
    linked" — callers that phrase a finding about active hypotheses must
    check the count first.
    """
    if not active_hypotheses:
        return 0.0

    densities = [_supporting_link_density(h) for h in active_hypotheses]

    return sum(densities) / len(densities)


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

    # Check recent turn history (last 3 turns). Exclude SKIPPED recovery
    # placeholders — they aren't real turns and would dilute momentum.
    real_turns = [t for t in case.turn_history if not t.is_skipped]
    recent_turns = real_turns[-3:] if real_turns else []

    if len(recent_turns) < 2:
        return InvestigationMomentum.MODERATE  # Not enough data

    # Count recent progress indicators.
    #
    # ‼ These thresholds were calibrated while ``hypotheses_validated`` was
    # PERMANENTLY EMPTY — it had no writer at all until #1284, measured at 0 of
    # 2,578 persisted turns — so the band has only ever seen two of its three
    # declared summands. It now sees three, which shifts cases upward.
    #
    # The shift is largest on an identification turn, where one derivation
    # contributes to TWO summands: the chain root validating puts the hypothesis
    # id in ``hypotheses_validated`` and, when the symptom is verified and the
    # root uncontested, also raises ``cause_state`` and records
    # ``root_cause_identified`` in ``milestones_completed``. They are genuinely
    # distinct facts — a hypothesis can validate turns before the symptom is
    # verified, and identification can arrive on a turn that validates nothing
    # new — but on the common turn where they coincide, work that previously
    # summed to 0 here can now reach HIGH on its own.
    #
    # Deliberately NOT re-tuned in #1284: picking new thresholds needs a corpus
    # of cases scored with a working third arm, which does not exist yet
    # (#1284 called for exactly this re-check). Read the bands as provisional.
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


def _generate_caveats(hypothesis: Hypothesis) -> List[str]:
    """Generate caveats based on evidence state and confidence.

    Takes no density: the thin-support caveat bands the raw link COUNT on this
    hypothesis, and keeping the parameter would tell the next reader the
    caveat still tracks a density it does not read.
    """
    caveats = []

    # Thin-support caveat, stated as the COUNT. The engine does not know what
    # this hypothesis requires, so it must not imply a fraction of it.
    linked = len(hypothesis.supporting_evidence)
    if linked < _WELL_SUPPORTED_LINK_COUNT:
        caveats.append(
            f"Only {linked} supporting evidence "
            f"item{'' if linked == 1 else 's'} linked to this hypothesis"
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
    support_density: float,
) -> List[str]:
    """Generate next steps based on investigation state."""
    steps = []

    # Status-based guidance
    if case.state == CaseState.INQUIRY:
        steps.append("Confirm problem statement and decide to investigate")
        return steps

    if case.state in [CaseState.RESOLVED, CaseState.CLOSED]:
        steps.append("Case closed - review documentation")
        return steps

    # Progress-based guidance
    progress = case.progress

    if not progress.symptom_verified:
        steps.append("Verify symptom with concrete evidence")
    elif progress.cause_state != CauseState.IDENTIFIED:
        if support_density < _COLLECT_MORE_EVIDENCE_DENSITY:
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
    support_density: float,
    active_hypotheses_count: int,
) -> List[str]:
    """Generate reasons why investigation is blocked or progressing slowly."""
    reasons = []

    if case.turns_without_progress >= 3:
        reasons.append(
            f"No progress for {case.turns_without_progress} consecutive turns"
        )

    # Guarded on the count: an empty active list averages to 0.0, and a reason
    # asserting that nothing is linked to any active hypothesis states a fact
    # about hypotheses that do not exist — the empty case has its own reason
    # below. Two bands so the exact-zero wording stays exact while a starved
    # pool (some links, far too few) is still reported.
    if active_hypotheses_count > 0:
        if support_density <= 0.0:
            reasons.append("No supporting evidence linked to any active hypothesis")
        elif support_density < _BLOCKED_SUPPORT_DENSITY:
            reasons.append("Supporting evidence is thin across active hypotheses")

    if active_hypotheses_count == 0 and len(case.hypotheses) > 0:
        reasons.append("No active hypotheses remaining (all refuted or retired)")

    return reasons


def _conclusion_from_root_cause(case: Case) -> WorkingConclusion | None:
    """Mirror a standing ``RootCauseConclusion`` into a WorkingConclusion, or
    ``None`` when no conclusion names a cause (#987).

    Used only on the no-standing-hypothesis path: with an ACTIVE/VALIDATED
    hypothesis the working conclusion tracks the live differential, which is
    its job. Without one, the honest answer is whatever the case's own
    conclusion says — not a claim that the investigation has not started.
    """
    rcc = getattr(case, "root_cause_conclusion", None)
    statement = getattr(rcc, "root_cause", None) if rcc is not None else None
    if not statement:
        return None
    likelihood = getattr(rcc, "likelihood", None) or 0.0
    return WorkingConclusion(
        statement=statement,
        likelihood=likelihood,
        reasoning=(
            "Mirrors the recorded root-cause conclusion; no hypothesis is "
            "currently active."
        ),
        supporting_evidence_ids=list(getattr(rcc, "evidence_basis", None) or []),
        caveats=[],
        updated_at=datetime.now(timezone.utc),
        # Marks this as a MIRROR, not an independent finding — so a retracted
        # conclusion cannot keep satisfying the working-conclusion backstop leg
        # for one more turn through its own stale mirror (#987).
        mirrors_root_cause_conclusion=True,
    )


def is_early_stage_conclusion(conclusion) -> bool:
    """Is this WorkingConclusion the EARLY-STAGE PLACEHOLDER rather than a real
    finding? (#987)

    The placeholder produced by ``_create_early_stage_conclusion`` says the
    investigation has not reached a cause yet ("Investigating potential causes
    - awaiting hypothesis generation"). It is legitimate context mid-diagnosis
    and illegitimate anywhere that renders a CONCLUSION — most sharply in the
    resolution-confirmation recap, where it contradicts everything the user
    just read.

    Detected STRUCTURALLY, never by matching the placeholder's wording: it is
    the only conclusion this module emits with zero likelihood AND no
    supporting evidence. Both the real hypothesis-backed conclusion (which
    carries the hypothesis's likelihood and its supporting evidence) and the
    conclusion mirror (which carries the RCC's) fail this test, so the
    predicate stays correct if the placeholder text is ever reworded.
    """
    if conclusion is None:
        return True
    return not (getattr(conclusion, "likelihood", 0) or 0) > 0 and not (
        getattr(conclusion, "supporting_evidence_ids", None) or []
    )


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
    elif progress.cause_state != CauseState.IDENTIFIED:
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
