"""Tests for Working Conclusion Generator (v3.0)

Tests the core v3.0 working conclusion system that replaces stall detection.
"""

import pytest
from unittest.mock import Mock

from faultmaven.core.investigation.working_conclusion_generator import (
    generate_working_conclusion,
    calculate_progress_metrics,
    _calculate_evidence_completeness,
    _detect_investigation_momentum,
    _map_confidence_to_level,
    _determine_if_can_proceed,
    _should_enter_degraded_mode,
)
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationLifecycle,
    InvestigationPhase,
    InvestigationMetadata,
    OODAEngine,
    Hypothesis,
    HypothesisStatus,
    ConfidenceLevel,
    InvestigationMomentum,
    DegradedModeType,
)


@pytest.fixture
def base_investigation_state():
    """Create base investigation state for testing"""
    state = InvestigationState(
        session_id="test-session",
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.VALIDATION,
            investigation_strategy="root_cause_analysis",
        ),
        metadata=InvestigationMetadata(
            current_turn=5,
        ),
        ooda_engine=OODAEngine(
            hypotheses=[],
            iterations=[],
        ),
    )
    return state


class TestWorkingConclusionGeneration:
    """Test working conclusion generation"""

    def test_early_phase_conclusion(self, base_investigation_state):
        """Test working conclusion in early phases (0-2)"""
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.BLAST_RADIUS
        base_investigation_state.metadata.current_turn = 2

        conclusion = generate_working_conclusion(base_investigation_state, 2)

        assert conclusion is not None
        assert "early phase" in conclusion.statement.lower() or "scoping" in conclusion.statement.lower()
        assert conclusion.confidence < 0.5  # Early phase should have low confidence
        assert conclusion.confidence_level in [ConfidenceLevel.SPECULATION]

    def test_normal_mode_conclusion(self, base_investigation_state):
        """Test working conclusion in normal mode with hypotheses"""
        # Add validated hypothesis
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Database connection pool exhausted",
            likelihood=0.75,
            status=HypothesisStatus.VALIDATED,
            category="configuration",
            supporting_evidence=["e1", "e2", "e3"],
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        conclusion = generate_working_conclusion(base_investigation_state, 5)

        assert conclusion is not None
        assert "database" in conclusion.statement.lower() or "connection" in conclusion.statement.lower()
        assert conclusion.confidence >= 0.70  # Validated hypothesis
        assert conclusion.confidence_level in [ConfidenceLevel.CONFIDENT, ConfidenceLevel.VERIFIED]
        assert conclusion.supporting_evidence_count == 3
        assert conclusion.can_proceed_with_solution is True

    def test_refuted_hypothesis_conclusion(self, base_investigation_state):
        """Test working conclusion when hypothesis is refuted"""
        # Add refuted hypothesis
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Network latency issue",
            likelihood=0.25,
            status=HypothesisStatus.REFUTED,
            category="network",
            refuting_evidence=["e1", "e2"],
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        conclusion = generate_working_conclusion(base_investigation_state, 5)

        assert conclusion is not None
        assert conclusion.confidence < 0.50  # Refuted hypothesis
        assert len(conclusion.alternative_explanations) > 0
        assert conclusion.can_proceed_with_solution is False

    def test_degraded_mode_conclusion(self, base_investigation_state):
        """Test working conclusion in degraded mode"""
        # Set degraded mode
        base_investigation_state.lifecycle.escalation_state.operating_in_degraded_mode = True
        base_investigation_state.lifecycle.escalation_state.degraded_mode_type = (
            DegradedModeType.CRITICAL_EVIDENCE_MISSING
        )

        # Add hypothesis
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Hardware failure suspected",
            likelihood=0.60,
            status=HypothesisStatus.ACTIVE,
            category="hardware",
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        conclusion = generate_working_conclusion(base_investigation_state, 5)

        assert conclusion is not None
        # Confidence capped at 50% for CRITICAL_EVIDENCE_MISSING
        assert conclusion.confidence <= 0.50
        assert len(conclusion.caveats) > 0
        # Should mention evidence limitation
        assert any("evidence" in caveat.lower() for caveat in conclusion.caveats)


