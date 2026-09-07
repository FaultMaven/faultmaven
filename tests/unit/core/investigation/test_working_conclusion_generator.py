"""Tests for WorkingConclusionGenerator

Tests progress metrics and working conclusion generation.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.working_conclusion_generator import (
    ProgressMetrics,
    calculate_progress_metrics,
    generate_working_conclusion,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationMomentum,
    InvestigationProgress,
    ProblemVerification,
    TurnOutcome,
    TurnProgress,
)


@pytest.fixture
def base_case():
    """Create a base case for testing."""
    return Case(
        case_id="case_1234567890ab",
        title="Test Case",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        enterprise_id="org_123",
        description="Test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
        ),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test symptom",
        ),
        progress=InvestigationProgress(),
        turns_without_progress=0,
    )


def create_hypothesis(
    hyp_id: str,
    statement: str,
    state: HypothesisState = HypothesisState.ACTIVE,
    likelihood: float = 0.5,
    supporting_evidence: list = None,
    generated_at_turn: int = 1,
) -> Hypothesis:
    """Helper to create a hypothesis."""
    hyp = Hypothesis(
        hypothesis_id=hyp_id,
        statement=statement,
        category=HypothesisCategory.CODE,
        state=state,
        likelihood=likelihood,
        generated_at_turn=generated_at_turn,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="Test hypothesis",
    )
    # Add supporting evidence via evidence_links if provided
    if supporting_evidence:
        from faultmaven.modules.case.contracts import (
            EvidenceStance,
            HypothesisEvidenceLink,
        )

        for ev_id in supporting_evidence:
            hyp.evidence_links.append(
                HypothesisEvidenceLink(
                    hypothesis_id=hyp_id,
                    evidence_id=ev_id,
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="Test supporting evidence",
                    stance_confidence=0.8,
                )
            )
    return hyp


def create_turn(
    turn_number: int,
    milestones_completed: list = None,
    evidence_added: list = None,
    hypotheses_validated: list = None,
) -> TurnProgress:
    """Helper to create a turn progress record."""
    return TurnProgress(
        turn_number=turn_number,
        timestamp=datetime.now(timezone.utc),
        milestones_completed=milestones_completed or [],
        evidence_added=evidence_added or [],
        hypotheses_generated=[],
        hypotheses_validated=hypotheses_validated or [],
        solutions_proposed=[],
        progress_made=bool(
            milestones_completed or evidence_added or hypotheses_validated
        ),
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary="test",
        agent_response_summary="test",
    )


class TestWorkingConclusionGeneration:
    """Test working conclusion generation."""

    def test_generates_conclusion_from_best_hypothesis(self, base_case):
        """Should generate conclusion from highest likelihood hypothesis."""
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001", "Low likelihood", HypothesisState.ACTIVE, 0.3
            ),
            "hyp_000000000002": create_hypothesis(
                "hyp_000000000002",
                "High likelihood",
                HypothesisState.ACTIVE,
                0.8,
                supporting_evidence=["ev_1", "ev_2"],
            ),
        }

        conclusion = generate_working_conclusion(base_case, current_turn=1)

        assert conclusion.statement == "High likelihood"
        assert conclusion.likelihood == 0.8

    def test_generates_early_stage_conclusion(self, base_case):
        """Should generate early stage conclusion when no hypotheses."""
        base_case.hypotheses = {}
        base_case.progress.symptom_verified = False

        conclusion = generate_working_conclusion(base_case, current_turn=1)

        assert "Verifying" in conclusion.statement
        assert conclusion.likelihood == 0.0
        assert "No hypotheses" in conclusion.caveats[0]

    def test_includes_supporting_evidence_ids(self, base_case):
        """Should include supporting evidence IDs."""
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Test",
                HypothesisState.ACTIVE,
                0.7,
                supporting_evidence=["ev_1", "ev_2", "ev_3"],
            ),
        }

        conclusion = generate_working_conclusion(base_case, current_turn=1)

        assert conclusion.supporting_evidence_ids == ["ev_1", "ev_2", "ev_3"]

    def test_generates_caveats_for_low_confidence(self, base_case):
        """Should generate caveats for low confidence hypotheses."""
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001", "Low confidence", HypothesisState.ACTIVE, 0.3
            ),
        }

        conclusion = generate_working_conclusion(base_case, current_turn=1)

        assert any("speculative" in c.lower() for c in conclusion.caveats)


class TestProgressMetricsCalculation:
    """Test progress metrics calculation."""

    def test_high_momentum_with_progress(self, base_case):
        """Should return HIGH momentum when milestones completing."""
        base_case.turn_history = [
            create_turn(1, milestones_completed=["symptom_verified"]),
            create_turn(2, evidence_added=["ev_1", "ev_2"]),
            create_turn(3, milestones_completed=["root_cause_identified"]),
        ]

        metrics = calculate_progress_metrics(base_case, current_turn=4)

        assert metrics.investigation_momentum == InvestigationMomentum.HIGH

    def test_moderate_momentum_with_some_progress(self, base_case):
        """Should return MODERATE momentum with minimal progress."""
        base_case.turn_history = [
            create_turn(1),
            create_turn(2, evidence_added=["ev_1"]),
            create_turn(3),
        ]

        metrics = calculate_progress_metrics(base_case, current_turn=4)

        assert metrics.investigation_momentum == InvestigationMomentum.MODERATE

    def test_blocked_momentum_after_stagnation(self, base_case):
        """Should return BLOCKED momentum after 5+ turns without progress."""
        base_case.turns_without_progress = 5
        base_case.turn_history = [
            create_turn(1),
            create_turn(2),
            create_turn(3),
            create_turn(4),
            create_turn(5),
        ]

        metrics = calculate_progress_metrics(base_case, current_turn=6)

        assert metrics.investigation_momentum == InvestigationMomentum.BLOCKED


class TestEvidenceCompleteness:
    """Test the supporting-link density calculation."""

    def test_density_is_links_over_the_editorial_band(self, base_case):
        """Density is a LINK COUNT over `_WELL_SUPPORTED_LINK_COUNT`.

        Renamed from "completeness": the old name implied a fraction of what
        the case required, which the engine never knew. Nothing may render
        this as a percentage of required evidence (fm#1122).
        """
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Test",
                HypothesisState.ACTIVE,
                0.5,
                supporting_evidence=["ev_1", "ev_2"],  # 2 out of typical 3
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        # 2 links over the band of 3 — a density, not a completeness claim.
        assert 0.6 <= metrics.support_density <= 0.7

    def test_no_visible_string_states_a_completeness_percentage(self, base_case):
        """The renderers must state COUNTS, never a fabricated fraction.

        The reasoning string goes into the LLM prompt every turn. With the old
        hardcoded denominator a 2-link hypothesis read "67% evidence
        completeness" and a 14-link one read "100%", so the model was handed an
        engine-voiced number that measured nothing (fm#1122).
        """
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Heap exceeds the container limit",
                HypothesisState.ACTIVE,
                0.8,
                supporting_evidence=["ev_1", "ev_2"],
            ),
        }

        conclusion = generate_working_conclusion(base_case, current_turn=4)
        rendered = conclusion.reasoning + " " + " ".join(conclusion.caveats)

        assert "%" not in rendered, f"a fabricated percentage survives: {rendered!r}"
        assert "completeness" not in rendered.lower()
        assert "2 supporting evidence items" in conclusion.reasoning


class TestNextStepsGeneration:
    """Test next steps suggestion."""

    def test_suggests_verify_symptom_first(self, base_case):
        """Should suggest symptom verification when not done."""
        base_case.progress.symptom_verified = False

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert any("symptom" in step.lower() for step in metrics.next_steps)

    def test_suggests_investigation_after_symptom(self, base_case):
        """Should suggest investigation steps after symptom verified."""
        base_case.progress.symptom_verified = True

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert len(metrics.next_steps) > 0
        assert not any("symptom" in step.lower() for step in metrics.next_steps)

    def test_suggests_solution_after_root_cause(self, base_case):
        """Should suggest solution after root cause identified."""
        from faultmaven.modules.case.contracts import CauseState

        base_case.progress.symptom_verified = True
        base_case.progress.cause_state = CauseState.IDENTIFIED
        base_case.progress.solution_proposed = False

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert any("solution" in step.lower() for step in metrics.next_steps)


class TestBlockedReasonsGeneration:
    """Test blocked reasons generation."""

    def test_generates_blocked_reasons_for_stagnation(self, base_case):
        """Should generate blocked reasons when momentum is low."""
        base_case.turns_without_progress = 5

        metrics = calculate_progress_metrics(base_case, current_turn=6)

        assert len(metrics.blocked_reasons) > 0
        assert any("progress" in reason.lower() for reason in metrics.blocked_reasons)

    def test_includes_stagnation_blocked_reason(self, base_case):
        """Should include blocked reason when turns without progress exceeds threshold."""
        base_case.turns_without_progress = 5

        metrics = calculate_progress_metrics(base_case, current_turn=6)

        assert any("progress" in reason.lower() for reason in metrics.blocked_reasons)

    def test_no_active_hypotheses_does_not_assert_a_fact_about_them(self, base_case):
        """An empty active pool averages to a density of 0.0, which must not
        be read as "nothing is linked".

        `_overall_support_density` returns 0.0 for "nothing to average", so an
        unguarded threshold makes the engine state a fact about active
        hypotheses that do not exist — next to the reason that correctly says
        there are none.
        """
        base_case.turns_without_progress = 5
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Retired theory",
                HypothesisState.RETIRED,
                0.1,
                supporting_evidence=[],
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=6)

        assert metrics.active_hypotheses_count == 0
        assert not any(
            "any active hypothesis" in reason for reason in metrics.blocked_reasons
        ), metrics.blocked_reasons
        assert any("No active hypotheses" in r for r in metrics.blocked_reasons)

    def test_starved_support_is_still_reported(self, base_case):
        """A pool with a few links and far too few is thin, not fine.

        The exact-zero wording only covers density 0.0, so without a second
        band a starved pool (1 link across two hypotheses => 0.167) produces
        no support-related reason at all.
        """
        base_case.turns_without_progress = 5
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Heap exceeds the container limit",
                HypothesisState.ACTIVE,
                0.5,
                supporting_evidence=["ev_1"],
            ),
            "hyp_000000000002": create_hypothesis(
                "hyp_000000000002",
                "Node memory pressure evicts the pod",
                HypothesisState.ACTIVE,
                0.4,
                supporting_evidence=[],
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=6)

        assert 0.0 < metrics.support_density < 0.30
        assert any(
            "thin" in reason.lower() for reason in metrics.blocked_reasons
        ), metrics.blocked_reasons
        # The COUNT contract still holds: no fabricated percentage.
        assert not any("%" in reason for reason in metrics.blocked_reasons)


class TestSupportDensityThresholds:
    """The density is a MEAN across active hypotheses, so the bands that read
    it must not let one thinly linked hypothesis speak for the pool."""

    def test_well_supported_pool_is_told_to_validate_not_collect(self, base_case):
        """Two hypotheses at 3 and 2 links (density 0.833) is a pool with
        evidence to work with — the next step is to validate it.

        Testing the mean against 1.0 instead of the 0.70 band makes every pool
        short of universally-well-supported read as "collect more evidence",
        however well supported the rest of it is.
        """
        from faultmaven.modules.case.contracts import CauseState

        base_case.progress.symptom_verified = True
        base_case.progress.cause_state = CauseState.CANDIDATES
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Heap exceeds the container limit",
                HypothesisState.ACTIVE,
                0.6,
                supporting_evidence=["ev_1", "ev_2", "ev_3"],
            ),
            "hyp_000000000002": create_hypothesis(
                "hyp_000000000002",
                "Node memory pressure evicts the pod",
                HypothesisState.ACTIVE,
                0.5,
                supporting_evidence=["ev_4", "ev_5"],
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=4)

        assert metrics.support_density > 0.80
        assert any("Validate hypotheses" in step for step in metrics.next_steps)
        assert not any("Collect more evidence" in step for step in metrics.next_steps)

    def test_thin_pool_is_told_to_collect(self, base_case):
        """The other side of the band: one link across two hypotheses is not
        yet something to validate."""
        from faultmaven.modules.case.contracts import CauseState

        base_case.progress.symptom_verified = True
        base_case.progress.cause_state = CauseState.CANDIDATES
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Heap exceeds the container limit",
                HypothesisState.ACTIVE,
                0.6,
                supporting_evidence=["ev_1"],
            ),
            "hyp_000000000002": create_hypothesis(
                "hyp_000000000002",
                "Node memory pressure evicts the pod",
                HypothesisState.ACTIVE,
                0.5,
                supporting_evidence=[],
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=4)

        assert any("Collect more evidence" in step for step in metrics.next_steps)
