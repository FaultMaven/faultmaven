"""Unit tests for OODA Engine

Tests:
- OODA step activation per phase
- Adaptive iteration intensity
- Phase-to-OODA mapping
- Iteration management
- Anchoring detection
- Progress tracking

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime

from faultmaven.core.investigation.ooda_engine import (
    OODAEngine,
    AdaptiveIntensityController,
    create_ooda_engine,
)
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    OODAStep,
    OODAIteration,
    Hypothesis,
    HypothesisStatus,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    EvidenceLayer,
    MemoryLayer,
)


@pytest.fixture
def ooda_engine():
    """Create OODA engine instance"""
    return create_ooda_engine()


@pytest.fixture
def investigation_state_phase4():
    """Create investigation state in Phase 4 (Validation)"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            current_turn=5,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.VALIDATION,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            current_iteration=0,
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )


class TestAdaptiveIntensityController:
    """Test adaptive intensity control logic"""

    def test_get_intensity_phase_0_intake(self):
        """Test intensity for Phase 0 (no OODA)"""
        controller = AdaptiveIntensityController()
        intensity = controller.get_intensity_level(0, InvestigationPhase.INTAKE)
        assert intensity == "none"

    def test_get_intensity_phase_1_blast_radius(self):
        """Test intensity for Phase 1 (always light)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.BLAST_RADIUS) == "light"
        assert controller.get_intensity_level(2, InvestigationPhase.BLAST_RADIUS) == "light"

    def test_get_intensity_phase_2_timeline(self):
        """Test intensity for Phase 2 (always light)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.TIMELINE) == "light"
        assert controller.get_intensity_level(2, InvestigationPhase.TIMELINE) == "light"

    def test_get_intensity_phase_3_hypothesis(self):
        """Test intensity for Phase 3 (light -> medium)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.HYPOTHESIS) == "light"
        assert controller.get_intensity_level(2, InvestigationPhase.HYPOTHESIS) == "light"
        assert controller.get_intensity_level(3, InvestigationPhase.HYPOTHESIS) == "medium"

    def test_get_intensity_phase_4_validation(self):
        """Test intensity for Phase 4 (medium -> full)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.VALIDATION) == "medium"
        assert controller.get_intensity_level(2, InvestigationPhase.VALIDATION) == "medium"
        assert controller.get_intensity_level(3, InvestigationPhase.VALIDATION) == "full"
        assert controller.get_intensity_level(6, InvestigationPhase.VALIDATION) == "full"

    def test_get_intensity_phase_5_solution(self):
        """Test intensity for Phase 5 (always medium)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.SOLUTION) == "medium"
        assert controller.get_intensity_level(3, InvestigationPhase.SOLUTION) == "medium"

    def test_get_intensity_phase_6_document(self):
        """Test intensity for Phase 6 (always light)"""
        controller = AdaptiveIntensityController()

        assert controller.get_intensity_level(1, InvestigationPhase.DOCUMENT) == "light"


class TestAnchoringPrevention:
    """Test anchoring bias detection"""

    def test_no_anchoring_early_iterations(self):
        """Test no anchoring detected in first 2 iterations"""
        controller = AdaptiveIntensityController()

        hypotheses = [
            Hypothesis(
                statement="Code issue",
                category="code",
                likelihood=0.7,
                initial_likelihood=0.7,
                created_at_turn=1,
                last_updated_turn=1,
            )
        ]

        should_trigger, reason = controller.should_trigger_anchoring_prevention(
            iteration_count=2,
            hypotheses=hypotheses,
        )

        assert should_trigger is False

    def test_anchoring_same_category(self):
        """Test anchoring detection when 4+ hypotheses in same category"""
        controller = AdaptiveIntensityController()

        hypotheses = [
            Hypothesis(
                statement=f"Code issue {i}",
                category="code",
                likelihood=0.7,
                initial_likelihood=0.7,
                created_at_turn=1,
                last_updated_turn=1,
                status=HypothesisStatus.TESTING,
            )
            for i in range(4)
        ]

        should_trigger, reason = controller.should_trigger_anchoring_prevention(
            iteration_count=4,
            hypotheses=hypotheses,
        )

        assert should_trigger is True
        assert ("same category" in reason.lower() or "category" in reason.lower())

    def test_anchoring_stalled_hypotheses(self):
        """Test anchoring detection when hypotheses stalled 3+ iterations"""
        controller = AdaptiveIntensityController()

        hypothesis = Hypothesis(
            statement="Stalled hypothesis",
            category="config",
            likelihood=0.7,
            initial_likelihood=0.7,
            created_at_turn=1,
            last_updated_turn=1,
            status=HypothesisStatus.TESTING,
            iterations_without_progress=3,
        )

        should_trigger, reason = controller.should_trigger_anchoring_prevention(
            iteration_count=4,
            hypotheses=[hypothesis],
        )

        assert should_trigger is True
        assert "stalled" in reason.lower()

    def test_no_anchoring_diverse_hypotheses(self):
        """Test no anchoring with diverse hypothesis categories"""
        controller = AdaptiveIntensityController()

        hypotheses = [
            Hypothesis(
                statement="Code issue",
                category="code",
                likelihood=0.7,
                initial_likelihood=0.7,
                created_at_turn=1,
                last_updated_turn=1,
                status=HypothesisStatus.TESTING,
            ),
            Hypothesis(
                statement="Config issue",
                category="config",
                likelihood=0.6,
                initial_likelihood=0.6,
                created_at_turn=1,
                last_updated_turn=1,
                status=HypothesisStatus.TESTING,
            ),
            Hypothesis(
                statement="Infrastructure issue",
                category="infrastructure",
                likelihood=0.5,
                initial_likelihood=0.5,
                created_at_turn=1,
                last_updated_turn=1,
                status=HypothesisStatus.TESTING,
            ),
        ]

        should_trigger, reason = controller.should_trigger_anchoring_prevention(
            iteration_count=4,
            hypotheses=hypotheses,
        )

        assert should_trigger is False


class TestIterationManagement:
    """Test OODA iteration lifecycle"""

    def test_start_new_iteration(self, ooda_engine, investigation_state_phase4):
        """Test starting new OODA iteration"""
        iteration = ooda_engine.start_new_iteration(investigation_state_phase4)

        assert isinstance(iteration, OODAIteration)
        assert iteration.iteration_number == 1
        assert iteration.phase == InvestigationPhase.VALIDATION
        assert iteration.started_at_turn == 5
        assert OODAStep.OBSERVE not in iteration.steps_completed

    def test_start_multiple_iterations(self, ooda_engine, investigation_state_phase4):
        """Test multiple iteration creation"""
        iter1 = ooda_engine.start_new_iteration(investigation_state_phase4)
        investigation_state_phase4.ooda_engine.current_iteration = 1

        iter2 = ooda_engine.start_new_iteration(investigation_state_phase4)

        assert iter1.iteration_number == 1
        assert iter2.iteration_number == 2

    def test_complete_iteration_with_progress(self, ooda_engine, investigation_state_phase4):
        """Test completing iteration that made progress"""
        iteration = OODAIteration(
            iteration_number=1,
            phase=InvestigationPhase.VALIDATION,
            started_at_turn=5,
            new_evidence_collected=3,
            hypotheses_tested=1,
            confidence_delta=0.15,
        )

        made_progress, insights = ooda_engine.complete_iteration(
            investigation_state_phase4,
            iteration,
        )

        assert made_progress is True
        assert iteration.made_progress is True
        assert iteration.completed_at_turn == 5

    def test_complete_iteration_no_progress(self, ooda_engine, investigation_state_phase4):
        """Test completing iteration with no progress"""
        iteration = OODAIteration(
            iteration_number=1,
            phase=InvestigationPhase.VALIDATION,
            started_at_turn=5,
            new_evidence_collected=0,
            hypotheses_tested=0,
            confidence_delta=0.0,
        )

        made_progress, insights = ooda_engine.complete_iteration(
            investigation_state_phase4,
            iteration,
        )

        assert made_progress is False
        assert iteration.stall_reason is not None


class TestOODAStepExecution:
    """Test individual OODA step execution"""

    def test_execute_observe_step(self, ooda_engine, investigation_state_phase4):
        """Test Observe step execution"""
        # Add a hypothesis with TESTING status so observe step generates information gaps
        investigation_state_phase4.ooda_engine.hypotheses.append(
            Hypothesis(
                statement="Test hypothesis for observation",
                category="code",
                likelihood=0.7,
                initial_likelihood=0.7,
                created_at_turn=3,
                last_updated_turn=4,
                status=HypothesisStatus.TESTING,
            )
        )

        result = ooda_engine.execute_observe_step(investigation_state_phase4)

        assert result["step"] == OODAStep.OBSERVE.value
        assert "information_gaps" in result
        assert len(result["information_gaps"]) > 0

    def test_execute_orient_step(self, ooda_engine, investigation_state_phase4):
        """Test Orient step execution"""
        evidence_collected = ["evidence_001", "evidence_002"]

        result = ooda_engine.execute_orient_step(
            investigation_state_phase4,
            evidence_collected,
        )

        assert result["step"] == OODAStep.ORIENT.value
        assert "insights" in result

    def test_execute_decide_step(self, ooda_engine, investigation_state_phase4):
        """Test Decide step execution"""
        # Setup: Add hypothesis to test
        investigation_state_phase4.ooda_engine.hypotheses.append(
            Hypothesis(
                statement="Database connection pool exhausted",
                category="infrastructure",
                likelihood=0.8,
                initial_likelihood=0.8,
                created_at_turn=3,
                last_updated_turn=3,
                status=HypothesisStatus.PENDING,
            )
        )

        result = ooda_engine.execute_decide_step(investigation_state_phase4)

        assert result["step"] == OODAStep.DECIDE.value
        assert result["decision"] is not None

    def test_execute_act_step(self, ooda_engine, investigation_state_phase4):
        """Test Act step execution"""
        decision = {
            "selected_hypothesis": "hyp_001",
            "decision": "test_hypothesis",
        }

        result = ooda_engine.execute_act_step(
            investigation_state_phase4,
            decision,
        )

        assert result["step"] == OODAStep.ACT.value
        assert result["action_taken"] is not None


class TestShouldContinueIterations:
    """Test iteration continuation logic"""

    def test_continue_below_minimum(self, ooda_engine, investigation_state_phase4):
        """Test continuation when below minimum iterations"""
        investigation_state_phase4.ooda_engine.current_iteration = 1

        should_continue, reason = ooda_engine.should_continue_iterations(
            investigation_state_phase4
        )

        # Phase 4 has min 3 iterations
        assert should_continue is True
        assert "Minimum iterations" in reason

    def test_stop_at_maximum(self, ooda_engine, investigation_state_phase4):
        """Test stopping at maximum iterations"""
        investigation_state_phase4.ooda_engine.current_iteration = 6

        should_continue, reason = ooda_engine.should_continue_iterations(
            investigation_state_phase4
        )

        assert should_continue is False
        assert "Max iterations" in reason

    def test_continue_no_validated_hypothesis(self, ooda_engine, investigation_state_phase4):
        """Test continuation when no hypothesis validated"""
        investigation_state_phase4.ooda_engine.current_iteration = 3
        investigation_state_phase4.ooda_engine.hypotheses.append(
            Hypothesis(
                statement="Test hypothesis",
                category="code",
                likelihood=0.6,
                initial_likelihood=0.6,
                created_at_turn=2,
                last_updated_turn=3,
                status=HypothesisStatus.TESTING,
            )
        )

        should_continue, reason = ooda_engine.should_continue_iterations(
            investigation_state_phase4
        )

        # Should continue until hypothesis validated
        assert should_continue is True
        assert "No validated hypothesis yet" in reason

    def test_stop_hypothesis_validated(self, ooda_engine, investigation_state_phase4):
        """Test stopping when hypothesis validated"""
        investigation_state_phase4.ooda_engine.current_iteration = 4
        investigation_state_phase4.ooda_engine.hypotheses.append(
            Hypothesis(
                statement="Validated hypothesis",
                category="code",
                likelihood=0.85,
                initial_likelihood=0.7,
                created_at_turn=2,
                last_updated_turn=4,
                status=HypothesisStatus.VALIDATED,
            )
        )

        should_continue, reason = ooda_engine.should_continue_iterations(
            investigation_state_phase4
        )

        assert should_continue is False
        assert "Phase objectives achieved" in reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
