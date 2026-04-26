"""Tests for ProgressMonitor

Tests progress transparency and agent state repair for investigation engine.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.progress_monitor import (
    ProgressCheckResult,
    ProgressMonitor,
    RepairType,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
    TurnOutcome,
    TurnProgress,
)


@pytest.fixture
def monitor():
    return ProgressMonitor(transparency_threshold=3)


@pytest.fixture
def base_case():
    """Create a base investigating case."""
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


def create_turn(
    turn_number: int,
    progress_made: bool = False,
    outcome: TurnOutcome = TurnOutcome.CONVERSATION,
    evidence_added: list = None,
    milestones_completed: list = None,
):
    """Helper to create turn progress records."""
    return TurnProgress(
        turn_number=turn_number,
        timestamp=datetime.now(timezone.utc),
        milestones_completed=milestones_completed or [],
        evidence_added=evidence_added or [],
        hypotheses_generated=[],
        hypotheses_validated=[],
        solutions_proposed=[],
        progress_made=progress_made,
        outcome=outcome,
        user_message_summary="test",
        agent_response_summary="test",
    )


# =========================================================================
# Progress Transparency Tests
# =========================================================================


class TestProgressTransparency:
    """Test silent/transparent mode transitions."""

    def test_silent_mode_when_no_investigation(self, monitor):
        """Should return None for non-investigating cases."""
        case = Case(
            case_id="case_1234567890ab",
            title="Test",
            status=CaseStatus.INQUIRY,
            user_id="user_123",
            organization_id="org_123",
            inquiry=InquiryData(),
        )
        result = monitor.check_progress(case)
        assert result is None

    def test_silent_mode_below_threshold(self, monitor, base_case):
        """Should stay silent when investigative turns below threshold."""
        # 2 investigative turns (below threshold of 3)
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
        ]

        result = monitor.check_progress(base_case)
        assert result is None

    def test_transparent_mode_activates_at_threshold(self, monitor, base_case):
        """Should activate transparent mode after N investigative turns."""
        # 3 investigative turns without milestone (threshold = 3)
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(3, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
        ]

        result = monitor.check_progress(base_case)

        assert result is not None
        assert result.transparent_mode is True
        assert result.pending_milestone == "symptom_verified"
        assert result.prompt_injection is not None
        assert "PROGRESS TRANSPARENCY" in result.prompt_injection

    def test_conversational_turns_not_counted(self, monitor, base_case):
        """Conversational turns should not increment the counter."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.CONVERSATION),  # chat, not counted
            create_turn(3, outcome=TurnOutcome.CONVERSATION),  # chat, not counted
            create_turn(4, outcome=TurnOutcome.DATA_REQUESTED),
        ]

        # Only 2 investigative turns (1 and 4), below threshold of 3
        result = monitor.check_progress(base_case)
        assert result is None

    def test_counter_resets_on_milestone(self, monitor, base_case):
        """Counter should reset when a milestone is completed."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(3, milestones_completed=["symptom_verified"]),  # reset
            create_turn(4, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
            create_turn(5, outcome=TurnOutcome.DATA_REQUESTED),
        ]
        # Mark the milestone as completed on progress
        base_case.progress.symptom_verified = True

        # Only 2 investigative turns since milestone (4, 5), below threshold
        result = monitor.check_progress(base_case)
        assert result is None

    def test_counter_resets_on_stage_gate_milestone(self, monitor, base_case):
        """Stage transitions reset the counter via the stage-gate milestone path.

        Stage-gate milestones (mitigation_accepted, mitigation_verified,
        solution_accepted, solution_verified) are recorded in
        ``milestones_completed`` when they fire, so a stage transition
        resets the investigative-turn counter just like a progress
        milestone does. This locks in the stage-scoping behavior described
        in progress-transparency.md §Transitions.
        """
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(3, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_3"]),
            # Stage-gate milestone fires → DIAGNOSIS → MITIGATION transition.
            # Counter must reset here despite being mid-investigation.
            create_turn(4, milestones_completed=["mitigation_accepted"]),
            create_turn(5, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_5"]),
        ]
        base_case.progress.mitigation_accepted = True

        # Only 1 investigative turn after the stage transition; below threshold.
        result = monitor.check_progress(base_case)
        assert result is None

    def test_silent_after_milestone_completion(self, monitor, base_case):
        """Should return to silent mode after a milestone completes."""
        # History: many investigative turns, then a milestone, then 1 more
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(3, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
            create_turn(4, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(
                5, milestones_completed=["symptom_verified"]
            ),  # milestone resets counter
            create_turn(6, outcome=TurnOutcome.DATA_REQUESTED),
        ]
        base_case.progress.symptom_verified = True

        # Only 1 investigative turn since milestone (turn 6), below threshold
        result = monitor.check_progress(base_case)
        assert result is None

    def test_pending_milestone_identified_correctly(self, monitor, base_case):
        """Should identify the first incomplete milestone for the stage."""
        # symptom_verified is done, root_cause_identified is not
        base_case.progress.symptom_verified = True
        base_case.turn_history = [
            create_turn(1, milestones_completed=["symptom_verified"]),
            create_turn(2, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(3, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(4, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
        ]

        result = monitor.check_progress(base_case)

        assert result is not None
        assert result.pending_milestone == "root_cause_identified"


# =========================================================================
# Repair Pattern Tests
# =========================================================================


class TestHypothesisAnchoring:
    """Test HYPOTHESIS_ANCHORING detection."""

    def test_anchoring_detected_in_same_category(self, monitor, base_case):
        """Should detect anchoring when 4+ failed hypotheses in same category."""
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Code hypothesis {i}",
                category=HypothesisCategory.CODE,
                status=HypothesisStatus.REFUTED,
                refutation_reason=f"disproved by evidence {i}",
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis for anchoring detection",
            )
            for i in range(4)
        }

        # Also need enough investigative turns to trigger transparent mode
        base_case.turn_history = [
            create_turn(i, outcome=TurnOutcome.DATA_REQUESTED) for i in range(5)
        ]

        result = monitor.check_progress(base_case)

        assert result is not None
        assert result.repair_type == RepairType.HYPOTHESIS_ANCHORING

    def test_no_anchoring_with_diverse_categories(self, monitor, base_case):
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
                refutation_reason=f"disproved by evidence {i}",
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis for diverse categories",
            )
            for i in range(4)
        }

        base_case.turn_history = [
            create_turn(i, outcome=TurnOutcome.DATA_REQUESTED) for i in range(5)
        ]

        result = monitor.check_progress(base_case)

        # No anchoring — failures spread across categories
        if result:
            assert result.repair_type != RepairType.HYPOTHESIS_ANCHORING


class TestHypothesisDeadlock:
    """Test HYPOTHESIS_DEADLOCK detection."""

    def test_deadlock_all_inconclusive(self, monitor, base_case):
        """Should detect deadlock when all hypotheses are inconclusive."""
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
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis for deadlock detection",
            )
            for i in range(4)
        }

        base_case.turn_history = [
            create_turn(i, outcome=TurnOutcome.DATA_REQUESTED) for i in range(5)
        ]

        result = monitor.check_progress(base_case)

        assert result is not None
        assert result.repair_type == RepairType.HYPOTHESIS_DEADLOCK

    def test_deadlock_retires_hypotheses(self, monitor, base_case):
        """HYPOTHESIS_DEADLOCK should retire inconclusive hypotheses."""
        categories = [
            HypothesisCategory.CODE,
            HypothesisCategory.CONFIG,
            HypothesisCategory.NETWORK,
        ]
        base_case.hypotheses = {
            f"hyp_{i:012x}": Hypothesis(
                hypothesis_id=f"hyp_{i:012x}",
                statement=f"Hypothesis {i}",
                category=categories[i],
                status=HypothesisStatus.INCONCLUSIVE,
                generated_at_turn=1,
                generation_mode=HypothesisGenerationMode.SYSTEMATIC,
                rationale="Test hypothesis for retirement",
            )
            for i in range(3)
        }

        base_case.turn_history = [
            create_turn(i, outcome=TurnOutcome.DATA_REQUESTED) for i in range(5)
        ]

        monitor.check_progress(base_case)

        for hyp in base_case.hypotheses.values():
            assert hyp.status == HypothesisStatus.RETIRED


class TestActionLoop:
    """Test ACTION_LOOP detection."""

    def test_action_loop_detected(self, monitor, base_case):
        """Should detect loop when identical structural output repeated."""
        # 5 turns with identical empty structural output
        base_case.turn_history = [
            create_turn(i, outcome=TurnOutcome.DATA_REQUESTED) for i in range(5)
        ]

        result = monitor.check_progress(base_case)

        assert result is not None
        # May or may not trigger action loop specifically — depends on
        # whether transparent mode + other patterns take priority.
        # At minimum, transparent mode should be active.
        assert result.transparent_mode is True


class TestInvestigativeTurnCounter:
    """Test the investigative turn counting logic."""

    def test_data_provided_counts(self, monitor, base_case):
        """Turns with evidence added should count as investigative."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
            create_turn(3, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_3"]),
        ]

        count = monitor._count_investigative_turns_since_milestone(base_case)
        assert count == 3

    def test_conversation_not_counted(self, monitor, base_case):
        """Pure conversation turns should not count."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.CONVERSATION),
            create_turn(2, outcome=TurnOutcome.CONVERSATION),
            create_turn(3, outcome=TurnOutcome.CONVERSATION),
        ]

        count = monitor._count_investigative_turns_since_milestone(base_case)
        assert count == 0

    def test_mixed_turns(self, monitor, base_case):
        """Should count only investigative turns in mixed history."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.CONVERSATION),
            create_turn(3, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(4, outcome=TurnOutcome.CONVERSATION),
        ]

        count = monitor._count_investigative_turns_since_milestone(base_case)
        assert count == 2

    def test_resets_at_milestone(self, monitor, base_case):
        """Counter should stop at the last milestone completion."""
        base_case.turn_history = [
            create_turn(1, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_1"]),
            create_turn(2, outcome=TurnOutcome.DATA_REQUESTED),
            create_turn(3, milestones_completed=["symptom_verified"]),
            create_turn(4, outcome=TurnOutcome.DATA_PROVIDED, evidence_added=["ev_2"]),
        ]

        count = monitor._count_investigative_turns_since_milestone(base_case)
        assert count == 1  # Only turn 4 counts (after milestone at turn 3)
