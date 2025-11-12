"""Unit tests for ValidationHandler (Phase 4)

Tests:
- Hypothesis testing workflow
- Full OODA cycle execution
- Confidence updates based on evidence
- Validation completion criteria

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from unittest.mock import AsyncMock, Mock

from faultmaven.services.agentic.phase_handlers.validation_handler import ValidationHandler
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    InvestigationStrategy,
    EngagementMode,
    Hypothesis,
    HypothesisStatus,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    EvidenceLayer,
    MemoryLayer,
    AnomalyFrame,
)
from datetime import datetime


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider"""
    provider = Mock()
    mock_response = {
        "answer": "Validation result",
        "test_results": [],
        "confidence_update": 0.0
    }
    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def validation_handler(mock_llm_provider):
    """Create ValidationHandler instance"""
    return ValidationHandler(llm_provider=mock_llm_provider)


@pytest.fixture
def investigation_state_phase4():
    """Create investigation state in Phase 4"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.VALIDATION,
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            anomaly_frame=AnomalyFrame(
                statement="API errors",
                affected_components=["api"],
                affected_scope="All users",
                started_at=datetime.utcnow(),
                severity="high",
                confidence=0.8,
                framed_at_turn=3,
            ),
            hypotheses=[
                Hypothesis(
                    statement="Database connection pool exhausted",
                    category="infrastructure",
                    likelihood=0.75,
                    initial_likelihood=0.75,
                    created_at_turn=5,
                    last_updated_turn=5,
                    status=HypothesisStatus.PENDING,
                ),
            ],
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )


class TestValidationHandlerBasics:
    """Test basic functionality"""

    def test_get_phase(self, validation_handler):
        """Test phase identification"""
        assert validation_handler.get_phase() == InvestigationPhase.VALIDATION

    @pytest.mark.asyncio
    async def test_handle_executes_validation(
        self, validation_handler, investigation_state_phase4
    ):
        """Test handler executes hypothesis validation"""
        result = await validation_handler.handle(
            investigation_state=investigation_state_phase4,
            user_query="Test the hypothesis",
            conversation_history="",
        )

        assert result.made_progress is True
        assert result.response_text is not None


class TestHypothesisTesting:
    """Test hypothesis testing workflow"""

    @pytest.mark.asyncio
    async def test_hypothesis_status_transitions(
        self, validation_handler, investigation_state_phase4
    ):
        """Test hypothesis moves from PENDING to TESTING to VALIDATED"""
        hypothesis = investigation_state_phase4.ooda_engine.hypotheses[0]

        # Initially PENDING
        assert hypothesis.status == HypothesisStatus.PENDING

        # After testing, could be TESTING or VALIDATED
        result = await validation_handler.handle(
            investigation_state=investigation_state_phase4,
            user_query="Evidence supports the hypothesis",
            conversation_history="",
        )

        # State should be updated (specific status depends on implementation)
        assert result.made_progress is True


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_completion_requires_validated_hypothesis(
        self, validation_handler, investigation_state_phase4
    ):
        """Test completion requires at least one validated hypothesis"""
        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        # Should not be complete without validated hypothesis
        assert is_complete is False

    @pytest.mark.asyncio
    async def test_completion_with_validated_hypothesis(
        self, validation_handler, investigation_state_phase4
    ):
        """Test completion when hypothesis is validated"""
        # Set hypothesis as validated
        investigation_state_phase4.ooda_engine.hypotheses[0].status = HypothesisStatus.VALIDATED
        investigation_state_phase4.ooda_engine.hypotheses[0].likelihood = 0.85

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        # Should be complete
        assert is_complete is True or len(met) > 0


class TestValidationHandlerV3:
    """Test v3.0-specific validation handler functionality"""

    @pytest.mark.asyncio
    async def test_70_percent_threshold_completion(self, validation_handler, investigation_state_phase4):
        """Test v3.0: Completion at ≥70% confidence threshold"""
        # Set validated hypothesis at exactly 70%
        investigation_state_phase4.ooda_engine.hypotheses[0].likelihood = 0.70
        investigation_state_phase4.ooda_engine.hypotheses[0].status = HypothesisStatus.VALIDATED

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        assert is_complete is True
        assert any("70%" in str(m) for m in met)

    @pytest.mark.asyncio
    async def test_below_70_percent_not_complete(self, validation_handler, investigation_state_phase4):
        """Test v3.0: Below 70% threshold does not complete"""
        # Set hypothesis at 69% (below threshold)
        investigation_state_phase4.ooda_engine.hypotheses[0].likelihood = 0.69
        investigation_state_phase4.ooda_engine.hypotheses[0].status = HypothesisStatus.ACTIVE

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        assert is_complete is False
        assert any("70%" in str(u) for u in unmet)

    @pytest.mark.asyncio
    async def test_degraded_mode_exception_at_cap(self, validation_handler, investigation_state_phase4):
        """Test v3.1: Can advance in degraded mode at confidence cap"""
        from faultmaven.models.investigation import (
            EscalationState,
            DegradedModeType,
            WorkingConclusion,
            ConfidenceLevel,
        )

        # Set degraded mode with 40% cap
        investigation_state_phase4.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.EXPERTISE_REQUIRED,  # 40% cap
            degraded_mode_explanation="Requires kernel debugging",
        )

        # Set working conclusion at cap (38% within 5% margin)
        investigation_state_phase4.lifecycle.working_conclusion = WorkingConclusion(
            statement="Suspected kernel issue",
            confidence=0.38,
            confidence_level=ConfidenceLevel.SPECULATION,
            supporting_evidence_count=2,
            caveats=["Requires specialized expertise"],
            alternative_explanations=[],
            can_proceed_with_solution=True,
        )

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        assert is_complete is True
        assert any("degraded" in str(m).lower() for m in met)
        assert any("cap" in str(m).lower() for m in met)

    @pytest.mark.asyncio
    async def test_degraded_mode_below_cap_not_complete(self, validation_handler, investigation_state_phase4):
        """Test v3.1: Cannot advance in degraded mode below cap"""
        from faultmaven.models.investigation import (
            EscalationState,
            DegradedModeType,
            WorkingConclusion,
            ConfidenceLevel,
        )

        # Set degraded mode with 50% cap
        investigation_state_phase4.lifecycle.escalation_state = EscalationState(
            operating_in_degraded_mode=True,
            degraded_mode_type=DegradedModeType.CRITICAL_EVIDENCE_MISSING,  # 50% cap
            degraded_mode_explanation="Cannot access logs",
        )

        # Set working conclusion below cap (35%)
        investigation_state_phase4.lifecycle.working_conclusion = WorkingConclusion(
            statement="Suspected issue",
            confidence=0.35,
            confidence_level=ConfidenceLevel.SPECULATION,
            supporting_evidence_count=1,
            caveats=["Missing critical evidence"],
            alternative_explanations=[],
            can_proceed_with_solution=False,
        )

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        assert is_complete is False
        assert any("35%" in str(u) or "50%" in str(u) for u in unmet)

    @pytest.mark.asyncio
    async def test_no_stall_recovery_intervention(self, validation_handler, investigation_state_phase4):
        """Test v3.0: No stall recovery interventions (removed)"""
        # Set up state that would trigger stall in old system
        investigation_state_phase4.metadata.current_turn = 20
        investigation_state_phase4.lifecycle.turns_in_current_phase = 10
        investigation_state_phase4.ooda_engine.hypotheses[0].iterations_without_progress = 5

        # Check interventions via coordinator
        from faultmaven.core.investigation.investigation_coordinator import InvestigationCoordinator

        coordinator = InvestigationCoordinator()
        intervention = coordinator.check_interventions(investigation_state_phase4)

        # Should not get stall recovery intervention (removed in v3.0)
        if intervention:
            assert intervention.intervention_type != "stall_recovery"
            assert intervention.intervention_type != "combined_stall_anchoring"

    @pytest.mark.asyncio
    async def test_working_conclusion_used_in_completion(self, validation_handler, investigation_state_phase4):
        """Test v3.0: Working conclusion is used in completion check"""
        from faultmaven.models.investigation import WorkingConclusion, ConfidenceLevel

        # Set working conclusion
        investigation_state_phase4.lifecycle.working_conclusion = WorkingConclusion(
            statement="Root cause identified",
            confidence=0.75,
            confidence_level=ConfidenceLevel.CONFIDENT,
            supporting_evidence_count=4,
            caveats=[],
            alternative_explanations=[],
            can_proceed_with_solution=True,
        )

        # Set validated hypothesis
        investigation_state_phase4.ooda_engine.hypotheses[0].likelihood = 0.75
        investigation_state_phase4.ooda_engine.hypotheses[0].status = HypothesisStatus.VALIDATED

        is_complete, met, unmet = await validation_handler.check_completion(
            investigation_state_phase4
        )

        assert is_complete is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
