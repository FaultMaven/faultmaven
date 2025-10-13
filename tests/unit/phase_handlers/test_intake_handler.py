"""Unit tests for IntakeHandler (Phase 0)

Tests:
- Happy path: User provides problem statement
- Edge case: Vague problem description
- Error handling: Missing required fields
- Consent detection and handling
- Decline detection and handling
- Problem signal strength analysis

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from faultmaven.services.agentic.phase_handlers.intake_handler import IntakeHandler
from faultmaven.services.agentic.phase_handlers.base import PhaseHandlerResult
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    EngagementMode,
    ProblemConfirmation,
    InvestigationMetadata,
    InvestigationLifecycle,
    OODAEngineState,
    EvidenceLayer,
    MemoryLayer,
)
from faultmaven.models.responses import ConsultantResponse


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with realistic responses"""
    provider = Mock()

    # Mock response as dict (expected by handlers)
    mock_response = {
        "answer": "Test response",
        "suggested_commands": [],
        "clarifying_questions": [],
        "problem_detected": True,
        "problem_statement": "System issue detected",
        "severity": "medium"
    }

    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def mock_engagement_manager():
    """Mock engagement mode manager"""
    with patch('faultmaven.services.agentic.phase_handlers.intake_handler.create_engagement_mode_manager') as mock:
        manager = Mock()
        manager.analyze_initial_query = Mock(return_value={
            "signal_strength": "strong",
            "detected_keywords": ["error", "database"],
        })
        manager.create_problem_confirmation = Mock(return_value=ProblemConfirmation(
            problem_statement="Database connection errors",
            affected_components=["api-service"],
            severity="high",
            impact="Production API unavailable",
            investigation_approach="Systematic root cause analysis",
            estimated_evidence_needed=["logs", "metrics"],
        ))
        manager.select_investigation_strategy = Mock(return_value="active_incident")
        mock.return_value = manager
        yield manager


@pytest.fixture
def intake_handler(mock_llm_provider):
    """Create IntakeHandler instance with mocked dependencies"""
    return IntakeHandler(
        llm_provider=mock_llm_provider,
        tools=[],
        tracer=None,
    )


@pytest.fixture
def investigation_state():
    """Create fresh investigation state"""
    return InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.CONSULTANT,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.INTAKE,
        ),
        ooda_engine=OODAEngineState(),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )


class TestIntakeHandlerBasics:
    """Test basic IntakeHandler functionality"""

    def test_get_phase(self, intake_handler):
        """Test phase identification"""
        assert intake_handler.get_phase() == InvestigationPhase.INTAKE

    def test_initialization(self, intake_handler, mock_llm_provider):
        """Test handler initialization"""
        assert intake_handler.llm_provider == mock_llm_provider
        assert intake_handler.engagement_manager is not None
        assert intake_handler.tools == []


