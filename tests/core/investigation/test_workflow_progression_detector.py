"""Tests for Workflow Progression Detector (v3.0)

Tests detection logic for agent-initiated workflow progression:
1. should_suggest_start_investigation
2. should_suggest_mark_complete
3. should_suggest_escalation

Design Reference: WORKFLOW_PROGRESSION_IMPLEMENTATION_STATUS.md
"""

import pytest
from datetime import datetime, timezone
from faultmaven.core.investigation.workflow_progression_detector import (
    should_suggest_start_investigation,
    should_suggest_mark_complete,
    should_suggest_escalation,
)
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    InvestigationLifecycle,
    InvestigationMetadata,
    OODAEngineState,
    ProblemConfirmation,
    WorkingConclusion,
    EscalationState,
    DegradedModeType,
    Hypothesis,
    HypothesisStatus,
)


# =============================================================================
# Fixtures for Test Data
# =============================================================================

@pytest.fixture
def base_investigation_state():
    """Base investigation state with minimal setup"""
    return InvestigationState(
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.INTAKE,
            case_status="active",
        ),
        metadata=InvestigationMetadata(
            current_turn=1,
            started_at=datetime.now(timezone.utc),
        ),
        ooda_engine=OODAEngineState(
            ooda_active=False,
        ),
    )


# =============================================================================
# Start Investigation Detection Tests
# =============================================================================

class TestShouldSuggestStartInvestigation:
    """Test detection of when to suggest starting systematic investigation"""

    def test_multi_turn_conversation_indicator(self, base_investigation_state):
        """Test detection based on multi-turn conversation (≥5 turns)"""
        base_investigation_state.metadata.current_turn = 5

        should_suggest, indicators = should_suggest_start_investigation(
            base_investigation_state,
            conversation_turn=5,
        )

        assert "multi_turn_conversation" in indicators
        # Need 2+ indicators to suggest
        # Just multi-turn alone is not enough

    def test_multiple_indicators_triggers_suggestion(self, base_investigation_state):
        """Test that 2+ indicators triggers suggestion"""
        # Set up: multi-turn + problem confirmation (complexity detected)
        base_investigation_state.metadata.current_turn = 5
        base_investigation_state.problem_confirmation = ProblemConfirmation(
            problem_statement="Database timeout",
            severity="high",
            investigation_approach="systematic",
        )

        should_suggest, indicators = should_suggest_start_investigation(
            base_investigation_state,
            conversation_turn=5,
        )

        # Should have at least 2 indicators
        assert len(indicators) >= 2
        assert should_suggest

    def test_unclear_scope_indicator(self, base_investigation_state):
        """Test detection when scope unclear after several turns"""
        base_investigation_state.metadata.current_turn = 4
        # No anomaly_frame means unclear scope
        base_investigation_state.ooda_engine.anomaly_frame = None

        should_suggest, indicators = should_suggest_start_investigation(
            base_investigation_state,
            conversation_turn=4,
        )

        assert "unclear_scope" in indicators

    def test_early_turns_no_suggestion(self, base_investigation_state):
        """Test that early turns (1-2) don't trigger suggestion"""
        base_investigation_state.metadata.current_turn = 2

        should_suggest, indicators = should_suggest_start_investigation(
            base_investigation_state,
            conversation_turn=2,
        )

        # Should have minimal indicators
        assert not should_suggest or len(indicators) < 2

    def test_single_indicator_not_enough(self, base_investigation_state):
        """Test that single indicator doesn't trigger suggestion"""
        # Only set multi-turn
        base_investigation_state.metadata.current_turn = 6

        should_suggest, indicators = should_suggest_start_investigation(
            base_investigation_state,
            conversation_turn=6,
        )

        # Has indicator but not enough
        assert "multi_turn_conversation" in indicators
        assert len(indicators) == 1
        assert not should_suggest


# =============================================================================
# Mark Complete Detection Tests
# =============================================================================