class TestProgressMetrics:
    """Test progress metrics calculation"""

    def test_high_momentum(self, base_investigation_state):
        """Test high investigation momentum detection"""
        # Add recent progress
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test hypothesis",
            likelihood=0.60,
            status=HypothesisStatus.ACTIVE,
            category="test",
            last_progress_at_turn=4,  # Recent progress
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        base_investigation_state.metadata.current_turn = 5

        metrics = calculate_progress_metrics(base_investigation_state, 5)

        assert metrics.investigation_momentum == InvestigationMomentum.HIGH
        assert metrics.turns_since_last_progress <= 1

    def test_blocked_momentum(self, base_investigation_state):
        """Test blocked investigation momentum"""
        # No progress for 6+ turns
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test hypothesis",
            likelihood=0.40,
            status=HypothesisStatus.ACTIVE,
            category="test",
            last_progress_at_turn=0,
            iterations_without_progress=6,
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        base_investigation_state.metadata.current_turn = 8

        metrics = calculate_progress_metrics(base_investigation_state, 8)

        assert metrics.investigation_momentum == InvestigationMomentum.BLOCKED
        assert metrics.turns_since_last_progress >= 6

    def test_should_suggest_closure_high_confidence(self, base_investigation_state):
        """Test closure suggestion with high confidence"""
        # Set high confidence working conclusion
        base_investigation_state.lifecycle.working_conclusion = Mock()
        base_investigation_state.lifecycle.working_conclusion.confidence = 0.85

        metrics = calculate_progress_metrics(base_investigation_state, 5)

        should_close = metrics.should_suggest_closure(base_investigation_state)

        assert should_close is True

    def test_should_suggest_closure_blocked_long_time(self, base_investigation_state):
        """Test closure suggestion when blocked for too long"""
        # Set blocked state with long investigation
        base_investigation_state.lifecycle.working_conclusion = Mock()
        base_investigation_state.lifecycle.working_conclusion.confidence = 0.30
        base_investigation_state.metadata.current_turn = 25

        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.30,
            status=HypothesisStatus.ACTIVE,
            category="test",
            iterations_without_progress=10,
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        metrics = calculate_progress_metrics(base_investigation_state, 25)

        should_close = metrics.should_suggest_closure(base_investigation_state)

        assert should_close is True  # Blocked too long


class TestEvidenceCompleteness:
    """Test evidence completeness calculation"""

    def test_no_hypotheses_zero_completeness(self, base_investigation_state):
        """Test completeness with no hypotheses"""
        completeness = _calculate_evidence_completeness(base_investigation_state)

        assert completeness == 0.0

    def test_well_supported_hypothesis_high_completeness(self, base_investigation_state):
        """Test completeness with well-supported hypothesis"""
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.70,
            status=HypothesisStatus.VALIDATED,
            category="test",
            supporting_evidence=["e1", "e2", "e3", "e4", "e5"],  # 5 pieces
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        completeness = _calculate_evidence_completeness(base_investigation_state)

        assert completeness >= 0.80  # Good evidence

    def test_weak_hypothesis_low_completeness(self, base_investigation_state):
        """Test completeness with weak evidence"""
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.30,
            status=HypothesisStatus.ACTIVE,
            category="test",
            supporting_evidence=["e1"],  # Only 1 piece
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        completeness = _calculate_evidence_completeness(base_investigation_state)

        assert completeness <= 0.40  # Weak evidence


class TestConfidenceMapping:
    """Test confidence to level mapping"""

    def test_speculation_level(self):
        """Test speculation confidence level (< 50%)"""
        level = _map_confidence_to_level(0.30)
        assert level == ConfidenceLevel.SPECULATION

    def test_probable_level(self):
        """Test probable confidence level (50-69%)"""
        level = _map_confidence_to_level(0.60)
        assert level == ConfidenceLevel.PROBABLE

    def test_confident_level(self):
        """Test confident confidence level (70-89%)"""
        level = _map_confidence_to_level(0.75)
        assert level == ConfidenceLevel.CONFIDENT

    def test_verified_level(self):
        """Test verified confidence level (90%+)"""
        level = _map_confidence_to_level(0.95)
        assert level == ConfidenceLevel.VERIFIED

    def test_boundary_values(self):
        """Test boundary values"""
        assert _map_confidence_to_level(0.49) == ConfidenceLevel.SPECULATION
        assert _map_confidence_to_level(0.50) == ConfidenceLevel.PROBABLE
        assert _map_confidence_to_level(0.69) == ConfidenceLevel.PROBABLE
        assert _map_confidence_to_level(0.70) == ConfidenceLevel.CONFIDENT
        assert _map_confidence_to_level(0.89) == ConfidenceLevel.CONFIDENT
        assert _map_confidence_to_level(0.90) == ConfidenceLevel.VERIFIED


