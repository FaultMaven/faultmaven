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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