class TestShouldSuggestMarkComplete:
    """Test detection of when to suggest marking investigation complete"""

    def test_requires_phase_6(self, base_investigation_state):
        """Test that suggestion only happens in Phase 5 (SOLUTION)"""
        # Set up complete investigation but in wrong phase
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION  # Wrong - needs Phase 5
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Memory leak in cache",
            confidence=0.85,
            can_proceed_with_solution=True,
        )

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert not should_suggest
        assert details is None

    def test_requires_high_confidence(self, base_investigation_state):
        """Test that ≥70% confidence is required"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Test conclusion",
            confidence=0.65,  # Below threshold
            can_proceed_with_solution=True,
        )
        base_investigation_state.lifecycle.solution_verified = True

        # Add validated hypothesis
        base_investigation_state.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Test hypothesis",
                likelihood=0.65,
                status=HypothesisStatus.VALIDATED,
            )
        ]

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        # Low confidence should prevent suggestion
        assert not should_suggest

    def test_requires_can_proceed_with_solution(self, base_investigation_state):
        """Test that can_proceed_with_solution must be True"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Test conclusion",
            confidence=0.85,
            can_proceed_with_solution=False,  # Blocked
        )
        base_investigation_state.lifecycle.solution_verified = True

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert not should_suggest

    def test_requires_solution_verified(self, base_investigation_state):
        """Test that solution must be verified"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Test conclusion",
            confidence=0.85,
            can_proceed_with_solution=True,
        )
        base_investigation_state.lifecycle.solution_verified = False  # Not verified

        # Add validated hypothesis
        base_investigation_state.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Test hypothesis",
                likelihood=0.85,
                status=HypothesisStatus.VALIDATED,
            )
        ]

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert not should_suggest

    def test_requires_validated_hypothesis(self, base_investigation_state):
        """Test that validated hypothesis with ≥70% is required"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Test conclusion",
            confidence=0.85,
            can_proceed_with_solution=True,
        )
        base_investigation_state.lifecycle.solution_verified = True

        # No validated hypotheses
        base_investigation_state.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Test hypothesis",
                likelihood=0.60,  # Too low
                status=HypothesisStatus.ACTIVE,  # Not validated
            )
        ]

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert not should_suggest

    def test_all_conditions_met_suggests_completion(self, base_investigation_state):
        """Test that all conditions met triggers suggestion"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Memory leak in cache module",
            confidence=0.85,
            can_proceed_with_solution=True,
        )
        base_investigation_state.lifecycle.solution_verified = True
        base_investigation_state.lifecycle.solution_summary = "Fixed cache cleanup"
        base_investigation_state.lifecycle.verification_details = "Memory stable for 24h"

        # Add validated hypothesis
        base_investigation_state.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Memory leak in cache module",
                likelihood=0.85,
                status=HypothesisStatus.VALIDATED,
            )
        ]

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert should_suggest
        assert details is not None
        assert details["root_cause"] == "Memory leak in cache module"
        assert details["confidence_level"] == 0.85

# =============================================================================
# Suggest Escalation Detection Tests
# =============================================================================

class TestShouldSuggestEscalation:
    """Test detection of when to suggest escalation/closure"""

    def test_hypothesis_space_exhausted_triggers(self, base_investigation_state):
        """Test that hypothesis space exhausted triggers escalation"""
        base_investigation_state.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED,
            degraded_mode_explanation="All hypotheses tested",
        )

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        assert should_suggest
        assert details is not None
        assert details["limitation_type"] == "Hypothesis Space Exhausted"
        assert "hypotheses" in details["limitation_explanation"].lower()

    def test_degraded_mode_6_turns_triggers(self, base_investigation_state):
        """Test that 6+ turns in degraded mode triggers escalation"""
        base_investigation_state.metadata.current_turn = 20
        base_investigation_state.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.CRITICAL_EVIDENCE_MISSING,
            entered_at_turn=14,  # 6 turns ago
            degraded_mode_explanation="Cannot access logs",
        )

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        assert should_suggest
        assert details is not None
        assert "extended period" in details["limitation_explanation"].lower()

    def test_degraded_mode_less_than_6_turns_no_trigger(self, base_investigation_state):
        """Test that <6 turns in degraded mode doesn't trigger"""
        base_investigation_state.metadata.current_turn = 18
        base_investigation_state.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.EXPERTISE_REQUIRED,
            entered_at_turn=15,  # Only 3 turns ago
        )

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        # Should not trigger yet (only for HYPOTHESIS_SPACE_EXHAUSTED)
        # Other degraded modes need 6+ turns

    def test_max_loop_backs_triggers(self, base_investigation_state):
        """Test that 3 loop-backs triggers escalation"""
        base_investigation_state.lifecycle.loop_back_count = 3

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        assert should_suggest
        assert details is not None
        assert "Loop-Back" in details["limitation_type"] or "loop" in details["limitation_type"].lower()
        assert "3" in details["limitation_explanation"]

    def test_normal_mode_no_escalation(self, base_investigation_state):
        """Test that normal operation doesn't trigger escalation"""
        base_investigation_state.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=False,
        )
        base_investigation_state.lifecycle.loop_back_count = 1

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        assert not should_suggest
        assert details is None

    def test_escalation_recommendations_included(self, base_investigation_state):
        """Test that escalation details include recommendations"""
        base_investigation_state.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.EXPERTISE_REQUIRED,
        )
        base_investigation_state.metadata.current_turn = 20
        base_investigation_state.lifecycle.escalation_state.entered_at_turn = 10

        should_suggest, details = should_suggest_escalation(base_investigation_state)

        assert should_suggest
        assert "next_steps_recommendations" in details
        assert len(details["next_steps_recommendations"]) > 0
        # Expertise required should suggest specialist
        assert any("specialist" in rec.lower() or "expert" in rec.lower()
                   for rec in details["next_steps_recommendations"])


# =============================================================================
# Edge Cases and Integration Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    def test_no_working_conclusion(self, base_investigation_state):
        """Test behavior when working conclusion is None"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = None

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert not should_suggest

    def test_exact_70_percent_confidence(self, base_investigation_state):
        """Test that exactly 70% confidence is acceptable"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
            statement="Test",
            confidence=0.70,  # Exactly at threshold
            can_proceed_with_solution=True,
        )
        base_investigation_state.lifecycle.solution_verified = True

        base_investigation_state.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Test",
                likelihood=0.70,  # Exactly at threshold
                status=HypothesisStatus.VALIDATED,
            )
        ]

        should_suggest, details = should_suggest_mark_complete(base_investigation_state)

        assert should_suggest

    def test_partial_completeness_blocks_completion(self, base_investigation_state):
        """Test that PARTIAL completeness blocks completion suggestion"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        base_investigation_state.lifecycle.working_conclusion = WorkingConclusion(
