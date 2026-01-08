"""Test module for session messages & agent chat API endpoints (TASK-027).

This module tests the REST API endpoints for session message management
and simplified agent chat, focusing on HTTP request/response handling,
authentication, validation, and error handling.

Tests cover:
- GET /api/v1/sessions/{session_id}/messages - Message retrieval
- POST /api/v1/agent/chat - Simplified agent chat

Test categories:
1. test_get_messages_* - Message retrieval endpoint tests (8 tests)
2. test_chat_* - Agent chat endpoint tests (7 tests)
3. test_e2e_* - End-to-end workflow tests (3 tests)
"""

import pytest
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from fastapi.testclient import TestClient

from faultmaven.modules.agent.domain.models.agent_execution import AgentExecution, AgentType, ExecutionStatus
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.case.domain.models import Case, CaseStatus
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.exceptions import NotFoundError, AuthorizationError
from faultmaven.main import app


# ============================================================
# Mock Classes
# ============================================================


class MockSessionRepository:
    """Mock implementation of InvestigationSessionRepository for testing."""

    def __init__(self, organization_id: str = "org-test"):
        self.organization_id = organization_id
        self.sessions = {}

    async def get_session(self, session_id: str) -> Optional[InvestigationSession]:
        return self.sessions.get(session_id)

    def add_session(self, session: InvestigationSession):
        self.sessions[session.session_id] = session


class MockCaseRepository:
    """Mock implementation of CaseRepository for testing."""

    def __init__(self, organization_id: str = "org-test"):
        self.organization_id = organization_id
        self.cases = {}

    async def get_case(self, case_id: str) -> Optional[Case]:
        return self.cases.get(case_id)

    def add_case(self, case: Case):
        self.cases[case.case_id] = case


class MockExecutionRepository:
    """Mock implementation of AgentExecutionRepository for testing."""

    def __init__(self):
        self.executions = {}

    async def list_executions_by_session(
        self,
        session_id: str,
        status: Optional[ExecutionStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AgentExecution], int]:
        results = [
            e for e in self.executions.values()
            if e.metadata and e.metadata.get("session_id") == session_id
        ]
        if status:
            results = [e for e in results if e.status == status]
        results.sort(key=lambda e: e.created_at)
        total = len(results)
        return results[offset:offset + limit], total

    def add_execution(self, execution: AgentExecution):
        self.executions[execution.execution_id] = execution


class MockServiceFactory:
    """Mock ServiceFactory for testing."""

    def __init__(
        self,
        session_repo: MockSessionRepository = None,
        case_repo: MockCaseRepository = None,
        execution_repo: MockExecutionRepository = None,
    ):
        self.session_repo = session_repo or MockSessionRepository()
        self.case_repo = case_repo or MockCaseRepository()
        self.execution_repo = execution_repo or MockExecutionRepository()


class MockSessionService:
    """Mock implementation of APIInvestigationSessionService for testing."""

    def __init__(self, organization_id: str = "org-test"):
        self.organization_id = organization_id
        self.sessions = {}
        self.session_counter = 0
        self.create_session = AsyncMock()

    def setup_create_session(self, session: InvestigationSession):
        self.create_session.return_value = session


class MockAgentOrchestrationService:
    """Mock implementation of AgentOrchestrationService for testing."""

    def __init__(self):
        # Use Mock for execute_agent since it's an async generator, not a coroutine
        # Tests should set side_effect to a function returning an async generator
        self.execute_agent = Mock()
        self.get_execution = AsyncMock()


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def auth_user():
    """Fixture providing authenticated test user."""
    return AuthenticatedUser(
        user_id="user-001",
        email="test@example.com",
        organization_id="org-test",
        roles=["member"],
        permissions=[
            "sessions:read",
            "sessions:write",
            "cases:read",
            "cases:write",
            "executions:read",
            "executions:write",
            "agent:execute",
        ],
    )


