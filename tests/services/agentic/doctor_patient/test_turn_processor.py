"""Tests for doctor/patient turn processor."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from faultmaven.models import (
    Case,
    CaseStatus,
    CaseDiagnosticState,
    UrgencyLevel,
    LLMResponse as DoctorPatientLLMResponse,
    SuggestedAction,
    ActionType,
    CommandSuggestion,
    CaseMessage,
    MessageType
)
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.prompts.doctor_patient import PromptVersion
from faultmaven.services.agentic.doctor_patient.turn_processor import process_turn


@pytest.fixture
def mock_llm_client():
    """Mock LLM client."""
    client = AsyncMock()
    return client


@pytest.fixture
def empty_case():
    """Case with no diagnostic state."""
    return Case(
        case_id="test-case-001",
        title="Test Case",
        description="Test case for doctor/patient testing",
        status=CaseStatus.ACTIVE,
        session_id="test-session-001",
        diagnostic_state=CaseDiagnosticState(),
        messages=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )


@pytest.fixture
def active_problem_case():
    """Case with an active problem."""
    return Case(
        case_id="test-case-002",
        title="API 500 Errors",
        description="User experiencing API errors",
        status=CaseStatus.ACTIVE,
        session_id="test-session-002",
        diagnostic_state=CaseDiagnosticState(
            has_active_problem=True,
            problem_statement="API returning 500 errors intermittently",
            urgency_level=UrgencyLevel.HIGH,
            current_phase=1,  # Blast Radius
            symptoms=["500 errors", "intermittent failures"],
            blast_radius={"affected_endpoints": ["/api/users", "/api/orders"]},
            timeline={"started": "2 hours ago"}
        ),
        messages=[
            CaseMessage(case_id="test-case",
                message_type="user_query",
                content="My API is returning 500 errors",
                timestamp=datetime.utcnow()
            ),
            CaseMessage(case_id="test-case",
                message_type="agent_response",
                content="I see. Let's diagnose this. Which endpoints are affected?",
                timestamp=datetime.utcnow()
            )
        ],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )


class TestProcessTurn:
    """Tests for process_turn function."""

    @pytest.mark.asyncio
    async def test_greeting_no_problem(self, mock_llm_client, empty_case):
        """Test greeting without technical problem."""
        # Mock LLM response - function calling with no active problem
        mock_llm_client.route.return_value = LLMResponse(
            content="Hello! I'm here to help with any technical troubleshooting you need. What can I assist you with today?",
            model="gpt-4",
            tokens_used=130, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_123",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '{"has_active_problem": false, "current_phase": 0, "problem_statement": ""}'
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="hello",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify LLM was called with function schema
        assert mock_llm_client.route.called
        call_args = mock_llm_client.route.call_args
        assert call_args.kwargs.get("tools") is not None

        # Verify response
        assert response.answer == "Hello! I'm here to help with any technical troubleshooting you need. What can I assist you with today?"

        # Verify state - no active problem
        assert updated_state.has_active_problem is False
        assert updated_state.current_phase == 0
        assert updated_state.problem_statement == ""

    @pytest.mark.asyncio
    async def test_problem_statement_detection(self, mock_llm_client, empty_case):
        """Test detection of active problem from user query."""
        # Mock LLM response - detects problem
        mock_llm_client.route.return_value = LLMResponse(
            content="I understand you're seeing 500 errors. Let's diagnose this. When did these errors start?",
            model="gpt-4",
            tokens_used=190, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_456",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '{"has_active_problem": true, "problem_statement": "API returning 500 errors", "current_phase": 1, "urgency_level": "high", "new_symptoms": ["500 errors"]}'
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="My API is returning 500 errors",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify state update
        assert updated_state.has_active_problem is True
        assert updated_state.problem_statement == "API returning 500 errors"
        assert updated_state.current_phase == 1  # Blast Radius
        assert updated_state.urgency_level == UrgencyLevel.HIGH
        assert "500 errors" in updated_state.symptoms

    @pytest.mark.asyncio
    async def test_phase_progression(self, mock_llm_client, active_problem_case):
        """Test diagnostic phase progression."""
        # User provides timeline info, LLM advances to hypothesis phase
        mock_llm_client.route.return_value = LLMResponse(
            content="Thanks. Since it started after deployment, let's form hypotheses. This could be: 1) Database connection pool exhaustion, or 2) Memory leak in new code.",
            model="gpt-4",
            tokens_used=250, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_789",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "current_phase": 3,
                            "phase_advancement_reason": "Timeline established, moving to hypothesis formation",
                            "timeline_updates": {"trigger_event": "deployment at 14:00"},
                            "new_hypotheses": [
                                {"hypothesis": "Database connection pool exhaustion", "likelihood": "high", "evidence": "500 errors started after deployment"},
                                {"hypothesis": "Memory leak in new code", "likelihood": "medium", "evidence": "Timing aligns with code changes"}
                            ]
                        }'''
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="It started about 2 hours ago, right after our 2pm deployment",
            case=active_problem_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify phase advancement
        assert updated_state.current_phase == 3  # Hypothesis
        assert len(updated_state.hypotheses) == 2
        assert updated_state.timeline.get("trigger_event") == "deployment at 14:00"

    @pytest.mark.asyncio
    async def test_suggested_actions(self, mock_llm_client, active_problem_case):
        """Test suggested actions in response."""
        # Mock LLM response with suggested actions in content
        mock_response_content = """Let's check database connections. Here's what we can do:

        [Suggested Actions:]
        • Check connection pool metrics
        • Review application logs
        • Analyze database slow queries
        """

        mock_llm_client.route.return_value = LLMResponse(
            content=mock_response_content,
            model="gpt-4",
            tokens_used=240, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_999",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '{"current_phase": 4, "new_tests_performed": ["connection pool metrics check requested"]}'
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="What should I check next?",
            case=active_problem_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify response contains guidance
        assert "connection pool" in response.answer.lower()
        assert updated_state.current_phase == 4  # Validation

    @pytest.mark.asyncio
    async def test_case_resolution(self, mock_llm_client, active_problem_case):
        """Test case resolution detection and runbook offer."""
        # Mock LLM response - problem resolved
        mock_llm_client.route.return_value = LLMResponse(
            content="Great! The API is back up. Glad we solved it! To help with future troubleshooting, would you like me to create a runbook for this issue?",
            model="gpt-4",
            tokens_used=290, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_final",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "current_phase": 5,
                            "solution_proposed": true,
                            "case_resolved": true,
                            "root_cause": "Database connection pool exhaustion due to connection leak in new code",
                            "solution_description": "Increased pool size and fixed connection leak"
                        }'''
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="Yes! That fixed it. The API is working now.",
            case=active_problem_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify resolution
        assert updated_state.case_resolved is True
        assert updated_state.solution_proposed is True
        assert updated_state.root_cause is not None
        assert "runbook" in response.answer.lower()

    @pytest.mark.asyncio
    async def test_json_fallback_when_no_function_call(self, mock_llm_client, empty_case):
        """Test JSON parsing fallback when LLM doesn't use function calling."""
        # Mock LLM response without tool_calls but with JSON in content
        mock_llm_client.route.return_value = LLMResponse(
            content="""Hello! How can I help you today?

            {"diagnostic_state_updates": {"has_active_problem": false, "current_phase": 0}}""",
            model="gpt-4",
            tokens_used=135, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=None
        )

        response, updated_state = await process_turn(
            user_query="hi",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.MINIMAL
        )

        # Verify JSON fallback worked
        assert updated_state.has_active_problem is False
        assert updated_state.current_phase == 0

    @pytest.mark.asyncio
    async def test_heuristic_fallback(self, mock_llm_client, empty_case):
        """Test heuristic fallback when function calling and JSON both fail."""
        # Mock LLM response with no function call and no valid JSON
        mock_llm_client.route.return_value = LLMResponse(
            content="I see you're experiencing errors. Let's troubleshoot this API issue.",
            model="gpt-4",
            tokens_used=145, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=None
        )

        response, updated_state = await process_turn(
            user_query="My service keeps crashing with errors",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.MINIMAL
        )

        # Verify heuristic detection worked (keywords: "crashing", "errors")
        assert updated_state.has_active_problem is True
        assert updated_state.urgency_level in [UrgencyLevel.HIGH, UrgencyLevel.HIGH]

    @pytest.mark.asyncio
    async def test_prompt_version_selection(self, mock_llm_client, empty_case):
        """Test different prompt versions produce different token estimates."""
        mock_llm_client.route.return_value = LLMResponse(
            content="Test response",
            model="gpt-4",
            tokens_used=110, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=None
        )

        # Test minimal version
        await process_turn(
            user_query="test",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.MINIMAL
        )
        minimal_call_args = mock_llm_client.route.call_args

        # Test detailed version
        await process_turn(
            user_query="test",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.DETAILED
        )
        detailed_call_args = mock_llm_client.route.call_args

        # Detailed prompt should be longer
        minimal_prompt = minimal_call_args.kwargs["prompt"]
        detailed_prompt = detailed_call_args.kwargs["prompt"]
        assert len(detailed_prompt) > len(minimal_prompt)

    @pytest.mark.asyncio
    async def test_conversation_history_included(self, mock_llm_client, active_problem_case):
        """Test that conversation history is included in prompt."""
        mock_llm_client.route.return_value = LLMResponse(
            content="Based on what you told me earlier about the 500 errors...",
            model="gpt-4",
            tokens_used=220, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=None
        )

        await process_turn(
            user_query="What do you think is causing this?",
            case=active_problem_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify history was included
        call_args = mock_llm_client.route.call_args
        prompt = call_args.kwargs["prompt"]
        assert "500 errors" in prompt  # From previous messages

    @pytest.mark.asyncio
    async def test_urgency_escalation(self, mock_llm_client, empty_case):
        """Test urgency level escalation for critical issues."""
        mock_llm_client.route.return_value = LLMResponse(
            content="This is critical! Your production database is down. Let's act fast.",
            model="gpt-4",
            tokens_used=160, confidence=0.95, provider="test", response_time_ms=100,
            tool_calls=[
                ToolCall(
                    id="call_critical",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '{"has_active_problem": true, "urgency_level": "critical", "problem_statement": "Production database is down", "current_phase": 1}'
                    }
                )
            ]
        )

        response, updated_state = await process_turn(
            user_query="URGENT: Production database is completely down!",
            case=empty_case,
            llm_client=mock_llm_client,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify critical urgency
        assert updated_state.urgency_level == UrgencyLevel.CRITICAL
        assert updated_state.has_active_problem is True


@pytest.mark.integration
class TestTurnProcessorIntegration:
    """Integration tests for turn processor (require real LLM)."""

    @pytest.mark.skip(reason="Requires real LLM API - manual testing only")
    @pytest.mark.asyncio
    async def test_real_llm_greeting(self):
        """Integration test with real LLM - greeting flow."""
        from faultmaven.container import FaultMavenContainer

        container = FaultMavenContainer()
        llm = container.get_llm_provider()

        case = Case(
            case_id="integration-test-001",
            title="Integration Test",
            status=CaseStatus.ACTIVE,
            diagnostic_state=CaseDiagnosticState()
        )

        response, state = await process_turn(
            user_query="Hello, can you help me?",
            case=case,
            llm_client=llm,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify natural greeting response
        assert len(response.answer) > 0
        assert state.has_active_problem is False

    @pytest.mark.skip(reason="Requires real LLM API - manual testing only")
    @pytest.mark.asyncio
    async def test_real_llm_problem_diagnosis(self):
        """Integration test with real LLM - problem diagnosis."""
        from faultmaven.container import FaultMavenContainer

        container = FaultMavenContainer()
        llm = container.get_llm_provider()

        case = Case(
            case_id="integration-test-002",
            title="API Error Test",
            status=CaseStatus.ACTIVE,
            diagnostic_state=CaseDiagnosticState()
        )

        response, state = await process_turn(
            user_query="My Kubernetes pod keeps crashing with OOMKilled error",
            case=case,
            llm_client=llm,
            prompt_version=PromptVersion.STANDARD
        )

        # Verify problem detection
        assert state.has_active_problem is True
        assert "oom" in state.problem_statement.lower() or "memory" in state.problem_statement.lower()
        assert state.current_phase >= 1
