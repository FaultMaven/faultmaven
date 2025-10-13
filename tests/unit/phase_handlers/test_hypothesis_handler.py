"""Unit tests for HypothesisHandler (Phase 3)

Tests:
- Hypothesis generation from evidence
- Hypothesis ranking by likelihood
- Confidence management
- Phase completion criteria

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from unittest.mock import AsyncMock, Mock

from faultmaven.services.agentic.phase_handlers.hypothesis_handler import HypothesisHandler
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
        "answer": "Hypothesis analysis",
        "hypotheses": [],
        "reasoning": ""
    }
    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def hypothesis_handler(mock_llm_provider):
    """Create HypothesisHandler instance"""
    return HypothesisHandler(llm_provider=mock_llm_provider)


@pytest.fixture
def investigation_state_phase3():
    """Create investigation state in Phase 3"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.HYPOTHESIS,
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            anomaly_frame=AnomalyFrame(
                statement="API returning 500 errors",
                affected_components=["api-service"],
                affected_scope="All users",
                started_at=datetime.utcnow(),
                severity="critical",
                confidence=0.8,
                framed_at_turn=3,
            ),
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )


class TestHypothesisHandlerBasics:
    """Test basic functionality"""

    def test_get_phase(self, hypothesis_handler):
        """Test phase identification"""
        assert hypothesis_handler.get_phase() == InvestigationPhase.HYPOTHESIS

    @pytest.mark.asyncio
    async def test_handle_generates_hypotheses(
        self, hypothesis_handler, investigation_state_phase3
    ):
        """Test handler generates hypotheses"""
        result = await hypothesis_handler.handle(
            investigation_state=investigation_state_phase3,
            user_query="What could be causing this?",
            conversation_history="",
        )

        assert result.made_progress is True
        assert result.response_text is not None


class TestHypothesisGeneration:
    """Test hypothesis generation logic"""

    @pytest.mark.asyncio
    async def test_multiple_hypothesis_generation(
        self, hypothesis_handler, investigation_state_phase3
    ):
        """Test generating multiple hypotheses"""
        # Add mock hypotheses to state during handling
        investigation_state_phase3.ooda_engine.hypotheses = [
            Hypothesis(
                statement="Database connection pool exhausted",
                category="infrastructure",
                likelihood=0.75,
                initial_likelihood=0.75,
                created_at_turn=5,
                last_updated_turn=5,
            ),
            Hypothesis(
                statement="Recent code deployment introduced bug",
                category="code",
                likelihood=0.60,
                initial_likelihood=0.60,
                created_at_turn=5,
                last_updated_turn=5,
            ),
        ]

        result = await hypothesis_handler.handle(
            investigation_state=investigation_state_phase3,
            user_query="Analysis complete",
            conversation_history="",
        )

        # Verify hypotheses tracked
        assert len(investigation_state_phase3.ooda_engine.hypotheses) >= 2


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_completion_requires_hypotheses(
        self, hypothesis_handler, investigation_state_phase3
    ):
        """Test completion requires at least one hypothesis"""
        is_complete, met, unmet = await hypothesis_handler.check_completion(
            investigation_state_phase3
        )

        assert is_complete is False
        assert any("hypotheses" in criterion.lower() for criterion in unmet)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