@pytest.fixture
def sample_session():
    """Fixture providing sample investigation session."""
    return InvestigationSession(
        session_id="sess-123",
        case_id="CASE-2026010100001",
        user_id="user-001",
        organization_id="org-test",
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        total_token_usage=0,
        total_agent_executions=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_case():
    """Fixture providing sample case."""
    case = Mock(spec=Case)
    case.case_id = "CASE-2026010100001"
    case.organization_id = "org-test"
    case.user_id = "user-001"
    case.title = "Test Case"
    case.status = CaseStatus.CONSULTING
    return case


@pytest.fixture
def sample_executions():
    """Fixture providing sample agent executions for message testing."""
    now = datetime.now(timezone.utc)
    return [
        AgentExecution(
            execution_id="exec-001",
            case_id="CASE-2026010100001",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="claude-3-opus",
            status=ExecutionStatus.COMPLETED,
            prompt="What caused the 500 error?",
            response="Based on the logs, the error was caused by a database timeout.",
            started_at=now,
            completed_at=now,
            token_usage={"input": 100, "output": 200, "total": 300},
            tool_calls=[],
            metadata={"session_id": "sess-123"},
            created_at=now,
            updated_at=now,
        ),
        AgentExecution(
            execution_id="exec-002",
            case_id="CASE-2026010100001",
            agent_type=AgentType.INVESTIGATOR,
            agent_model="claude-3-opus",
            status=ExecutionStatus.COMPLETED,
            prompt="How can we fix this?",
            response="I recommend increasing the database connection timeout to 30 seconds.",
            started_at=now,
            completed_at=now,
            token_usage={"input": 150, "output": 250, "total": 400},
            tool_calls=[],
            metadata={"session_id": "sess-123"},
            created_at=now,
            updated_at=now,
        ),
    ]


@pytest.fixture
def mock_factory(sample_session, sample_case, sample_executions):
    """Fixture providing mock service factory with pre-populated data."""
    session_repo = MockSessionRepository()
    session_repo.add_session(sample_session)

    case_repo = MockCaseRepository()
    case_repo.add_case(sample_case)

    execution_repo = MockExecutionRepository()
    for execution in sample_executions:
        execution_repo.add_execution(execution)

    return MockServiceFactory(
        session_repo=session_repo,
        case_repo=case_repo,
        execution_repo=execution_repo,
    )


@pytest.fixture
def mock_session_service(sample_session):
    """Fixture providing mock session service."""
    service = MockSessionService()
    service.setup_create_session(sample_session)
    return service


@pytest.fixture
def mock_agent_service():
    """Fixture providing mock agent orchestration service."""
    return MockAgentOrchestrationService()


@pytest.fixture
def client(auth_user, mock_factory, mock_session_service, mock_agent_service):
    """Fixture providing test client with mocked dependencies."""
    from faultmaven.api.dependencies import (
        get_service_factory,
        get_agent_orchestration_service,
        get_investigation_session_service,
    )
    from faultmaven.api.middleware.auth import get_current_user

    # Override dependencies
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_service_factory] = lambda: mock_factory
    app.dependency_overrides[get_investigation_session_service] = lambda: mock_session_service
    app.dependency_overrides[get_agent_orchestration_service] = lambda: mock_agent_service

    client = TestClient(app)
    yield client

    # Clean up overrides
    app.dependency_overrides.clear()


# ============================================================
# GET /sessions/{session_id}/messages Tests
# ============================================================


def test_get_messages_success(client, sample_executions):
    """Test retrieving messages for a valid session returns 200."""
    response = client.get("/api/v1/sessions/sess-123/messages")

    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "sess-123"
    assert len(data["messages"]) == 4  # 2 executions * 2 messages each
    assert data["total_count"] == 4
    assert data["has_more"] is False


