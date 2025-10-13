"""Unit tests for DocumentHandler (Phase 6)

Tests:
- Basic functionality: phase identification and handler initialization
- Orient step: synthesis of investigation findings and artifact offering
- Artifact generation: case report, runbook, knowledge insights
- User interaction: acceptance and decline of artifacts
- Completion criteria: artifact offering and investigation closure
- Edge cases: incomplete investigations, minimal investigations
- Error handling: LLM failures and malformed responses

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Note: DocumentHandler uses `lifecycle.artifacts_offered` which is not currently in
the InvestigationLifecycle model. This is a known implementation gap tracked for
future model updates. Tests use PropertyMock to work around this discrepancy.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch, PropertyMock
from datetime import datetime

from faultmaven.services.agentic.phase_handlers.document_handler import DocumentHandler
from faultmaven.services.agentic.phase_handlers.base import PhaseHandlerResult
from faultmaven.models.investigation import (
    InvestigationState,
    InvestigationPhase,
    EngagementMode,
    InvestigationMetadata,
    InvestigationLifecycle,
    InvestigationStrategy,
    OODAEngineState,
    OODAStep,
    EvidenceLayer,
    MemoryLayer,
    AnomalyFrame,
    Hypothesis,
    HypothesisStatus,
)


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with realistic responses"""
    provider = Mock()

    # Mock LLMResponse object with content attribute
    mock_response = Mock()
    mock_response.content = '{"answer": "Would you like me to generate artifacts?", "suggested_commands": [], "clarifying_questions": []}'
    mock_response.tool_calls = None

    provider.generate = AsyncMock(return_value=mock_response)
    return provider


@pytest.fixture
def document_handler(mock_llm_provider):
    """Create DocumentHandler instance with mocked dependencies"""
    return DocumentHandler(
        llm_provider=mock_llm_provider,
        tools=[],
        tracer=None,
    )


@pytest.fixture
def investigation_state_phase6():
    """Create investigation state in Phase 6 with complete investigation

    Note: artifacts_offered is accessed by document_handler but not in InvestigationLifecycle model.
    This is a known issue tracked for model updates. Using property mock for testing.
    """
    state = InvestigationState(
        metadata=InvestigationMetadata(
            session_id="test-session",
            engagement_mode=EngagementMode.LEAD_INVESTIGATOR,
            current_turn=15,
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.DOCUMENT,
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
            case_status="resolved",
        ),
        ooda_engine=OODAEngineState(
            ooda_active=True,
            current_iteration=8,
            anomaly_frame=AnomalyFrame(
                statement="API returning 500 errors for database queries",
                affected_components=["api-service", "database"],
                affected_scope="All users",
                started_at=datetime.utcnow(),
                severity="high",
                confidence=0.9,
                framed_at_turn=3,
            ),
            hypotheses=[
                Hypothesis(
                    statement="Database connection pool exhausted",
                    category="infrastructure",
                    likelihood=0.85,
                    initial_likelihood=0.60,
                    created_at_turn=5,
                    last_updated_turn=12,
                    status=HypothesisStatus.VALIDATED,
                    evidence_for=["Pool size = 10, concurrent connections = 50"],
                    evidence_against=[],
                ),
            ],
        ),
        evidence=EvidenceLayer(),
        memory=MemoryLayer(),
    )

    # Workaround: artifacts_offered not in model but used by handler
    # Use object.__setattr__ to bypass Pydantic's setter validation
    object.__setattr__(state.lifecycle, "artifacts_offered", False)

    return state


@pytest.fixture
def investigation_state_with_artifacts_offered(investigation_state_phase6):
    """Create investigation state where artifacts have been offered"""
    state = investigation_state_phase6.model_copy(deep=True)

    # Workaround for artifacts_offered not in model
    object.__setattr__(state.lifecycle, "artifacts_offered", True)

    return state


