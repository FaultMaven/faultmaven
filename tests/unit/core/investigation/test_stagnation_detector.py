"""Tests for StagnationDetector

Tests stagnation detection and recovery for investigation engine.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.stagnation_detector import (
    BreakoutAction,
    StagnationBreaker,
    StagnationDetector,
    StagnationType,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
    TurnOutcome,
    TurnProgress,
)


@pytest.fixture
def detector():
    return StagnationDetector()


@pytest.fixture
def breaker():
    return StagnationBreaker()


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
        ),
        progress=InvestigationProgress(),
        turns_without_progress=0,
    )


def create_turn(turn_number: int, progress_made: bool = False, actions: list = None):
    """Helper to create turn progress records."""
    return TurnProgress(
        turn_number=turn_number,
        timestamp=datetime.now(timezone.utc),
        milestones_completed=[],
        evidence_added=[],
        hypotheses_generated=[],
        hypotheses_validated=[],
        solutions_proposed=[],
        progress_made=progress_made,
        actions_taken=actions or ["analyzed"],
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary="test",
        agent_response_summary="test",
    )


class TestNoProgressDetection:
    """Test NO_PROGRESS stagnation detection."""

    def test_no_progress_detected_after_threshold(self, detector, base_case):
        """Should detect NO_PROGRESS after 3 turns without progress."""
        base_case.turns_without_progress = 3

        result = detector.detect_stagnation(base_case)

        assert result == StagnationType.NO_PROGRESS

    def test_no_stagnation_below_threshold(self, detector, base_case):
        """Should not detect stagnation below threshold."""
        base_case.turns_without_progress = 2

        result = detector.detect_stagnation(base_case)

        assert result is None

    def test_custom_threshold(self, base_case):
        """Should respect custom no_progress_threshold."""
        detector = StagnationDetector(no_progress_threshold=5)
        base_case.turns_without_progress = 4

        result = detector.detect_stagnation(base_case)

        assert result is None

        base_case.turns_without_progress = 5
        result = detector.detect_stagnation(base_case)

        assert result == StagnationType.NO_PROGRESS


class TestHypothesisAnchoringDetection:
    """Test HYPOTHESIS_ANCHORING detection."""

    def test_anchoring_detected_in_same_category(self, detector, base_case):
        """Should detect anchoring when 4+ failed hypotheses in same category."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Code hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.REFUTED,
                generated_at_turn=1,
            )
            for i in range(4)
        }

        result = detector.detect_stagnation(base_case)

        assert result == StagnationType.HYPOTHESIS_ANCHORING

    def test_no_anchoring_with_diverse_categories(self, detector, base_case):
        """Should not detect anchoring with diverse category failures."""
        categories = [
            HypothesisCategory.CODE,
            HypothesisCategory.CONFIG,
            HypothesisCategory.NETWORK,
            HypothesisCategory.DATA,
        ]
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Hypothesis {i}",
                category=categories[i],
                status=HypothesisStatus.REFUTED,
                generated_at_turn=1,
            )
            for i in range(4)
        }

        result = detector.detect_stagnation(base_case)

        # No stagnation - failures spread across categories
        assert result is None

    def test_active_hypotheses_not_counted(self, detector, base_case):
        """ACTIVE hypotheses should not count toward anchoring."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Code hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.ACTIVE,
                generated_at_turn=1,
            )
            for i in range(5)
        }

        result = detector.detect_stagnation(base_case)

        assert result is None


class TestActionLoopDetection:
    """Test ACTION_LOOP detection."""

    def test_action_loop_detected(self, detector, base_case):
        """Should detect action loop when same actions repeated."""
        # Create 5 turns with identical actions
        base_case.turn_history = [
            create_turn(i, actions=["analyzed", "verified"]) for i in range(5)
        ]

        result = detector.detect_stagnation(base_case)

        assert result == StagnationType.ACTION_LOOP

    def test_no_loop_with_varied_actions(self, detector, base_case):
        """Should not detect loop when actions vary."""
        actions_list = [
            ["analyzed"],
            ["verified"],
            ["tested"],
            ["analyzed", "confirmed"],
            ["proposed"],
        ]
        base_case.turn_history = [
            create_turn(i, actions=actions_list[i]) for i in range(5)
        ]

        result = detector.detect_stagnation(base_case)

        # No action loop
        assert result != StagnationType.ACTION_LOOP


class TestHypothesisDeadlockDetection:
    """Test HYPOTHESIS_DEADLOCK detection."""

    def test_deadlock_all_inconclusive(self, detector, base_case):
        """Should detect deadlock when all hypotheses are inconclusive."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
            )
            for i in range(4)
        }

        result = detector.detect_stagnation(base_case)

        assert result == StagnationType.HYPOTHESIS_DEADLOCK

    def test_no_deadlock_with_active_hypotheses(self, detector, base_case):
        """Should not detect deadlock if some hypotheses are active."""
        base_case.hypotheses = {
            "hyp_000000000001": Hypothesis(
                hypothesis_id="hyp_000000000001",
                statement="Hypothesis 1",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
            ),
            "hyp_000000000002": Hypothesis(
                hypothesis_id="hyp_000000000002",
                statement="Hypothesis 2",
                category=HypothesisCategory.CONFIG,
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
            ),
            "hyp_000000000003": Hypothesis(
                hypothesis_id="hyp_000000000003",
                statement="Hypothesis 3",
                category=HypothesisCategory.NETWORK,
                status=HypothesisStatus.ACTIVE,  # Not inconclusive
                generated_at_turn=1,
            ),
        }

        result = detector.detect_stagnation(base_case)

        assert result != StagnationType.HYPOTHESIS_DEADLOCK