def test_get_messages_empty_session(client, mock_factory, sample_session):
    """Test retrieving messages for session with no executions."""
    # Create a new session with no executions
    empty_session = InvestigationSession(
        session_id="sess-empty",
        case_id="CASE-2026010100001",
        user_id="user-001",
        organization_id="org-test",
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        total_token_usage=0,
        total_agent_executions=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    mock_factory.session_repo.add_session(empty_session)

    response = client.get("/api/v1/sessions/sess-empty/messages")

    assert response.status_code == 200
    data = response.json()
    assert len(data["messages"]) == 0
    assert data["total_count"] == 0
    assert data["has_more"] is False


def test_get_messages_pagination(client, mock_factory, sample_session):
    """Test message pagination with limit and offset."""
    response = client.get("/api/v1/sessions/sess-123/messages?limit=2&offset=0")

    assert response.status_code == 200
    data = response.json()
    assert len(data["messages"]) <= 2
    assert data["pagination"]["limit"] == 2
    assert data["pagination"]["offset"] == 0


def test_get_messages_pagination_odd_offset(client, mock_factory, sample_executions):
    """Test message pagination with odd offset returns correct messages.

    Verifies that odd offsets correctly skip into the middle of an execution's
    messages (e.g., offset=1 skips the first user message and starts with
    the first assistant message).
    """
    # With 2 executions, we have 4 messages total:
    # [0] user "What caused the 500 error?"
    # [1] assistant "Based on the logs..."
    # [2] user "How can we fix this?"
    # [3] assistant "I recommend increasing..."

    # Test offset=1 (skip first user message, start with first assistant)
    response = client.get("/api/v1/sessions/sess-123/messages?limit=50&offset=1")

    assert response.status_code == 200
    data = response.json()
    # Should get 3 messages (indices 1, 2, 3)
    assert len(data["messages"]) == 3
    assert data["total_count"] == 4
    # First message should be assistant (the response to first prompt)
    assert data["messages"][0]["role"] == "assistant"
    assert "Based on the logs" in data["messages"][0]["content"]


def test_get_messages_pagination_odd_offset_small_limit(client, mock_factory, sample_executions):
    """Test odd offset with small limit returns correct count."""
    # Test offset=1 with limit=2 should return exactly 2 messages
    response = client.get("/api/v1/sessions/sess-123/messages?limit=2&offset=1")

    assert response.status_code == 200
    data = response.json()
    assert len(data["messages"]) == 2
    assert data["has_more"] is True
    assert data["pagination"]["next_offset"] == 3  # 1 + 2


def test_get_messages_not_found(client):
    """Test retrieving messages for non-existent session returns 404."""
    response = client.get("/api/v1/sessions/sess-nonexistent/messages")

    assert response.status_code == 404


def test_get_messages_forbidden(client, mock_factory):
    """Test retrieving messages for session in different org returns 403."""
    # Create session in different organization
    other_org_session = InvestigationSession(
        session_id="sess-other-org",
        case_id="CASE-2026010100001",
        user_id="user-002",
        organization_id="org-other",  # Different org
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        total_token_usage=0,
        total_agent_executions=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    mock_factory.session_repo.add_session(other_org_session)

    response = client.get("/api/v1/sessions/sess-other-org/messages")

    assert response.status_code == 403


def test_get_messages_order(client, sample_executions):
    """Test messages are returned in chronological order."""
    response = client.get("/api/v1/sessions/sess-123/messages")

    assert response.status_code == 200
    data = response.json()
    messages = data["messages"]

    # First message should be from first execution
    assert messages[0]["content"] == sample_executions[0].prompt
    assert messages[0]["role"] == "user"


def test_get_messages_metadata_included(client):
    """Test message metadata includes token usage and tool calls."""
    response = client.get("/api/v1/sessions/sess-123/messages")

    assert response.status_code == 200
    data = response.json()

    # Find an assistant message
    assistant_messages = [m for m in data["messages"] if m["role"] == "assistant"]
    assert len(assistant_messages) > 0

    # Check metadata is present
    msg = assistant_messages[0]
    assert "metadata" in msg
    assert msg["metadata"]["execution_id"] is not None
    assert msg["metadata"]["agent_type"] == "investigator"


def test_get_messages_max_limit(client):
    """Test limit is capped at 100."""
    response = client.get("/api/v1/sessions/sess-123/messages?limit=200")

    # FastAPI validation should reject limit > 100
    assert response.status_code == 422


# ============================================================
# POST /agent/chat Tests
# ============================================================


def test_chat_success_non_streaming(client, mock_agent_service, sample_session):
    """Test non-streaming chat returns complete response."""
    from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent, ExecutionEventType

    # Setup mock agent to yield events
    async def mock_execute(*args, **kwargs):
        yield ExecutionEvent.started(
            execution_id="exec-new",
            metadata={"agent_type": "investigator"},
        )
        yield ExecutionEvent.response(content="This is the response.")
        yield ExecutionEvent.completed(
            execution_id="exec-new",
            total_tokens=300,
            duration_ms=1000,
        )

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute()

    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": "sess-123",
            "message": "What's the issue?",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["execution_id"] == "exec-new"
    assert data["session_id"] == "sess-123"
    assert "message" in data
    assert data["message"]["role"] == "assistant"


def test_chat_auto_create_session(client, mock_agent_service, mock_session_service, sample_session):
    """Test chat auto-creates session when session_id is not provided."""
    from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent

    # Setup mock agent to yield events
    async def mock_execute(*args, **kwargs):
        yield ExecutionEvent.started(
            execution_id="exec-auto",
            metadata={"agent_type": "investigator"},
        )
        yield ExecutionEvent.response(content="Response content.")
        yield ExecutionEvent.completed(
            execution_id="exec-auto",
            total_tokens=200,
            duration_ms=500,
        )

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute()

    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "message": "New conversation",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    # Session service should have been called to create session
    mock_session_service.create_session.assert_called_once()


def test_chat_case_not_found(client):
    """Test chat with non-existent case returns 404."""
    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-NONEXISTENT",
            "message": "Hello",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    assert response.status_code == 404


def test_chat_case_forbidden(client, mock_factory):
    """Test chat with case from different org returns 403."""
    # Create case in different organization
    other_case = Mock(spec=Case)
    other_case.case_id = "CASE-OTHER"
    other_case.organization_id = "org-other"  # Different org
    mock_factory.case_repo.add_case(other_case)

    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-OTHER",
            "message": "Hello",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    assert response.status_code == 403


def test_chat_session_case_mismatch(client, mock_factory, sample_case):
    """Test chat with session belonging to different case returns 422."""
    # Create session for a different case
    wrong_case_session = InvestigationSession(
        session_id="sess-wrong-case",
        case_id="CASE-DIFFERENT",  # Different from request.case_id
        user_id="user-001",
        organization_id="org-test",
        status=SessionStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        total_token_usage=0,
        total_agent_executions=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    mock_factory.session_repo.add_session(wrong_case_session)

    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",  # Different from session.case_id
            "session_id": "sess-wrong-case",
            "message": "Hello",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    # Should fail validation because session belongs to different case
    assert response.status_code == 422


def test_chat_invalid_agent_type(client):
    """Test chat with invalid agent type returns 422."""
    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": "sess-123",
            "message": "Hello",
            "agent_type": "invalid_type",
            "stream": False,
        },
    )

    assert response.status_code == 422


def test_chat_message_validation(client):
    """Test chat validates message is not empty."""
    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": "sess-123",
            "message": "",  # Empty message
            "agent_type": "investigator",
            "stream": False,
        },
    )

    assert response.status_code == 422