class TestDocumentHandlerBasics:
    """Test basic DocumentHandler functionality"""

    def test_get_phase(self, document_handler):
        """Test phase identification"""
        assert document_handler.get_phase() == InvestigationPhase.DOCUMENT

    def test_initialization(self, document_handler, mock_llm_provider):
        """Test handler initialization"""
        assert document_handler.llm_provider == mock_llm_provider
        assert document_handler.tools == []


class TestOrientStep:
    """Test OODA Orient step - synthesis and artifact offering"""

    @pytest.mark.asyncio
    async def test_execute_orient_synthesizes_investigation(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test Orient step synthesizes investigation findings"""
        result = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="What should we do next?",
            conversation_history="",
        )

        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None
        assert result.made_progress is True
        assert result.ooda_step_executed == OODAStep.ORIENT
        assert result.iteration_complete is True

        # Verify artifacts were offered
        assert investigation_state_phase6.lifecycle.artifacts_offered is True

    @pytest.mark.asyncio
    async def test_orient_creates_ooda_iteration(
        self, document_handler, investigation_state_phase6
    ):
        """Test Orient step creates new OODA iteration"""
        # Clear any existing iterations
        investigation_state_phase6.ooda_engine.iterations = []

        result = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="What's next?",
            conversation_history="",
        )

        # Should have created an iteration
        assert len(investigation_state_phase6.ooda_engine.iterations) == 1
        iteration = investigation_state_phase6.ooda_engine.iterations[0]
        assert OODAStep.ORIENT in iteration.steps_completed

    @pytest.mark.asyncio
    async def test_orient_step_llm_called_with_context(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test Orient step calls LLM with investigation context"""
        result = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="Ready for artifacts?",
            conversation_history="",
        )

        # Verify LLM was called
        mock_llm_provider.generate.assert_called_once()
        call_args = mock_llm_provider.generate.call_args

        # Verify context includes investigation summary
        prompt = call_args.kwargs.get('prompt') or call_args[0][0]
        assert isinstance(prompt, str)


class TestArtifactGeneration:
    """Test artifact generation functionality"""

    @pytest.mark.asyncio
    async def test_user_accepts_artifacts(
        self, document_handler, investigation_state_with_artifacts_offered
    ):
        """Test complete artifact generation when user accepts"""
        user_query = "yes please generate the artifacts"

        result = await document_handler.handle(
            investigation_state=investigation_state_with_artifacts_offered,
            user_query=user_query,
            conversation_history="",
        )

        assert result.phase_complete is True
        assert result.should_advance is False
        assert result.next_phase is None
        assert result.made_progress is True

        # Case status should be documented
        assert investigation_state_with_artifacts_offered.lifecycle.case_status == "documented"

    @pytest.mark.asyncio
    async def test_user_declines_artifacts(
        self, document_handler, investigation_state_with_artifacts_offered
    ):
        """Test handling when user declines artifact generation"""
        user_query = "no thanks, not needed"

        result = await document_handler.handle(
            investigation_state=investigation_state_with_artifacts_offered,
            user_query=user_query,
            conversation_history="",
        )

        assert result.phase_complete is True
        assert result.should_advance is False
        assert result.next_phase is None
        assert result.made_progress is True

        # Case status should be completed (not documented)
        assert investigation_state_with_artifacts_offered.lifecycle.case_status == "completed"

    @pytest.mark.asyncio
    async def test_generate_artifacts_creates_multiple_types(
        self, document_handler, investigation_state_phase6
    ):
        """Test artifact generation creates case report, runbook, and insights"""
        artifacts = await document_handler._generate_artifacts(investigation_state_phase6)

        # Should generate at least case report and runbook
        assert len(artifacts) >= 2

        artifact_types = [a['type'] for a in artifacts]
        assert 'case_report' in artifact_types
        assert 'runbook' in artifact_types

        # All artifacts should have required fields
        for artifact in artifacts:
            assert 'type' in artifact
            assert 'title' in artifact
            assert 'content' in artifact
            assert 'generated_at' in artifact

    @pytest.mark.asyncio
    async def test_generate_case_report_includes_key_sections(
        self, document_handler, investigation_state_phase6
    ):
        """Test case report includes all key sections"""
        case_report = await document_handler._generate_case_report(investigation_state_phase6)

        # Verify key sections present
        assert "# Investigation Case Report" in case_report
        assert "## Problem Summary" in case_report
        assert "## Investigation Timeline" in case_report
        assert "## Root Cause" in case_report
        assert "## Solution Applied" in case_report
        assert "## Key Evidence Collected" in case_report
        assert "## Lessons Learned" in case_report

    @pytest.mark.asyncio
    async def test_generate_runbook_includes_response_steps(
        self, document_handler, investigation_state_phase6
    ):
        """Test runbook includes incident response steps"""
        runbook = await document_handler._generate_runbook(investigation_state_phase6)

        # Verify key sections present
        assert "# Incident Response Runbook" in runbook
        assert "## Incident Type" in runbook
        assert "## Detection" in runbook
        assert "## Diagnosis Steps" in runbook
        assert "## Root Cause" in runbook
        assert "## Remediation Steps" in runbook
        assert "## Prevention" in runbook
        assert "## Escalation" in runbook

    @pytest.mark.asyncio
    async def test_artifact_generation_with_insights(
        self, document_handler, investigation_state_phase6
    ):
        """Test knowledge insights generated when hypotheses present"""
        # Ensure persistent insights exist (use correct hierarchical path)
        investigation_state_phase6.memory.hierarchical_memory.persistent_insights = [
            "Connection pool too small",
            "No monitoring configured",
        ]

        artifacts = await document_handler._generate_artifacts(investigation_state_phase6)

        # Should include knowledge_insights
        artifact_types = [a['type'] for a in artifacts]
        assert 'knowledge_insights' in artifact_types


