"""End-to-End Integration tests for Agent Execution API (TASK-016, TASK-020).

Tests:
- Complete streaming workflow with mocked services
- Tool call streaming
- Non-streaming mode workflow
- Error handling across the stack
- Session state validation
- Token budget enforcement
- Multi-turn conversation context

Note: As of TASK-020, JWT authentication is required. Legacy header authentication
(X-Organization-ID, X-User-ID) has been removed.
"""

import json
from datetime import datetime, timezone
from typing import AsyncGenerator, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
)
from faultmaven.main import app as main_app
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.events.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_user():
    """Create a mock authenticated user for testing."""
    return AuthenticatedUser(
        user_id="user_789",
        organization_id="org_456",
        email="test@example.com",
        roles=["admin"],
        permissions=["sessions:execute", "executions:read", "executions:cancel"],
    )


@pytest.fixture
def mock_session():
    """Create a mock InvestigationSession."""
    session = MagicMock(spec=InvestigationSession)
    session.session_id = "session_test123"
    session.case_id = "case_456def"
    session.user_id = "user_789"
    session.organization_id = "org_456"
    session.status = SessionStatus.ACTIVE
    session.total_token_usage = 1000
    session.token_budget_limit = 50000
    session.session_goal = "Debug the API errors"
    session.started_at = datetime.now(timezone.utc)
    session.created_at = datetime.now(timezone.utc)
    session.updated_at = datetime.now(timezone.utc)
    return session


@pytest.fixture
def mock_execution():
    """Create a mock AgentExecution."""
    execution = MagicMock(spec=AgentExecution)
    execution.execution_id = "exec_test123"
    execution.case_id = "case_456def"
    execution.agent_type = AgentType.INVESTIGATOR
    execution.agent_model = "claude-3-opus"
    execution.status = ExecutionStatus.COMPLETED
    execution.started_at = datetime.now(timezone.utc)
    execution.completed_at = datetime.now(timezone.utc)
    execution.created_at = datetime.now(timezone.utc)
    execution.response = (
        "Based on my analysis, the error is caused by a database connection timeout."
    )
    execution.prompt = "What is causing the errors?"
    execution.error_message = None
    execution.token_usage = {
        "prompt_tokens": 100,
        "completion_tokens": 200,
        "total_tokens": 300,
    }
    execution.tool_calls = []
    execution.get_total_tokens.return_value = 300
    return execution


@pytest.fixture
def mock_tool_call():
    """Create a mock AgentToolCall."""
    tool_call = MagicMock(spec=AgentToolCall)
    tool_call.tool_call_id = "tc_123abc"
    tool_call.execution_id = "exec_test123"
    tool_call.tool_name = "read_file"
    tool_call.tool_input = {"evidence_id": "ev_456def"}
    tool_call.tool_output = {"content": "Error log contents..."}
    tool_call.status = "success"
    tool_call.error_message = None
    return tool_call


@pytest.fixture
def mock_agent_service():
    """Create a mock AgentOrchestrationService."""
    service = AsyncMock()
    return service


@pytest.fixture
def app(mock_agent_service, mock_user):
    """Create test application with mocked dependencies."""
    app = main_app

    async def get_mock_agent_service():
        return mock_agent_service

    async def get_mock_current_user():
        return mock_user

    from faultmaven.api.dependencies import get_agent_orchestration_service
    from faultmaven.api.middleware.auth import get_current_user

    app.dependency_overrides[get_agent_orchestration_service] = get_mock_agent_service
    app.dependency_overrides[get_current_user] = get_mock_current_user

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def headers():
    """Standard request headers (auth is mocked via dependency override)."""
    return {}


# ============================================================
# End-to-End Streaming Workflow Tests
# ============================================================


