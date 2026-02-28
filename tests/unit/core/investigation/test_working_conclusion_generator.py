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
    CaseStatus,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
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
        status=CaseStatus.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
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
    status: HypothesisStatus = HypothesisStatus.ACTIVE,
    likelihood: float = 0.5,
    supporting_evidence: list = None,
    generated_at_turn: int = 1,
) -> Hypothesis:
    """Helper to create a hypothesis."""
    hyp = Hypothesis(
        hypothesis_id=hyp_id,
        statement=statement,
        category=HypothesisCategory.CODE,
        status=status,
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
            hyp.evidence_links[ev_id] = HypothesisEvidenceLink(
                hypothesis_id=hyp_id,
                evidence_id=ev_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="Test supporting evidence",
                stance_confidence=0.8,
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
        actions_taken=["analyzed"],
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
                "hyp_000000000001", "Low likelihood", HypothesisStatus.ACTIVE, 0.3
            ),
            "hyp_000000000002": create_hypothesis(
                "hyp_000000000002",
                "High likelihood",
                HypothesisStatus.ACTIVE,
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
                HypothesisStatus.ACTIVE,
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
                "hyp_000000000001", "Low confidence", HypothesisStatus.ACTIVE, 0.3
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
            create_turn(3, milestones_completed=["scope_assessed"]),
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

    def test_blocked_momentum_with_degraded_mode(self, base_case):
        """Should return BLOCKED momentum when in degraded mode."""
        from faultmaven.modules.case.contracts import DegradedMode, DegradedModeType

        base_case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType.NO_PROGRESS,
            reason="Test degraded",
            entered_at=datetime.now(timezone.utc),
        )

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert metrics.investigation_momentum == InvestigationMomentum.BLOCKED


class TestEvidenceCompleteness:
    """Test evidence completeness calculation."""

    def test_calculates_completeness_from_evidence(self, base_case):
        """Should calculate completeness from supporting evidence count."""
        base_case.hypotheses = {
            "hyp_000000000001": create_hypothesis(
                "hyp_000000000001",
                "Test",
                HypothesisStatus.ACTIVE,
                0.5,
                supporting_evidence=["ev_1", "ev_2"],  # 2 out of typical 3
            ),
        }

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        # 2/3 = 0.67 completeness
        assert 0.6 <= metrics.evidence_completeness <= 0.7


class TestNextStepsGeneration:
    """Test next steps suggestion."""

    def test_suggests_verify_symptom_first(self, base_case):
        """Should suggest symptom verification when not done."""
        base_case.progress.symptom_verified = False

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert any("symptom" in step.lower() for step in metrics.next_steps)

    def test_suggests_scope_after_symptom(self, base_case):
        """Should suggest scope assessment after symptom verified."""
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = False

        metrics = calculate_progress_metrics(base_case, current_turn=1)

        assert any("scope" in step.lower() for step in metrics.next_steps)

    def test_suggests_solution_after_root_cause(self, base_case):
        """Should suggest solution after root cause identified."""
        base_case.progress.symptom_verified = True
        base_case.progress.scope_assessed = True
        base_case.progress.timeline_established = True
        base_case.progress.changes_identified = True
        base_case.progress.root_cause_identified = True
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

    def test_includes_degraded_mode_reason(self, base_case):
        """Should include degraded mode reason when present."""
        from faultmaven.modules.case.contracts import DegradedMode, DegradedModeType

        base_case.turns_without_progress = 5
        base_case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType.NO_PROGRESS,
            reason="Custom degraded reason",
            entered_at=datetime.now(timezone.utc),
        )

        metrics = calculate_progress_metrics(base_case, current_turn=4)

        assert any("Custom degraded" in reason for reason in metrics.blocked_reasons)
