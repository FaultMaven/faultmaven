"""Integration test for OODA investigation workflow

Tests the complete OODA framework end-to-end:
1. Phase 0: Intake (problem detection and consent)
2. Phase 1: Blast Radius (scope assessment)
3. Phase 2: Timeline (temporal context)
4. Phase 3: Hypothesis (theory generation)
5. Phase 4: Validation (systematic testing)
6. Phase 5: Solution (fix implementation)
7. Phase 6: Document (artifact generation)

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from faultmaven.services.agentic.orchestration.phase_orchestrator import PhaseOrchestrator
from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    EngagementMode,
    InvestigationStrategy,
)
from faultmaven.models.interfaces import ILLMProvider


@pytest.fixture
def mock_llm_provider():
    """Create mock LLM provider for testing"""
    mock = Mock(spec=ILLMProvider)
    mock.generate = AsyncMock()
    return mock


@pytest.fixture
def session_id():
    """Test session ID"""
    return "test_session_123"


@pytest.fixture
async def orchestrator(mock_llm_provider, session_id):
    """Create PhaseOrchestrator instance"""
    # FIXED: Pass case_id (investigation state belongs to case, not session)
    return PhaseOrchestrator(
        llm_provider=mock_llm_provider,
        case_id="test_case_123",  # Mock case ID for testing
        session_id=session_id,
    )


@pytest.mark.asyncio
class TestOODAWorkflowIntegration:
    """Integration tests for complete OODA workflow"""

    async def test_phase_0_to_phase_1_transition(self, orchestrator, mock_llm_provider):
        """Test transition from Phase 0 (Intake) to Phase 1 (Blast Radius)"""

        # Initialize investigation
        investigation_state = await orchestrator.initialize_investigation(
            user_query="My API is returning 500 errors",
        )

        # Verify starts in Phase 0 Consultant mode
        assert investigation_state.lifecycle.current_phase == InvestigationPhase.INTAKE
        assert investigation_state.metadata.engagement_mode == EngagementMode.CONSULTANT

        # Mock LLM response for problem detection
        mock_llm_provider.generate.return_value = (
            "I detected you're experiencing API errors. "
            "Would you like me to investigate this systematically?"
        )

        # Process turn with problem detection
        response, updated_state = await orchestrator.process_turn(
            user_query="My API is returning 500 errors",
            investigation_state=investigation_state,
        )

        assert "investigate" in response.lower()

        # User gives consent
        mock_llm_provider.generate.return_value = (
            "Great! Let's start investigating. First, I need to understand the scope..."
        )

        response, updated_state = await orchestrator.process_turn(
            user_query="Yes, please help me investigate",
            investigation_state=updated_state,
        )

        # Should transition to Phase 1 Lead Investigator mode
        assert updated_state.lifecycle.current_phase == InvestigationPhase.BLAST_RADIUS
        assert updated_state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR

    async def test_phase_1_blast_radius_ooda_cycle(self, orchestrator, mock_llm_provider):
        """Test Phase 1 OODA cycle (Observe, Orient)"""

        # Create investigation in Phase 1
        investigation_state = await orchestrator.initialize_investigation(
            user_query="API errors affecting production",
        )

        # Manually set to Phase 1 for this test
        investigation_state.lifecycle.current_phase = InvestigationPhase.BLAST_RADIUS
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR

        # Mock problem confirmation
        from faultmaven.models.investigation import ProblemConfirmation
        investigation_state.problem_confirmation = ProblemConfirmation(
            problem_detected=True,
            problem_statement="API returning 500 errors",
            severity="high",
            urgency_level="high",
            signal_strength="strong",
        )

        # Phase 1 Step 1: Observe - Request scope evidence
        mock_llm_provider.generate.return_value = (
            "I need to understand the blast radius. "
            "What components are affected?"
        )

        response, updated_state = await orchestrator.process_turn(
            user_query="The errors started 2 hours ago",
            investigation_state=investigation_state,
        )

        # Should have created evidence requests
        assert len(updated_state.evidence.evidence_requests) > 0

        # Phase 1 Step 2: Orient - Analyze scope
        mock_llm_provider.generate.return_value = "Based on the scope, this appears to affect the entire API service."

        response, updated_state = await orchestrator.process_turn(
            user_query="All API endpoints are affected, about 1000 users",
            investigation_state=updated_state,
        )

        # Should have created AnomalyFrame
        assert updated_state.ooda_engine.anomaly_frame is not None
        assert "API" in updated_state.ooda_engine.anomaly_frame.statement

    async def test_phase_3_hypothesis_generation(self, orchestrator, mock_llm_provider):
        """Test Phase 3 hypothesis generation"""

        # Create investigation in Phase 3
        investigation_state = await orchestrator.initialize_investigation(
            user_query="Database connection errors",
        )

        # Set to Phase 3
        investigation_state.lifecycle.current_phase = InvestigationPhase.HYPOTHESIS
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR
        investigation_state.lifecycle.investigation_strategy = InvestigationStrategy.ACTIVE_INCIDENT

        # Create AnomalyFrame from previous phases
        from faultmaven.models.investigation import AnomalyFrame
        investigation_state.ooda_engine.anomaly_frame = AnomalyFrame(
            statement="Database connection pool exhausted",
            affected_components=["api-service", "database"],
            affected_scope="all_users",
            started_at=datetime.utcnow(),
            severity="high",
            confidence=0.75,
        )

        # Mock LLM for hypothesis generation
        mock_llm_provider.generate.return_value = (
            "Based on the evidence, I have 3 hypotheses: "
            "1) Connection pool too small, "
            "2) Connection leak in code, "
            "3) Database performance degraded"
        )

        response, updated_state = await orchestrator.process_turn(
            user_query="What could be causing this?",
            investigation_state=investigation_state,
        )

        # Should have generated hypotheses
        # Note: Actual hypothesis creation happens in handler
        # This verifies the orchestrator correctly routes to hypothesis handler
        assert updated_state.lifecycle.current_phase == InvestigationPhase.HYPOTHESIS

    async def test_phase_4_validation_with_anchoring_detection(self, orchestrator, mock_llm_provider):
        """Test Phase 4 validation with anchoring detection"""

        # Create investigation in Phase 4
        investigation_state = await orchestrator.initialize_investigation(
            user_query="Testing hypotheses",
        )

        # Set to Phase 4
        investigation_state.lifecycle.current_phase = InvestigationPhase.VALIDATION
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR

        # Create multiple hypotheses (potential anchoring scenario)
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        from faultmaven.models.investigation import HypothesisStatus

        hypothesis_manager = create_hypothesis_manager()

        h1 = hypothesis_manager.create_hypothesis(
            "Code bug in handler", "code", 0.70, 1
        )
        h2 = hypothesis_manager.create_hypothesis(
            "Code bug in middleware", "code", 0.65, 1
        )
        h3 = hypothesis_manager.create_hypothesis(
            "Code bug in validator", "code", 0.60, 1
        )
        h4 = hypothesis_manager.create_hypothesis(
            "Code bug in parser", "code", 0.55, 1
        )

        # All testing, all same category
        for h in [h1, h2, h3, h4]:
            h.status = HypothesisStatus.TESTING
            h.iterations_without_progress = 2

        investigation_state.ooda_engine.hypotheses = [h1, h2, h3, h4]

        # Should detect anchoring (4 code hypotheses)
        anchored, reason, alternatives = hypothesis_manager.detect_anchoring(
            hypotheses=investigation_state.ooda_engine.hypotheses,
            current_iteration=3,
        )

        assert anchored is True
        assert "same category" in reason.lower() or "stalled" in reason.lower()

    async def test_phase_5_solution_implementation(self, orchestrator, mock_llm_provider):
        """Test Phase 5 solution implementation"""

        # Create investigation in Phase 5
        investigation_state = await orchestrator.initialize_investigation(
            user_query="Implementing solution",
        )

        # Set to Phase 5
        investigation_state.lifecycle.current_phase = InvestigationPhase.SOLUTION
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR

        # Add validated hypothesis
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        from faultmaven.models.investigation import HypothesisStatus

        hypothesis_manager = create_hypothesis_manager()
        validated_hypothesis = hypothesis_manager.create_hypothesis(
            "Connection pool size too small", "config", 0.95, 1
        )
        validated_hypothesis.status = HypothesisStatus.VALIDATED

        investigation_state.ooda_engine.hypotheses = [validated_hypothesis]

        # Mock solution proposal
        mock_llm_provider.generate.return_value = (
            "Based on the validated hypothesis, "
            "increase the connection pool size from 10 to 50."
        )

        response, updated_state = await orchestrator.process_turn(
            user_query="What's the solution?",
            investigation_state=investigation_state,
        )

        assert "solution" in response.lower() or "fix" in response.lower()

    async def test_phase_6_document_generation(self, orchestrator, mock_llm_provider):
        """Test Phase 6 document and artifact generation"""

        # Create investigation in Phase 6
        investigation_state = await orchestrator.initialize_investigation(
            user_query="Generate documentation",
        )

        # Set to Phase 6
        investigation_state.lifecycle.current_phase = InvestigationPhase.DOCUMENT
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR
        investigation_state.lifecycle.case_status = "resolved"

        # Add complete investigation data
        from faultmaven.models.investigation import AnomalyFrame, HypothesisStatus
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager

        investigation_state.ooda_engine.anomaly_frame = AnomalyFrame(
            statement="Database connection pool exhausted",
            affected_components=["api-service"],
            affected_scope="all_users",
            started_at=datetime.utcnow(),
            severity="high",
            confidence=0.90,
        )

        hypothesis_manager = create_hypothesis_manager()
        validated = hypothesis_manager.create_hypothesis(
            "Connection pool size insufficient", "config", 0.95, 1
        )
        validated.status = HypothesisStatus.VALIDATED
        investigation_state.ooda_engine.hypotheses = [validated]

        # Mock artifact offer
        mock_llm_provider.generate.return_value = (
            "Investigation complete! Would you like me to generate "
            "a case report and runbook?"
        )

        response, updated_state = await orchestrator.process_turn(
            user_query="Wrap up the investigation",
            investigation_state=investigation_state,
        )

        # Should offer artifacts
        assert updated_state.lifecycle.artifacts_offered is True

    async def test_get_phase_status(self, orchestrator):
        """Test getting phase status information"""

        investigation_state = await orchestrator.initialize_investigation(
            user_query="Test query",
        )

        # Set to specific phase
        investigation_state.lifecycle.current_phase = InvestigationPhase.HYPOTHESIS
        investigation_state.ooda_engine.current_iteration = 2

        status = orchestrator.get_phase_status(investigation_state)

        assert status["current_phase"] == "HYPOTHESIS"
        assert status["phase_number"] == 3
        assert status["ooda_iteration"] == 2
        assert "engagement_mode" in status
        assert "case_status" in status

    async def test_check_phase_completion(self, orchestrator):
        """Test checking phase completion criteria"""

        investigation_state = await orchestrator.initialize_investigation(
            user_query="Test completion",
        )

        # Check Phase 0 completion (should not be complete yet)
        is_complete, met, unmet = await orchestrator.check_phase_completion(
            investigation_state
        )

        assert is_complete is False
        assert len(unmet) > 0

    async def test_investigation_summary(self, orchestrator):
        """Test generating investigation summary"""

        investigation_state = await orchestrator.initialize_investigation(
            user_query="Summarize investigation",
        )

        # Add some data
        investigation_state.lifecycle.current_phase = InvestigationPhase.VALIDATION
        investigation_state.metadata.current_turn = 10
        investigation_state.ooda_engine.current_iteration = 3

        summary = orchestrator.get_investigation_summary(investigation_state)

        assert "Phase" in summary
        assert "VALIDATION" in summary
        assert "Turns: 10" in summary
        assert "OODA Iterations: 3" in summary


@pytest.mark.asyncio
class TestOODAIntegrationScenarios:
    """Test complete investigation scenarios"""

    async def test_active_incident_fast_path(self, orchestrator, mock_llm_provider):
        """Test active incident investigation (speed priority)"""

        # Initialize with urgent problem
        investigation_state = await orchestrator.initialize_investigation(
            user_query="URGENT: Production API is down!",
        )

        # Should detect urgency
        assert investigation_state.metadata.engagement_mode == EngagementMode.CONSULTANT

        # Fast-forward through consent
        investigation_state.lifecycle.current_phase = InvestigationPhase.BLAST_RADIUS
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR
        investigation_state.lifecycle.investigation_strategy = InvestigationStrategy.ACTIVE_INCIDENT

        # Active incident allows 60-70% confidence threshold
        from faultmaven.core.investigation.strategy_selector import StrategyConfig

        config = StrategyConfig.ACTIVE_INCIDENT
        assert config["min_hypothesis_confidence"] == 0.60

    async def test_post_mortem_thorough_path(self, orchestrator, mock_llm_provider):
        """Test post-mortem investigation (thoroughness priority)"""

        # Initialize with past problem
        investigation_state = await orchestrator.initialize_investigation(
            user_query="I want to analyze what caused the outage last week",
        )

        # Fast-forward to investigation
        investigation_state.lifecycle.current_phase = InvestigationPhase.HYPOTHESIS
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR
        investigation_state.lifecycle.investigation_strategy = InvestigationStrategy.POST_MORTEM

        # Post-mortem requires 85%+ confidence
        from faultmaven.core.investigation.strategy_selector import StrategyConfig

        config = StrategyConfig.POST_MORTEM
        assert config["min_hypothesis_confidence"] == 0.85
        assert config["allow_phase_skipping"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