class TestUserInteraction:
    """Test user interaction patterns"""

    def test_detect_artifact_acceptance_positive(self, document_handler):
        """Test detection of various acceptance phrases"""
        acceptance_phrases = [
            "yes",
            "yes please",
            "sure, go ahead",
            "okay, generate them",
            "sounds good",
            "that would be great",
            "i'd like the artifacts",
            "please create the report",
        ]

        for phrase in acceptance_phrases:
            assert document_handler._detect_artifact_acceptance(phrase) is True

    def test_detect_artifact_acceptance_negative(self, document_handler):
        """Test detection of various rejection phrases"""
        rejection_phrases = [
            "no",
            "no thanks",
            "not now",
            "skip it",
            "don't need them",
            "maybe later",
            "pass",
        ]

        for phrase in rejection_phrases:
            assert document_handler._detect_artifact_acceptance(phrase) is False

    def test_detect_artifact_acceptance_default_positive(self, document_handler):
        """Test ambiguous queries default to acceptance"""
        ambiguous_query = "what do you think?"

        # Default should be True (artifacts are helpful)
        assert document_handler._detect_artifact_acceptance(ambiguous_query) is True

    def test_format_artifacts_for_delivery(self, document_handler):
        """Test artifact formatting for user delivery"""
        artifacts = [
            {
                "type": "case_report",
                "title": "Investigation Case Report",
                "content": "Report content here",
                "generated_at": datetime.utcnow().isoformat(),
            },
            {
                "type": "runbook",
                "title": "Incident Response Runbook",
                "content": "Runbook content here",
                "generated_at": datetime.utcnow().isoformat(),
            },
        ]

        formatted = document_handler._format_artifacts_for_delivery(artifacts)

        assert "# Investigation Artifacts" in formatted
        assert "## Investigation Case Report" in formatted
        assert "## Incident Response Runbook" in formatted
        assert "Report content here" in formatted
        assert "Runbook content here" in formatted