class TestE2EStreamingWorkflow:
    """End-to-end tests for streaming workflow."""

    def test_complete_streaming_workflow(
        self, client, mock_agent_service, mock_execution, headers
    ):
        """Test complete streaming workflow from start to finish."""

        async def mock_execute(*args, **kwargs):
            # Simulate full execution workflow
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={
                    "agent_type": "investigator",
                    "session_id": "session_test123",
                },
            )
            yield ExecutionEvent.thinking(
                content="Analyzing the problem context...",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.thinking(
                content="Reviewing available evidence...",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="Based on my analysis, ",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="the error appears to be caused by a database timeout.",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=300,
                duration_ms=2500,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={
                "user_message": "What is causing the database errors?",
                "agent_type": "investigator",
                "stream": True,
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert "text/event-stream" in response.headers["content-type"]

        content = response.text
        events = content.split("\n\n")

        # Verify all expected events are present
        event_types = [e.split("\n")[0] for e in events if e.strip()]
        assert any("started" in e for e in event_types)
        assert any("thinking" in e for e in event_types)
        assert any("response" in e for e in event_types)
        assert any("completed" in e for e in event_types)

    def test_streaming_events_in_order(self, client, mock_agent_service, headers):
        """Test streaming events arrive in correct order."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={"order": 1},
            )
            yield ExecutionEvent.thinking(
                content="Step 1",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="Response part 1",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=100,
                duration_ms=1000,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        started_pos = content.find("event: started")
        thinking_pos = content.find("event: thinking")
        response_pos = content.find("event: response")
        completed_pos = content.find("event: completed")

        # Verify order
        assert started_pos < thinking_pos < response_pos < completed_pos


# ============================================================
# Tool Call Streaming Tests
# ============================================================


class TestE2EToolCallStreaming:
    """End-to-end tests for tool call streaming."""

    def test_streaming_with_tool_calls(self, client, mock_agent_service, headers):
        """Test streaming includes tool call and result events."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={},
            )
            yield ExecutionEvent.thinking(
                content="I need to read the error log file...",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.tool_call(
                tool_name="read_file",
                tool_input={"evidence_id": "ev_log123"},
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.tool_result(
                tool_name="read_file",
                result="ERROR: Connection timeout at line 45\nERROR: Database unavailable",
                success=True,
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="Based on the log file, I can see connection timeout errors.",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=500,
                duration_ms=3000,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Read the error log", "stream": True},
            headers=headers,
        )

        content = response.text

        # Verify tool events present
        assert "event: tool_call" in content
        assert "event: tool_result" in content
        assert "read_file" in content
        assert "ev_log123" in content

    def test_streaming_with_multiple_tool_calls(
        self, client, mock_agent_service, headers
    ):
        """Test streaming with multiple sequential tool calls."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={},
            )
            # First tool call
            yield ExecutionEvent.tool_call(
                tool_name="list_evidence",
                tool_input={},
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.tool_result(
                tool_name="list_evidence",
                result='[{"id": "ev_1", "name": "app.log"}, {"id": "ev_2", "name": "error.log"}]',
                success=True,
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            # Second tool call
            yield ExecutionEvent.tool_call(
                tool_name="read_file",
                tool_input={"evidence_id": "ev_2"},
                tool_call_id="tc_002",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.tool_result(
                tool_name="read_file",
                result="Error log contents...",
                success=True,
                tool_call_id="tc_002",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="I found two log files and analyzed the error log.",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=800,
                duration_ms=5000,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Analyze all evidence", "stream": True},
            headers=headers,
        )

        content = response.text

        # Count tool events
        assert content.count("event: tool_call") == 2
        assert content.count("event: tool_result") == 2

    def test_streaming_tool_call_failure(self, client, mock_agent_service, headers):
        """Test streaming when a tool call fails."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={},
            )
            yield ExecutionEvent.tool_call(
                tool_name="read_file",
                tool_input={"evidence_id": "nonexistent"},
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.tool_result(
                tool_name="read_file",
                result="Error: Evidence file not found",
                success=False,
                tool_call_id="tc_001",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="I was unable to read the file. It may have been deleted.",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=300,
                duration_ms=2000,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Read the missing file", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: tool_result" in content
        # Agent should still complete despite tool failure


# ============================================================
# Non-Streaming Mode Tests
# ============================================================


class TestE2ENonStreamingMode:
    """End-to-end tests for non-streaming mode."""

    def test_non_streaming_complete_response(
        self, client, mock_agent_service, mock_execution, headers
    ):
        """Test non-streaming returns complete response."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={},
            )
            yield ExecutionEvent.response(
                content="Part 1 of the response. ",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.response(
                content="Part 2 of the response.",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=300,
                duration_ms=2000,
            )

        mock_agent_service.execute_agent = mock_execute
        mock_agent_service.get_execution.return_value = mock_execution

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={
                "user_message": "What is the issue?",
                "stream": False,
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]

        data = response.json()
        assert data["execution_id"] == "exec_test123"
        assert data["status"] == "completed"
        assert "tokens_used" in data

    def test_non_streaming_with_tool_calls(
        self, client, mock_agent_service, mock_execution, mock_tool_call, headers
    ):
        """Test non-streaming includes tool calls in response."""
        mock_execution.tool_calls = [mock_tool_call]

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={},
            )
            yield ExecutionEvent.tool_call(
                tool_name="read_file",
                tool_input={"evidence_id": "ev_456def"},
                tool_call_id="tc_123abc",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=400,
                duration_ms=3000,
            )

        mock_agent_service.execute_agent = mock_execute
        mock_agent_service.get_execution.return_value = mock_execution

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Read the file", "stream": False},
            headers=headers,
        )

        data = response.json()
        assert len(data["tool_calls"]) == 1
        assert data["tool_calls"][0]["tool_name"] == "read_file"


# ============================================================
# Error Handling Tests
# ============================================================


class TestE2EErrorHandling:
    """End-to-end tests for error handling."""

    def test_session_not_found_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for session not found."""

        async def mock_execute(*args, **kwargs):
            raise NotFoundError("Session", "nonexistent_session")
            yield  # Make it a generator

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/nonexistent_session/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        assert "not_found" in content

    def test_authorization_error_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for authorization failure."""

        async def mock_execute(*args, **kwargs):
            raise AuthorizationError("Session does not belong to organization")
            yield

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        assert "forbidden" in content

    def test_session_not_active_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for inactive session."""

        async def mock_execute(*args, **kwargs):
            raise ConflictError(
                "Session is paused",
                resource_type="Session",
                resource_id="session_test123",
                conflict_reason="session_paused",
            )
            yield

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        assert "conflict" in content

    def test_budget_exceeded_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for budget exceeded."""

        async def mock_execute(*args, **kwargs):
            raise ConflictError(
                "Token budget exceeded for session",
                resource_type="Session",
                resource_id="session_test123",
                conflict_reason="budget_exceeded",
            )
            yield

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        assert "conflict" in content

    def test_llm_error_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for LLM failure."""

        async def mock_execute(*args, **kwargs):
            raise LLMException("Rate limit exceeded. Please try again later.")
            yield

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        assert "llm_error" in content

    def test_unexpected_error_streaming(self, client, mock_agent_service, headers):
        """Test streaming error for unexpected exception."""

        async def mock_execute(*args, **kwargs):
            raise RuntimeError("Unexpected internal error")
            yield

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        assert "event: error" in content
        # Should not expose internal error message
        assert "internal_error" in content or "unexpected" in content.lower()


# ============================================================
# Agent Type Tests
# ============================================================


class TestE2EAgentTypes:
    """End-to-end tests for different agent types."""

    @pytest.mark.parametrize(
        "agent_type,expected_type",
        [
            ("investigator", AgentType.INVESTIGATOR),
            ("debugger", AgentType.DEBUGGER),
            ("researcher", AgentType.RESEARCHER),
            ("validator", AgentType.VALIDATOR),
            ("reporter", AgentType.REPORTER),
        ],
    )
    def test_agent_type_passed_correctly(
        self, client, mock_agent_service, headers, agent_type, expected_type
    ):
        """Test agent type is passed correctly to service."""
        captured_kwargs = {}

        async def mock_execute(*args, **kwargs):
            captured_kwargs.update(kwargs)
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=100,
                duration_ms=500,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={
                "user_message": "Test",
                "agent_type": agent_type,
                "stream": True,
            },
            headers=headers,
        )

        # Verify the agent type was passed correctly
        assert captured_kwargs["agent_type"] == expected_type


# ============================================================
# Execution Management Tests
# ============================================================


class TestE2EExecutionManagement:
    """End-to-end tests for execution list/get/cancel."""

    def test_list_and_get_execution_workflow(
        self, client, mock_agent_service, mock_execution, headers
    ):
        """Test listing and getting executions."""
        mock_agent_service.list_executions.return_value = ([mock_execution], 1)
        mock_agent_service.get_execution.return_value = mock_execution

        # List executions
        list_response = client.get(
            "/api/v1/cases/case_456def/sessions/session_test123/executions",
            headers=headers,
        )
        assert list_response.status_code == status.HTTP_200_OK
        assert len(list_response.json()) == 1

        # Get specific execution
        get_response = client.get(
            "/api/v1/cases/case_456def/sessions/session_test123/executions/exec_test123",
            headers=headers,
        )
        assert get_response.status_code == status.HTTP_200_OK
        assert get_response.json()["execution_id"] == "exec_test123"

    def test_cancel_running_execution(self, client, mock_agent_service, headers):
        """Test cancelling a running execution."""
        mock_agent_service.cancel_execution.return_value = True

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/executions/exec_test123/cancel",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "cancelled"


# ============================================================
# Response Format Validation Tests
# ============================================================


class TestE2EResponseFormat:
    """End-to-end tests for response format validation."""

    def test_sse_event_json_validity(self, client, mock_agent_service, headers):
        """Test all SSE events contain valid JSON data."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.started(
                execution_id="exec_test123",
                metadata={"key": "value"},
            )
            yield ExecutionEvent.thinking(
                content="Processing...",
                execution_id="exec_test123",
            )
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=100,
                duration_ms=500,
            )

        mock_agent_service.execute_agent = mock_execute

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": True},
            headers=headers,
        )

        content = response.text
        events = [e for e in content.split("\n\n") if e.strip()]

        for event in events:
            lines = event.strip().split("\n")
            for line in lines:
                if line.startswith("data: "):
                    data_json = line[6:]
                    # Should parse as valid JSON
                    parsed = json.loads(data_json)
                    assert isinstance(parsed, dict)

    def test_non_streaming_response_structure(
        self, client, mock_agent_service, mock_execution, headers
    ):
        """Test non-streaming response has correct structure."""

        async def mock_execute(*args, **kwargs):
            yield ExecutionEvent.completed(
                execution_id="exec_test123",
                total_tokens=300,
                duration_ms=2000,
            )

        mock_agent_service.execute_agent = mock_execute
        mock_agent_service.get_execution.return_value = mock_execution

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_test123/execute",
            json={"user_message": "Test", "stream": False},
            headers=headers,
        )

        data = response.json()

        # Verify required fields
        assert "execution_id" in data
        assert "status" in data
        assert "agent_response" in data
        assert "tokens_used" in data
        assert "started_at" in data
        assert "tool_calls" in data
        assert isinstance(data["tool_calls"], list)
