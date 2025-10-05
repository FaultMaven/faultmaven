"""Integration tests for doctor/patient prompting workflow.

These tests verify the complete end-to-end flow through the doctor/patient
architecture, from user query to response with state updates.
"""

import pytest
from unittest.mock import AsyncMock, patch, Mock
from datetime import datetime

from faultmaven.models import (
    Case,
    CaseStatus,
    CaseDiagnosticState,
    UrgencyLevel,
    QueryRequest,
    QueryContext,
    AgentResponse,
    MessageType
)
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.services.agentic.orchestration.agent_service import AgentService
from faultmaven.prompts.doctor_patient import PromptVersion


@pytest.fixture
def mock_container():
    """Mock FaultMaven container with all dependencies."""
    container = Mock()

    # Mock LLM provider
    llm = AsyncMock()
    container.get_llm_provider.return_value = llm

    # Mock other services
    container.get_tools.return_value = []
    container.get_tracer.return_value = Mock()
    container.get_sanitizer.return_value = Mock(sanitize=lambda x: x)

    return container


@pytest.fixture
def mock_case_service():
    """Mock case service."""
    service = AsyncMock()

    # Default case
    service.get_case.return_value = Case(
        case_id="test-case-001",
        title="Test Case",
        status=CaseStatus.ACTIVE,
        session_id="test-session-001",
        diagnostic_state=CaseDiagnosticState(),
        messages=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    service.update_case.return_value = None

    return service


@pytest.fixture
def mock_session_service():
    """Mock session service."""
    service = AsyncMock()
    service.add_message.return_value = None
    service.format_conversation_context_token_aware.return_value = "No previous conversation"
    return service


@pytest.mark.integration
class TestDoctorPatientWorkflow:
    """Integration tests for complete doctor/patient workflow."""

    @pytest.mark.asyncio
    async def test_greeting_flow(self, mock_container, mock_case_service, mock_session_service):
        """Test complete greeting flow - no problem detection."""
        # Setup LLM response
        llm = mock_container.get_llm_provider()
        llm.route.return_value = LLMResponse(
            content="Hello! I'm here to help with troubleshooting. What brings you here today?",
            model="gpt-4",
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            tool_calls=[
                ToolCall(
                    id="call_greeting",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '{"has_active_problem": false, "current_phase": 0}'
                    }
                )
            ]
        )

        # Create agent service
        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        # Execute query
        request = QueryRequest(
            query="hello",
            session_id="test-session-001",
            context=QueryContext()
        )

        response = await agent_service.process_query_with_case(
            case_id="test-case-001",
            request=request
        )

        # Verify response
        assert isinstance(response, AgentResponse)
        assert "hello" in response.answer.lower() or "help" in response.answer.lower()

        # Verify case was updated with no active problem
        assert mock_case_service.update_case.called
        update_call = mock_case_service.update_case.call_args
        assert "diagnostic_state" in update_call.kwargs["updates"]

    @pytest.mark.asyncio
    async def test_problem_detection_flow(self, mock_container, mock_case_service, mock_session_service):
        """Test problem detection and initial diagnosis."""
        # Setup LLM response - detects problem
        llm = mock_container.get_llm_provider()
        llm.route.return_value = LLMResponse(
            content="I see you're experiencing 500 errors. Let's diagnose this. When did these errors start?",
            model="gpt-4",
            usage={"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180},
            tool_calls=[
                ToolCall(
                    id="call_problem",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "has_active_problem": true,
                            "problem_statement": "API returning 500 errors",
                            "current_phase": 1,
                            "urgency_level": "high",
                            "new_symptoms": ["500 errors", "API failures"]
                        }'''
                    }
                )
            ]
        )

        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        request = QueryRequest(
            query="My API is returning 500 errors constantly",
            session_id="test-session-001",
            context=QueryContext()
        )

        response = await agent_service.process_query_with_case(
            case_id="test-case-001",
            request=request
        )

        # Verify problem detection
        assert "500" in response.answer or "error" in response.answer.lower()

        # Verify state update
        update_call = mock_case_service.update_case.call_args
        diagnostic_state = update_call.kwargs["updates"]["diagnostic_state"]
        assert diagnostic_state["has_active_problem"] is True
        assert "500" in diagnostic_state["problem_statement"].lower()

    @pytest.mark.asyncio
    async def test_multi_turn_diagnosis_flow(self, mock_container, mock_case_service, mock_session_service):
        """Test multi-turn diagnostic conversation."""
        llm = mock_container.get_llm_provider()

        # Turn 1: Initial problem
        mock_case_service.get_case.return_value = Case(
            case_id="test-case-002",
            title="Multi-turn Test",
            status=CaseStatus.ACTIVE,
            session_id="test-session-002",
            diagnostic_state=CaseDiagnosticState(),
            messages=[],
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        llm.route.return_value = LLMResponse(
            content="Let's diagnose this database slowness. What changed recently?",
            model="gpt-4",
            usage={"prompt_tokens": 140, "completion_tokens": 25, "total_tokens": 165},
            tool_calls=[
                ToolCall(
                    id="call_t1",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "has_active_problem": true,
                            "problem_statement": "Database queries are slow",
                            "current_phase": 1,
                            "new_symptoms": ["slow queries"]
                        }'''
                    }
                )
            ]
        )

        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        request1 = QueryRequest(
            query="My database queries are very slow",
            session_id="test-session-002",
            context=QueryContext()
        )

        response1 = await agent_service.process_query_with_case(
            case_id="test-case-002",
            request=request1
        )

        assert "slow" in response1.answer.lower()

        # Turn 2: Timeline information
        mock_case_service.get_case.return_value = Case(
            case_id="test-case-002",
            title="Multi-turn Test",
            status=CaseStatus.ACTIVE,
            session_id="test-session-002",
            diagnostic_state=CaseDiagnosticState(
                has_active_problem=True,
                problem_statement="Database queries are slow",
                current_phase=1,
                symptoms=["slow queries"]
            ),
            messages=[],
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        llm.route.return_value = LLMResponse(
            content="Since it started after deployment, let's check for these issues...",
            model="gpt-4",
            usage={"prompt_tokens": 180, "completion_tokens": 40, "total_tokens": 220},
            tool_calls=[
                ToolCall(
                    id="call_t2",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "current_phase": 2,
                            "timeline_updates": {"started": "after deployment", "trigger_event": "v2.3 deploy"}
                        }'''
                    }
                )
            ]
        )

        request2 = QueryRequest(
            query="It started right after we deployed version 2.3 yesterday",
            session_id="test-session-002",
            context=QueryContext()
        )

        response2 = await agent_service.process_query_with_case(
            case_id="test-case-002",
            request=request2
        )

        # Verify phase progression
        update_call = mock_case_service.update_case.call_args
        diagnostic_state = update_call.kwargs["updates"]["diagnostic_state"]
        assert diagnostic_state["current_phase"] == 2  # Timeline phase

    @pytest.mark.asyncio
    async def test_case_resolution_flow(self, mock_container, mock_case_service, mock_session_service):
        """Test complete flow to case resolution with runbook offer."""
        llm = mock_container.get_llm_provider()

        # Setup case with active problem
        mock_case_service.get_case.return_value = Case(
            case_id="test-case-003",
            title="Resolution Test",
            status=CaseStatus.ACTIVE,
            session_id="test-session-003",
            diagnostic_state=CaseDiagnosticState(
                has_active_problem=True,
                problem_statement="Memory leak in application",
                current_phase=4,  # Validation phase
                root_cause="",
                solution_proposed=False
            ),
            messages=[],
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        # LLM detects resolution
        llm.route.return_value = LLMResponse(
            content="Excellent! The memory issue is resolved. Glad we solved it! To help with future troubleshooting, would you like me to create a runbook for this issue?",
            model="gpt-4",
            usage={"prompt_tokens": 200, "completion_tokens": 35, "total_tokens": 235},
            tool_calls=[
                ToolCall(
                    id="call_resolved",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "current_phase": 5,
                            "root_cause": "Memory leak in user session management",
                            "solution_proposed": true,
                            "solution_description": "Fixed session cleanup logic",
                            "case_resolved": true
                        }'''
                    }
                )
            ]
        )

        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        request = QueryRequest(
            query="Yes! The fix worked. Memory is stable now.",
            session_id="test-session-003",
            context=QueryContext()
        )

        response = await agent_service.process_query_with_case(
            case_id="test-case-003",
            request=request
        )

        # Verify resolution and runbook offer
        assert "runbook" in response.answer.lower()

        # Verify state update shows resolution
        update_call = mock_case_service.update_case.call_args
        diagnostic_state = update_call.kwargs["updates"]["diagnostic_state"]
        assert diagnostic_state["case_resolved"] is True
        assert diagnostic_state["solution_proposed"] is True

    @pytest.mark.asyncio
    async def test_urgency_escalation(self, mock_container, mock_case_service, mock_session_service):
        """Test urgency level escalation for critical issues."""
        llm = mock_container.get_llm_provider()

        llm.route.return_value = LLMResponse(
            content="This is CRITICAL! Your production database is down. Let's act immediately.",
            model="gpt-4",
            usage={"prompt_tokens": 120, "completion_tokens": 25, "total_tokens": 145},
            tool_calls=[
                ToolCall(
                    id="call_critical",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "has_active_problem": true,
                            "problem_statement": "Production database is completely down",
                            "urgency_level": "critical",
                            "current_phase": 1
                        }'''
                    }
                )
            ]
        )

        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        request = QueryRequest(
            query="URGENT!!! Production database is completely DOWN! All users affected!",
            session_id="test-session-004",
            context=QueryContext()
        )

        response = await agent_service.process_query_with_case(
            case_id="test-case-004",
            request=request
        )

        # Verify critical urgency detected
        update_call = mock_case_service.update_case.call_args
        diagnostic_state = update_call.kwargs["updates"]["diagnostic_state"]
        assert diagnostic_state["urgency_level"] == "critical"

    @pytest.mark.asyncio
    async def test_informational_query_handling(self, mock_container, mock_case_service, mock_session_service):
        """Test handling of informational queries (no problem)."""
        llm = mock_container.get_llm_provider()

        llm.route.return_value = LLMResponse(
            content="Redis is an in-memory data store that supports various data structures. It's great for caching and session storage.",
            model="gpt-4",
            usage={"prompt_tokens": 110, "completion_tokens": 30, "total_tokens": 140},
            tool_calls=[
                ToolCall(
                    id="call_info",
                    type="function",
                    function={
                        "name": "update_diagnostic_state",
                        "arguments": '''{
                            "has_active_problem": false,
                            "current_phase": 0
                        }'''
                    }
                )
            ]
        )

        agent_service = AgentService(
            llm_provider=llm,
            tools=[],
            tracer=Mock(),
            sanitizer=Mock(sanitize=lambda x: x),
            session_service=mock_session_service,
            case_service=mock_case_service,
            settings=Mock(prompts=Mock(doctor_patient_version="standard")),
            prompt_version="standard"
        )

        request = QueryRequest(
            query="What's the difference between Redis and Memcached?",
            session_id="test-session-005",
            context=QueryContext()
        )

        response = await agent_service.process_query_with_case(
            case_id="test-case-005",
            request=request
        )

        # Verify educational response
        assert "redis" in response.answer.lower()

        # Verify no problem state
        update_call = mock_case_service.update_case.call_args
        diagnostic_state = update_call.kwargs["updates"]["diagnostic_state"]
        assert diagnostic_state["has_active_problem"] is False


@pytest.mark.integration
@pytest.mark.skip(reason="Requires real LLM - manual testing only")
class TestDoctorPatientRealLLM:
    """Integration tests with real LLM (skip in CI)."""

    @pytest.mark.asyncio
    async def test_real_greeting_flow(self):
        """Test greeting with real LLM."""
        from faultmaven.container import FaultMavenContainer

        container = FaultMavenContainer()
        # Test implementation here
        pass

    @pytest.mark.asyncio
    async def test_real_problem_diagnosis(self):
        """Test problem diagnosis with real LLM."""
        from faultmaven.container import FaultMavenContainer

        container = FaultMavenContainer()
        # Test implementation here
        pass