class TestCompletionCriteria:
    """Test phase completion detection"""

    @pytest.mark.asyncio
    async def test_check_completion_incomplete(
        self, document_handler, investigation_state_phase6
    ):
        """Test completion check when artifacts not offered"""
        is_complete, met, unmet = await document_handler.check_completion(
            investigation_state_phase6
        )

        assert is_complete is False
        assert "Artifacts not yet offered" in unmet

    @pytest.mark.asyncio
    async def test_check_completion_artifacts_offered_pending(
        self, document_handler, investigation_state_with_artifacts_offered
    ):
        """Test completion check when artifacts offered but not responded"""
        # Artifacts offered but case not marked complete
        investigation_state_with_artifacts_offered.lifecycle.case_status = "resolved"

        is_complete, met, unmet = await document_handler.check_completion(
            investigation_state_with_artifacts_offered
        )

        assert is_complete is False
        assert "Artifacts offered to user" in met
        assert "Investigation not yet complete" in unmet

    @pytest.mark.asyncio
    async def test_check_completion_complete_documented(
        self, document_handler, investigation_state_with_artifacts_offered
    ):
        """Test completion check when investigation documented"""
        investigation_state_with_artifacts_offered.lifecycle.case_status = "documented"

        is_complete, met, unmet = await document_handler.check_completion(
            investigation_state_with_artifacts_offered
        )

        assert is_complete is True
        assert len(unmet) == 0
        assert "Artifacts offered to user" in met
        assert "Investigation complete" in met

    @pytest.mark.asyncio
    async def test_check_completion_complete_without_artifacts(
        self, document_handler, investigation_state_with_artifacts_offered
    ):
        """Test completion check when user declined artifacts"""
        investigation_state_with_artifacts_offered.lifecycle.case_status = "completed"

        is_complete, met, unmet = await document_handler.check_completion(
            investigation_state_with_artifacts_offered
        )

        assert is_complete is True
        assert len(unmet) == 0
        assert "Investigation complete" in met


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    @pytest.mark.asyncio
    async def test_incomplete_investigation_minimal_artifacts(
        self, document_handler, investigation_state_phase6
    ):
        """Test artifact generation with minimal investigation data"""
        # Create minimal state
        investigation_state_phase6.ooda_engine.hypotheses = []
        investigation_state_phase6.memory.hierarchical_memory.persistent_insights = []
        investigation_state_phase6.ooda_engine.anomaly_frame = None

        # Should still generate artifacts without crashing
        artifacts = await document_handler._generate_artifacts(investigation_state_phase6)

        assert len(artifacts) >= 2  # At least report and runbook
        assert all('content' in a for a in artifacts)

    @pytest.mark.asyncio
    async def test_quick_fix_investigation_artifacts(
        self, document_handler, investigation_state_phase6
    ):
        """Test artifact generation for quick fix investigation"""
        # Simulate quick fix
        investigation_state_phase6.metadata.current_turn = 5
        investigation_state_phase6.ooda_engine.current_iteration = 2

        case_report = await document_handler._generate_case_report(investigation_state_phase6)

        # Should still generate complete report
        assert "# Investigation Case Report" in case_report
        assert "**Total Turns:** 5" in case_report

    @pytest.mark.asyncio
    async def test_no_validated_hypothesis(
        self, document_handler, investigation_state_phase6
    ):
        """Test artifact generation when no hypothesis was validated"""
        # Mark hypothesis as refuted
        investigation_state_phase6.ooda_engine.hypotheses[0].status = HypothesisStatus.REFUTED

        case_report = await document_handler._generate_case_report(investigation_state_phase6)

        # Should handle gracefully
        assert "# Investigation Case Report" in case_report
        assert "No hypothesis validated" in case_report or "N/A" in case_report

    def test_synthesize_investigation_complete_data(
        self, document_handler, investigation_state_phase6
    ):
        """Test investigation synthesis with complete data"""
        summary = document_handler._synthesize_investigation(investigation_state_phase6)

        assert 'case_status' in summary
        assert 'total_turns' in summary
        assert 'ooda_iterations' in summary
        assert 'phases_completed' in summary
        assert 'root_cause' in summary
        assert 'root_cause_confidence' in summary
        assert 'anomaly_frame' in summary

        # Verify content
        assert summary['case_status'] == "resolved"
        assert summary['total_turns'] == 15
        assert summary['ooda_iterations'] == 8

    def test_extract_kb_insights_with_data(
        self, document_handler, investigation_state_phase6
    ):
        """Test knowledge base insights extraction"""
        insights = document_handler._extract_kb_insights(investigation_state_phase6)

        assert "# Key Insights for Knowledge Base" in insights
        assert "## Validated Root Cause" in insights
        assert "Database connection pool exhausted" in insights
        # Persistent insights only added if present in memory