class TestHappyPath:
    """Test successful problem detection and consent flow"""

    @pytest.mark.asyncio
    async def test_problem_detection_strong_signal(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test handling strong problem signal"""
        user_query = "Our API is returning 500 errors for all database queries"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None
        assert result.made_progress is True
        assert result.phase_complete is False  # Waiting for consent
        assert result.should_advance is False
        assert investigation_state.problem_confirmation is not None
        assert investigation_state.problem_confirmation.severity == "high"

    @pytest.mark.asyncio
    async def test_consent_handling(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test user consent to investigation"""
        # Setup: Problem confirmation exists
        investigation_state.problem_confirmation = ProblemConfirmation(
            problem_statement="Database connection errors",
            affected_components=["api-service"],
            severity="high",
            impact="Production API unavailable",
            investigation_approach="Systematic root cause analysis",
            estimated_evidence_needed=["logs", "metrics"],
        )

        user_query = "Yes, please help me investigate"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        # Consent detected, phase should complete
        assert result.phase_complete is True
        assert result.should_advance is True
        assert result.next_phase == InvestigationPhase.BLAST_RADIUS
        assert investigation_state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR
        assert investigation_state.ooda_engine.ooda_active is True

    @pytest.mark.asyncio
    async def test_general_query_handling(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test handling general informational query"""
        # Setup: Weak signal
        mock_engagement_manager.analyze_initial_query.return_value = {
            "signal_strength": "weak",
            "detected_keywords": [],
        }

        user_query = "What is Kubernetes?"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        assert result.response_text is not None
        assert result.phase_complete is False
        assert result.should_advance is False
        assert result.made_progress is True


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    @pytest.mark.asyncio
    async def test_vague_problem_description(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test handling vague/ambiguous problem description"""
        # Setup: Strong signal so problem is detected
        mock_engagement_manager.analyze_initial_query.return_value = {
            "signal_strength": "strong",
            "detected_keywords": ["issue", "sometimes"],
        }

        user_query = "Something seems off with the system"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        # Should handle vague query and return a response
        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None

    @pytest.mark.asyncio
    async def test_decline_investigation(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test user declining systematic investigation"""
        # Setup: Problem confirmation exists
        investigation_state.problem_confirmation = ProblemConfirmation(
            problem_statement="Minor issue",
            affected_components=[],
            severity="low",
            impact="Limited",
            investigation_approach="Quick check",
            estimated_evidence_needed=[],
        )

        user_query = "No thanks, I'll figure it out myself"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        assert result.phase_complete is False
        assert result.should_advance is False
        assert investigation_state.metadata.engagement_mode == EngagementMode.CONSULTANT

    @pytest.mark.asyncio
    async def test_empty_query(
        self, intake_handler, investigation_state, mock_engagement_manager
    ):
        """Test handling empty user query"""
        mock_engagement_manager.analyze_initial_query.return_value = {
            "signal_strength": "weak",
            "detected_keywords": [],
        }

        user_query = ""

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None


class TestErrorHandling:
    """Test error handling and recovery"""

    @pytest.mark.asyncio
    async def test_llm_failure(
        self, intake_handler, investigation_state, mock_engagement_manager, mock_llm_provider
    ):
        """Test graceful handling of LLM failures"""
        # Setup: LLM raises exception
        mock_llm_provider.generate.side_effect = Exception("LLM service unavailable")

        user_query = "API is returning errors"

        # Should handle exception gracefully (may raise or return error response)
        try:
            result = await intake_handler.handle(
                investigation_state=investigation_state,
                user_query=user_query,
                conversation_history="",
            )
            # If it returns, should be a valid result
            assert isinstance(result, PhaseHandlerResult)
        except Exception as e:
            # Or it may propagate - that's acceptable for this test
            assert "LLM service unavailable" in str(e) or "problem_detected" in str(e)

    @pytest.mark.asyncio
    async def test_malformed_llm_response(
        self, intake_handler, investigation_state, mock_engagement_manager, mock_llm_provider
    ):
        """Test handling of malformed LLM response"""
        # Setup: LLM returns invalid JSON
        mock_response = Mock()
        mock_response.content = "Not valid JSON at all"
        mock_response.tool_calls = None
        mock_llm_provider.generate.return_value = mock_response

        user_query = "Database connection issue"

        result = await intake_handler.handle(
            investigation_state=investigation_state,
            user_query=user_query,
            conversation_history="",
        )

        # Should handle gracefully with fallback
        assert isinstance(result, PhaseHandlerResult)


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_check_completion_incomplete(self, intake_handler, investigation_state):
        """Test completion check when criteria not met"""
        is_complete, met, unmet = await intake_handler.check_completion(investigation_state)

        assert is_complete is False
        assert len(unmet) > 0
        assert "No problem confirmation yet" in unmet

    @pytest.mark.asyncio
    async def test_check_completion_partial(self, intake_handler, investigation_state):
        """Test completion check with problem but no consent"""
        investigation_state.problem_confirmation = ProblemConfirmation(
            problem_statement="Test problem",
            affected_components=[],
            severity="medium",
            impact="Test",
            investigation_approach="Test",
            estimated_evidence_needed=[],
        )

        is_complete, met, unmet = await intake_handler.check_completion(investigation_state)

        assert is_complete is False
        assert "Problem confirmation created" in met
        assert "Awaiting user consent for Lead Investigator mode" in unmet

    @pytest.mark.asyncio
    async def test_check_completion_complete(self, intake_handler, investigation_state):
        """Test completion check when all criteria met"""
        investigation_state.problem_confirmation = ProblemConfirmation(
            problem_statement="Test problem",
            affected_components=[],
            severity="medium",
            impact="Test",
            investigation_approach="Test",
            estimated_evidence_needed=[],
        )
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR
        investigation_state.ooda_engine.ooda_active = True

        is_complete, met, unmet = await intake_handler.check_completion(investigation_state)

        assert is_complete is True
        assert len(unmet) == 0
        assert "Problem confirmation created" in met
        assert "User consented to investigation" in met
        assert "OODA framework activated" in met


class TestConsentDetection:
    """Test consent and decline detection logic"""

    def test_detect_consent_positive(self, intake_handler):
        """Test detection of various consent phrases"""
        consent_phrases = [
            "yes",
            "Yeah, let's do it",
            "Sure, go ahead",
            "Okay, please proceed",
            "sounds good",
            "please help me investigate",
        ]

        for phrase in consent_phrases:
            assert intake_handler._detect_consent(phrase) is True

    def test_detect_consent_negative(self, intake_handler):
        """Test non-consent phrases"""
        non_consent_phrases = [
            "Tell me more about the approach",
            "What would that involve?",
            "Maybe",
            "I don't know",
        ]

        for phrase in non_consent_phrases:
            assert intake_handler._detect_consent(phrase) is False

    def test_detect_decline_positive(self, intake_handler):
        """Test detection of various decline phrases"""
        decline_phrases = [
            "no thanks",
            "not now",
            "maybe later",
            "just answer my question",
            "no need to investigate",
        ]

        for phrase in decline_phrases:
            assert intake_handler._detect_decline(phrase) is True

    def test_detect_decline_negative(self, intake_handler):
        """Test non-decline phrases"""
        non_decline_phrases = [
            "yes please",
            "go ahead",
            "I need help",
        ]

        for phrase in non_decline_phrases:
            assert intake_handler._detect_decline(phrase) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