class TestStagnationBreaker:
    """Test StagnationBreaker recovery actions."""

    def test_no_progress_enters_degraded_mode(self, breaker, base_case):
        """NO_PROGRESS should trigger degraded mode entry."""
        action = breaker.break_stagnation(base_case, StagnationType.NO_PROGRESS)

        assert action.action == "enter_degraded_mode"
        assert base_case.degraded_mode is not None

    def test_anchoring_forces_alternative_category(self, breaker, base_case):
        """HYPOTHESIS_ANCHORING should force alternative category."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Code hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.REFUTED,
                generated_at_turn=1,
            )
            for i in range(4)
        }

        action = breaker.break_stagnation(
            base_case, StagnationType.HYPOTHESIS_ANCHORING
        )

        assert action.action == "force_alternative_category"
        assert "code" in action.message.lower()
        assert action.prompt_injection is not None

    def test_action_loop_requests_user_input(self, breaker, base_case):
        """ACTION_LOOP should request user input."""
        action = breaker.break_stagnation(base_case, StagnationType.ACTION_LOOP)

        assert action.action == "request_user_input"

    def test_deadlock_retires_hypotheses(self, breaker, base_case):
        """HYPOTHESIS_DEADLOCK should retire inconclusive hypotheses."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
            )
            for i in range(3)
        }

        action = breaker.break_stagnation(base_case, StagnationType.HYPOTHESIS_DEADLOCK)

        assert action.action == "reset_hypotheses"
        # Check that hypotheses were retired
        for hyp in base_case.hypotheses.values():
            assert hyp.status == HypothesisStatus.RETIRED


class TestStagnationSummary:
    """Test stagnation summary generation."""

    def test_summary_includes_metrics(self, detector, base_case):
        """Summary should include relevant metrics."""
        base_case.turns_without_progress = 2
        base_case.hypotheses = {
            "hyp_000000000001": Hypothesis(
                hypothesis_id="hyp_000000000001",
                statement="Hypothesis 1",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
            ),
        }

        summary = detector.get_stagnation_summary(base_case)

        assert "turns_without_progress" in summary
        assert summary["turns_without_progress"] == 2
        assert "total_hypotheses" in summary
        assert summary["total_hypotheses"] == 1
        assert "is_stagnating" in summary