def test_chat_streaming_mode(client, mock_agent_service, sample_session):
    """Test streaming chat returns SSE response."""
    from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent

    # Setup mock agent to yield events
    async def mock_execute(*args, **kwargs):
        yield ExecutionEvent.started(
            execution_id="exec-stream",
            metadata={"agent_type": "investigator"},
        )
        yield ExecutionEvent.response(content="Streaming response.")
        yield ExecutionEvent.completed(
            execution_id="exec-stream",
            total_tokens=150,
            duration_ms=800,
        )

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute()

    response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": "sess-123",
            "message": "Stream this",
            "agent_type": "investigator",
            "stream": True,
        },
    )

    # Streaming response should have text/event-stream content type
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")


# ============================================================
# End-to-End Tests
# ============================================================


def test_e2e_chat_and_retrieve(client, mock_factory, mock_agent_service, mock_session_service, sample_session):
    """Test chat followed by message retrieval shows the conversation."""
    from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent

    # First, send a chat message
    async def mock_execute(*args, **kwargs):
        yield ExecutionEvent.started(
            execution_id="exec-e2e",
            metadata={"agent_type": "investigator"},
        )
        yield ExecutionEvent.response(content="E2E test response.")
        yield ExecutionEvent.completed(
            execution_id="exec-e2e",
            total_tokens=200,
            duration_ms=500,
        )

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute()

    chat_response = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": "sess-123",
            "message": "E2E test message",
            "agent_type": "investigator",
            "stream": False,
        },
    )

    assert chat_response.status_code == 200

    # Then retrieve messages (using pre-populated executions)
    messages_response = client.get("/api/v1/sessions/sess-123/messages")
    assert messages_response.status_code == 200

    data = messages_response.json()
    assert len(data["messages"]) >= 2  # At least the pre-populated executions


def test_e2e_session_continuity(client, mock_factory, mock_agent_service, sample_session):
    """Test that session_id maintains conversation context."""
    from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent

    session_id = "sess-123"

    # First chat
    async def mock_execute_1(*args, **kwargs):
        yield ExecutionEvent.started(execution_id="exec-1", metadata={"agent_type": "investigator"})
        yield ExecutionEvent.response(content="First response.")
        yield ExecutionEvent.completed(execution_id="exec-1", total_tokens=100, duration_ms=200)

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute_1()

    response1 = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": session_id,
            "message": "First message",
            "stream": False,
        },
    )
    assert response1.status_code == 200
    assert response1.json()["session_id"] == session_id

    # Second chat with same session
    async def mock_execute_2(*args, **kwargs):
        yield ExecutionEvent.started(execution_id="exec-2", metadata={"agent_type": "investigator"})
        yield ExecutionEvent.response(content="Second response.")
        yield ExecutionEvent.completed(execution_id="exec-2", total_tokens=100, duration_ms=200)

    mock_agent_service.execute_agent.side_effect = lambda *args, **kwargs: mock_execute_2()

    response2 = client.post(
        "/api/v1/agent/chat",
        json={
            "case_id": "CASE-2026010100001",
            "session_id": session_id,
            "message": "Second message",
            "stream": False,
        },
    )
    assert response2.status_code == 200
    assert response2.json()["session_id"] == session_id


def test_e2e_multi_turn_conversation(client, mock_factory, sample_executions):
    """Test message retrieval preserves conversation order across turns."""
    response = client.get("/api/v1/sessions/sess-123/messages")

    assert response.status_code == 200
    data = response.json()
    messages = data["messages"]

    # Verify alternating user/assistant pattern
    for i in range(0, len(messages), 2):
        if i < len(messages):
            assert messages[i]["role"] == "user"
        if i + 1 < len(messages):
            assert messages[i + 1]["role"] == "assistant"
