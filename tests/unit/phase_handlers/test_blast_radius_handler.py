"""Unit tests for BlastRadiusHandler (Phase 1)

Tests:
- Scope assessment workflow
- Evidence request generation
- AnomalyFrame creation
- Phase transition logic

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from faultmaven.services.agentic.phase_handlers.blast_radius_handler import BlastRadiusHandler
from faultmaven.services.agentic.phase_handlers.base import PhaseHandlerResult
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    InvestigationStrategy,
    EngagementMode,
    OODAStep,
    OODAIteration,
    AnomalyFrame,
    ProblemConfirmation,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    EvidenceLayer,
    MemoryLayer,
)


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider"""
    provider = Mock()
    mock_response = {
        "answer": "Blast radius assessment",
        "evidence_needed": [],
        "suggested_actions": []
    }
    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def blast_radius_handler(mock_llm_provider):
    """Create BlastRadiusHandler instance"""
    return BlastRadiusHandler(
        llm_provider=mock_llm_provider,
        tools=[],
        tracer=None,
    )


@pytest.fixture
def investigation_state_phase1():
    """Create investigation state in Phase 1 (Blast Radius)"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.BLAST_RADIUS,
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
        problem_confirmation=ProblemConfirmation(
            problem_statement="API returning 500 errors",
            affected_components=["api-service"],
            severity="high",
            impact="Production users affected",
            investigation_approach="Systematic investigation",
            estimated_evidence_needed=["logs", "metrics"],
        ),
    )


class TestBlastRadiusBasics:
    """Test basic BlastRadiusHandler functionality"""

    def test_get_phase(self, blast_radius_handler):
        """Test phase identification"""
        assert blast_radius_handler.get_phase() == InvestigationPhase.BLAST_RADIUS

    def test_initialization(self, blast_radius_handler, mock_llm_provider):
        """Test handler initialization"""
        assert blast_radius_handler.llm_provider == mock_llm_provider
        assert blast_radius_handler.tools == []


class TestObserveStep:
    """Test OODA Observe step execution"""

    @pytest.mark.asyncio
    async def test_execute_observe_creates_evidence_requests(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test Observe step generates scope evidence requests"""
        result = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="Help me understand the scope of this issue",
            conversation_history="",
        )

        assert isinstance(result, PhaseHandlerResult)
        assert result.ooda_step_executed == OODAStep.OBSERVE
        assert len(result.evidence_requests_generated) > 0
        assert result.made_progress is True

        # Verify evidence categories
        categories = [req.category for req in result.evidence_requests_generated]
        assert "scope" in [cat.value for cat in categories]

    @pytest.mark.asyncio
    async def test_observe_creates_ooda_iteration(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test Observe step creates OODA iteration"""
        result = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="What's the impact?",
            conversation_history="",
        )

        # Verify iteration created
        assert len(investigation_state_phase1.ooda_engine.iterations) > 0
        iteration = investigation_state_phase1.ooda_engine.iterations[-1]
        assert OODAStep.OBSERVE in iteration.steps_completed
        assert iteration.phase == InvestigationPhase.BLAST_RADIUS


class TestOrientStep:
    """Test OODA Orient step execution"""

    @pytest.mark.asyncio
    async def test_execute_orient_creates_anomaly_frame(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test Orient step creates AnomalyFrame"""
        # Setup: Observe step completed
        iteration = OODAIteration(
            iteration_number=1,
            phase=InvestigationPhase.BLAST_RADIUS,
            started_at_turn=1,
            steps_completed=[OODAStep.OBSERVE],
        )
        investigation_state_phase1.ooda_engine.iterations.append(iteration)

        result = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="I've gathered the evidence",
            conversation_history="",
        )

        # Verify AnomalyFrame created
        assert investigation_state_phase1.ooda_engine.anomaly_frame is not None
        assert isinstance(investigation_state_phase1.ooda_engine.anomaly_frame, AnomalyFrame)
        assert result.ooda_step_executed == OODAStep.ORIENT

    @pytest.mark.asyncio
    async def test_orient_updates_severity_from_evidence(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test Orient step analyzes evidence to update severity"""
        # Setup
        iteration = OODAIteration(
            iteration_number=1,
            phase=InvestigationPhase.BLAST_RADIUS,
            started_at_turn=1,
            steps_completed=[OODAStep.OBSERVE],
        )
        investigation_state_phase1.ooda_engine.iterations.append(iteration)

        result = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="Evidence shows 80% of users affected",
            conversation_history="",
        )

        # Verify AnomalyFrame reflects severity
        assert investigation_state_phase1.ooda_engine.anomaly_frame is not None
        assert investigation_state_phase1.ooda_engine.anomaly_frame.confidence > 0


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_check_completion_incomplete_no_anomaly(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test completion check when AnomalyFrame not created"""
        is_complete, met, unmet = await blast_radius_handler.check_completion(
            investigation_state_phase1
        )

        assert is_complete is False
        assert "AnomalyFrame not created" in unmet

    @pytest.mark.asyncio
    async def test_check_completion_with_anomaly(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test completion check with AnomalyFrame created"""
        # Setup: Create AnomalyFrame
        investigation_state_phase1.ooda_engine.anomaly_frame = AnomalyFrame(
            statement="API 500 errors affecting production",
            affected_components=["api-service", "database"],
            affected_scope="80% of users",
            started_at=datetime.utcnow(),
            severity="critical",
            confidence=0.8,
            framed_at_turn=2,
        )

        # Add sufficient scope evidence
        investigation_state_phase1.evidence.evidence_requests.extend([
            "Check scope of API errors",
            "Verify symptoms across services"
        ])

        # Set iteration count
        investigation_state_phase1.ooda_engine.current_iteration = 1

        is_complete, met, unmet = await blast_radius_handler.check_completion(
            investigation_state_phase1
        )

        assert is_complete is True
        assert "AnomalyFrame created" in met
        assert len(unmet) == 0


class TestEdgeCases:
    """Test edge cases"""

    @pytest.mark.asyncio
    async def test_handle_with_no_problem_confirmation(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test handling case where problem confirmation is missing"""
        investigation_state_phase1.problem_confirmation = None

        result = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="What's the scope?",
            conversation_history="",
        )

        # Should still execute but may have lower confidence
        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None

    @pytest.mark.asyncio
    async def test_multiple_iterations(
        self, blast_radius_handler, investigation_state_phase1
    ):
        """Test multiple OODA iterations within phase"""
        # Execute Observe
        result1 = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="What evidence do you need?",
            conversation_history="",
        )
        assert result1.ooda_step_executed == OODAStep.OBSERVE

        # Execute Orient
        result2 = await blast_radius_handler.handle(
            investigation_state=investigation_state_phase1,
            user_query="Here's the evidence",
            conversation_history="",
        )
        assert result2.ooda_step_executed == OODAStep.ORIENT

        # Verify multiple iterations tracked
        assert len(investigation_state_phase1.ooda_engine.iterations) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
