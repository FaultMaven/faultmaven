"""Systematic Hypothesis Generation (Proposal #2: Continuous Hypothesis Generation)

Phase 3 systematic hypothesis generation that reviews opportunistic hypotheses
and generates comprehensive coverage.

Design Principle:
- Review opportunistic hypotheses captured in Phases 0-2
- Promote high-evidence hypotheses to ACTIVE status
- Retire low-evidence hypotheses
- Identify coverage gaps (untested categories)
- Generate systematic hypotheses for gaps
- Ensure minimum hypothesis count (2-4)

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from typing import List, Set, Optional, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationState, Hypothesis

from faultmaven.models.investigation import (
    Hypothesis,
    HypothesisStatus,
    HypothesisGenerationMode,
    InvestigationPhase,
)
from faultmaven.services.agentic.hypothesis.opportunistic_capture import (
    calculate_evidence_ratio,
)

logger = logging.getLogger(__name__)


# Hypothesis categories for coverage tracking
HYPOTHESIS_CATEGORIES = [
    "infrastructure",
    "code",
    "configuration",
    "external_dependency",
    "client_side",
    "data",
    "network",
    "security",
    "resource_exhaustion",
]


def systematic_hypothesis_generation(
    state: "InvestigationState",
    llm_generate_hypotheses_callback=None,
) -> List["Hypothesis"]:
    """Phase 3: Systematic hypothesis generation

    Reviews opportunistic hypotheses and generates systematic coverage.

    Args:
        state: Investigation state
        llm_generate_hypotheses_callback: Optional callback to LLM for generating hypotheses

    Returns:
        List of newly generated systematic hypotheses

    Process:
    1. Review opportunistic hypotheses
    2. Promote or retire based on evidence ratio
    3. Identify coverage gaps
    4. Generate systematic hypotheses for gaps
    5. Ensure minimum hypothesis count
    """
    logger.info("Starting systematic hypothesis generation (Phase 3)")

    # Step 1: Review opportunistic hypotheses
    opportunistic = [
        h for h in state.ooda_engine.hypotheses
        if h.generation_mode == HypothesisGenerationMode.OPPORTUNISTIC
        and h.status == HypothesisStatus.CAPTURED
    ]

    logger.info(f"Found {len(opportunistic)} opportunistic hypotheses to review")

    promoted_count = 0
    retired_count = 0

    for hypo in opportunistic:
        evidence_ratio = calculate_evidence_ratio(hypo)

        if evidence_ratio > 0.7:
            # High supporting evidence → Promote to ACTIVE
            hypo.status = HypothesisStatus.ACTIVE
            hypo.promoted_to_active_at_turn = state.metadata.current_turn
            promoted_count += 1
            logger.info(
                f"Promoted opportunistic hypothesis to ACTIVE: {hypo.statement} "
                f"(evidence ratio: {evidence_ratio:.2f})"
            )

        elif evidence_ratio < 0.3:
            # Low supporting evidence → Retire
            hypo.status = HypothesisStatus.RETIRED
            retired_count += 1
            logger.info(
                f"Retired opportunistic hypothesis: {hypo.statement} "
                f"(evidence ratio: {evidence_ratio:.2f})"
            )

        else:
            # Moderate evidence (0.3-0.7) → Keep as CAPTURED for now
            logger.info(
                f"Keeping opportunistic hypothesis as CAPTURED: {hypo.statement} "
                f"(evidence ratio: {evidence_ratio:.2f})"
            )

    logger.info(f"Review complete: {promoted_count} promoted, {retired_count} retired")

    # Step 2: Identify coverage gaps
    tested_categories = _get_tested_categories(state)
    untested_categories = set(HYPOTHESIS_CATEGORIES) - tested_categories

    logger.info(
        f"Coverage analysis: {len(tested_categories)} categories covered, "
        f"{len(untested_categories)} gaps identified"
    )

    # Step 3: Generate systematic hypotheses for gaps
    new_hypotheses = []

    if llm_generate_hypotheses_callback and untested_categories:
        # Use LLM to generate hypotheses for untested categories
        logger.info(f"Generating systematic hypotheses for categories: {untested_categories}")

        # LLM callback should return list of hypothesis statements
        generated = llm_generate_hypotheses_callback(
            state=state,
            target_categories=list(untested_categories),
        )

        for statement, category in generated:
            hypo = _create_systematic_hypothesis(
                statement=statement,
                category=category,
                turn=state.metadata.current_turn,
            )
            new_hypotheses.append(hypo)
            state.ooda_engine.hypotheses.append(hypo)

    # Step 4: Ensure minimum hypothesis count
    active_count = sum(
        1 for h in state.ooda_engine.hypotheses
        if h.status == HypothesisStatus.ACTIVE
    )

    if active_count < 2:
        logger.warning(f"Only {active_count} active hypotheses. Minimum is 2.")

        # Promote best CAPTURED hypotheses
        captured = [
            h for h in state.ooda_engine.hypotheses
            if h.status == HypothesisStatus.CAPTURED
        ]

        # Sort by evidence ratio (descending)
        captured_sorted = sorted(
            captured,
            key=lambda h: calculate_evidence_ratio(h),
            reverse=True,
        )

        needed = 2 - active_count
        for hypo in captured_sorted[:needed]:
            hypo.status = HypothesisStatus.ACTIVE
            hypo.promoted_to_active_at_turn = state.metadata.current_turn
            logger.info(f"Force-promoted hypothesis to meet minimum: {hypo.statement}")

    logger.info(
        f"Systematic generation complete. Generated {len(new_hypotheses)} new hypotheses. "
        f"Total active: {active_count}"
    )

    return new_hypotheses


def _get_tested_categories(state: "InvestigationState") -> Set[str]:
    """Get categories already covered by hypotheses

    Args:
        state: Investigation state

    Returns:
        Set of category strings
    """
    categories = set()

    for hypo in state.ooda_engine.hypotheses:
        if hypo.status in [HypothesisStatus.ACTIVE, HypothesisStatus.CAPTURED]:
            categories.add(hypo.category)

    return categories


def _create_systematic_hypothesis(
    statement: str,
    category: str,
    turn: int,
) -> "Hypothesis":
    """Create a systematic hypothesis

    Args:
        statement: Hypothesis statement
        category: Hypothesis category
        turn: Current turn number

    Returns:
        Hypothesis with ACTIVE status and moderate confidence (0.5)
    """
    hypothesis = Hypothesis(
        hypothesis_id=str(uuid4()),
        statement=statement,
        category=category,

        # Generation metadata
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        captured_in_phase=InvestigationPhase.HYPOTHESIS,
        captured_at_turn=turn,

        # Lifecycle
        status=HypothesisStatus.ACTIVE,
        promoted_to_active_at_turn=turn,

        # Context
        triggering_observation=None,  # Systematic, not triggered

        # Confidence
        likelihood=0.5,  # Moderate initial confidence for systematic
        initial_likelihood=0.5,
        confidence_trajectory=[(turn, 0.5)],

        # Legacy fields
        created_at_turn=turn,
        last_updated_turn=turn,

        # Evidence (empty initially)
        supporting_evidence=[],
        refuting_evidence=[],

        # Testing (to be filled)
        test_plan=None,
        test_results=[],
    )

    logger.info(
        f"Created systematic hypothesis: {statement}",
        extra={
            "hypothesis_id": hypothesis.hypothesis_id,
            "category": category,
            "turn": turn,
        },
    )

    return hypothesis


def get_active_hypotheses(state: "InvestigationState") -> List["Hypothesis"]:
    """Get all active hypotheses ready for testing

    Args:
        state: Investigation state

    Returns:
        List of hypotheses with ACTIVE status
    """
    return [
        h for h in state.ooda_engine.hypotheses
        if h.status == HypothesisStatus.ACTIVE
    ]


def promote_hypothesis(
    hypothesis: "Hypothesis",
    turn: int,
) -> None:
    """Manually promote a hypothesis to ACTIVE status

    Args:
        hypothesis: Hypothesis to promote
        turn: Current turn number
    """
    if hypothesis.status != HypothesisStatus.CAPTURED:
        logger.warning(
            f"Cannot promote hypothesis with status {hypothesis.status}. "
            f"Only CAPTURED hypotheses can be promoted."
        )
        return

    hypothesis.status = HypothesisStatus.ACTIVE
    hypothesis.promoted_to_active_at_turn = turn

    logger.info(f"Manually promoted hypothesis to ACTIVE: {hypothesis.statement}")


def retire_hypothesis(
    hypothesis: "Hypothesis",
    reason: str,
) -> None:
    """Retire a hypothesis

    Args:
        hypothesis: Hypothesis to retire
        reason: Reason for retirement
    """
    if hypothesis.status in [HypothesisStatus.VALIDATED, HypothesisStatus.REFUTED]:
        logger.warning(
            f"Cannot retire hypothesis with status {hypothesis.status}. "
            f"Already conclusively tested."
        )
        return

    hypothesis.status = HypothesisStatus.RETIRED

    logger.info(
        f"Retired hypothesis: {hypothesis.statement}",
        extra={"reason": reason},
    )


def supersede_hypothesis(
    old_hypothesis: "Hypothesis",
    new_hypothesis: "Hypothesis",
) -> None:
    """Mark old hypothesis as superseded by new one

    Args:
        old_hypothesis: Hypothesis being replaced
        new_hypothesis: Replacement hypothesis
    """
    old_hypothesis.status = HypothesisStatus.SUPERSEDED

    logger.info(
        f"Superseded hypothesis: {old_hypothesis.statement} → {new_hypothesis.statement}",
        extra={
            "old_id": old_hypothesis.hypothesis_id,
            "new_id": new_hypothesis.hypothesis_id,
        },
    )