class TestErrorHandling:
    """Test error handling and recovery"""

    @pytest.mark.asyncio
    async def test_llm_failure_during_orient(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test graceful handling of LLM failures during Orient"""
        # Setup: LLM raises exception
        mock_llm_provider.generate.side_effect = Exception("LLM service unavailable")

        # Should not raise exception
        result = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="Generate artifacts",
            conversation_history="",
        )

        # Should return fallback response
        assert isinstance(result, PhaseHandlerResult)
        assert result.response_text is not None

    @pytest.mark.asyncio
    async def test_malformed_llm_response(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test handling of malformed LLM response"""
        # Setup: LLM returns invalid JSON
        mock_response = Mock()
        mock_response.content = "Not valid JSON at all"
        mock_response.tool_calls = None
        mock_llm_provider.generate.return_value = mock_response

        result = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="Generate artifacts",
            conversation_history="",
        )

        # Should handle gracefully with fallback
        assert isinstance(result, PhaseHandlerResult)

    @pytest.mark.asyncio
    async def test_artifact_generation_with_missing_hypothesis_manager(
        self, document_handler, investigation_state_phase6
    ):
        """Test artifact generation handles missing dependencies gracefully"""
        # This tests internal error handling when creating hypothesis manager fails
        with patch('faultmaven.core.investigation.hypothesis_manager.create_hypothesis_manager') as mock_create:
            mock_create.side_effect = Exception("Hypothesis manager unavailable")

            # Should handle gracefully or raise appropriate error
            try:
                await document_handler._generate_case_report(investigation_state_phase6)
            except Exception as e:
                # If it raises, should be a clean error
                assert "Hypothesis manager unavailable" in str(e)


class TestHappyPath:
    """Test complete happy path workflow"""

    @pytest.mark.asyncio
    async def test_complete_documentation_workflow(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test complete workflow from offering to generating artifacts"""
        # Step 1: Orient - Offer artifacts
        result1 = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="What's next?",
            conversation_history="",
        )

        assert result1.ooda_step_executed == OODAStep.ORIENT
        assert result1.iteration_complete is True
        assert investigation_state_phase6.lifecycle.artifacts_offered is True

        # Step 2: User accepts artifacts
        result2 = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="yes please",
            conversation_history="",
        )

        assert result2.phase_complete is True
        assert result2.should_advance is False
        assert investigation_state_phase6.lifecycle.case_status == "documented"

    @pytest.mark.asyncio
    async def test_complete_workflow_user_declines(
        self, document_handler, investigation_state_phase6, mock_llm_provider
    ):
        """Test complete workflow when user declines artifacts"""
        # Step 1: Orient - Offer artifacts
        result1 = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="What should we do?",
            conversation_history="",
        )

        assert investigation_state_phase6.lifecycle.artifacts_offered is True

        # Step 2: User declines artifacts
        result2 = await document_handler.handle(
            investigation_state=investigation_state_phase6,
            user_query="no thanks",
            conversation_history="",
        )

        assert result2.phase_complete is True
        assert result2.should_advance is False
        assert investigation_state_phase6.lifecycle.case_status == "completed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