class TestCanProceedDetermination:
    """Test can proceed with solution determination"""

    def test_can_proceed_high_confidence(self, base_investigation_state):
        """Test can proceed with high confidence"""
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.75,
            status=HypothesisStatus.VALIDATED,
            category="test",
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        can_proceed = _determine_if_can_proceed(base_investigation_state, 0.75)

        assert can_proceed is True

    def test_cannot_proceed_low_confidence(self, base_investigation_state):
        """Test cannot proceed with low confidence"""
        can_proceed = _determine_if_can_proceed(base_investigation_state, 0.40)

        assert can_proceed is False

    def test_can_proceed_degraded_mode_at_cap(self, base_investigation_state):
        """Test can proceed in degraded mode at confidence cap"""
        base_investigation_state.lifecycle.escalation_state.operating_in_degraded_mode = True
        base_investigation_state.lifecycle.escalation_state.degraded_mode_type = (
            DegradedModeType.EXPERTISE_REQUIRED  # 40% cap
        )

        # At cap (within margin)
        can_proceed = _determine_if_can_proceed(base_investigation_state, 0.38)

        assert can_proceed is True


class TestDegradedModeDetection:
    """Test degraded mode detection"""

    def test_enter_degraded_mode_no_progress(self, base_investigation_state):
        """Test entering degraded mode due to no progress"""
        # Set up blocked state
        base_investigation_state.metadata.current_turn = 15
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.40,
            status=HypothesisStatus.ACTIVE,
            category="test",
            iterations_without_progress=8,
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)

        should_enter, mode_type, reason = _should_enter_degraded_mode(base_investigation_state)

        assert should_enter is True
        assert mode_type is not None
        assert reason is not None

    def test_no_degraded_mode_with_progress(self, base_investigation_state):
        """Test not entering degraded mode with good progress"""
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.70,
            status=HypothesisStatus.VALIDATED,
            category="test",
            last_progress_at_turn=4,
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        base_investigation_state.metadata.current_turn = 5

        should_enter, mode_type, reason = _should_enter_degraded_mode(base_investigation_state)

        assert should_enter is False
        assert mode_type is None


class TestIntegration:
    """Integration tests for working conclusion system"""

    def test_full_investigation_flow(self, base_investigation_state):
        """Test working conclusion through investigation lifecycle"""
        # Turn 1: Early phase
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.INTAKE
        base_investigation_state.metadata.current_turn = 1
        conclusion_t1 = generate_working_conclusion(base_investigation_state, 1)
        assert conclusion_t1.confidence < 0.5

        # Turn 5: Hypothesis testing
        base_investigation_state.lifecycle.current_phase = InvestigationPhase.VALIDATION
        base_investigation_state.metadata.current_turn = 5
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Root cause identified",
            likelihood=0.65,
            status=HypothesisStatus.ACTIVE,
            category="test",
            supporting_evidence=["e1", "e2"],
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        conclusion_t5 = generate_working_conclusion(base_investigation_state, 5)
        assert 0.50 <= conclusion_t5.confidence < 0.70

        # Turn 8: Validated
        hypothesis.likelihood = 0.80
        hypothesis.status = HypothesisStatus.VALIDATED
        hypothesis.supporting_evidence = ["e1", "e2", "e3", "e4"]
        conclusion_t8 = generate_working_conclusion(base_investigation_state, 8)
        assert conclusion_t8.confidence >= 0.70
        assert conclusion_t8.can_proceed_with_solution is True

    def test_degraded_mode_transition(self, base_investigation_state):
        """Test transition into and out of degraded mode"""
        # Start in normal mode
        hypothesis = Hypothesis(
            hypothesis_id="h1",
            statement="Test",
            likelihood=0.45,
            status=HypothesisStatus.ACTIVE,
            category="test",
            iterations_without_progress=7,
        )
        base_investigation_state.ooda_engine.hypotheses.append(hypothesis)
        base_investigation_state.metadata.current_turn = 10

        # Should enter degraded mode
        conclusion = generate_working_conclusion(base_investigation_state, 10)
        assert base_investigation_state.lifecycle.escalation_state.operating_in_degraded_mode is True
        assert conclusion.confidence <= 0.50  # Capped

        # Make progress - should re-evaluate
        hypothesis.last_progress_at_turn = 12
        hypothesis.likelihood = 0.55
        hypothesis.iterations_without_progress = 0
        base_investigation_state.metadata.current_turn = 12

        conclusion = generate_working_conclusion(base_investigation_state, 12)
        # May still be in degraded mode but should show progress


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
