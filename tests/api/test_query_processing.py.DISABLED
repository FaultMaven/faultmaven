from unittest.mock import AsyncMock, MagicMock, Mock, patch
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.models import QueryRequest, AgentResponse, ViewState, UploadedData, Source, SourceType, ResponseType
from faultmaven.api.v1.routes.agent import router
from faultmaven.api.v1.dependencies import get_agent_service

app = FastAPI()
app.include_router(router, prefix="/api/v1")


@pytest.fixture
def mock_agent_service():
    """Fixture to mock the AgentService dependency."""
    mock = MagicMock()
    mock.process_query = AsyncMock()
    mock.get_investigation_results = AsyncMock()
    mock.list_session_investigations = AsyncMock()
    mock.health_check = AsyncMock()
    app.dependency_overrides[get_agent_service] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_agent_service, None)


@pytest.fixture
def sample_agent_response():
    """Sample v3.1.0 agent response for tests."""
    view_state = ViewState(
        session_id="test_session",
        case_id="test_investigation_id",
        running_summary="Test investigation completed",
        uploaded_data=[
            UploadedData(id="data1", name="test.log", type="log_file")
        ]
    )
    
    sources = [
        Source(
            type=SourceType.KNOWLEDGE_BASE,
            name="troubleshooting_guide.md",
            snippet="Sample troubleshooting guidance"
        )
    ]
    
    return AgentResponse(
        content="Sample root cause identified. Analysis shows sample finding and sample warning.",
        response_type=ResponseType.ANSWER,
        view_state=view_state,
        sources=sources
    )


def test_process_query_session_not_found(mock_agent_service):
    """
    Test processing a query for a session that does not exist.
    """
    # Configure mock to raise FileNotFoundError for session not found
    mock_agent_service.process_query.side_effect = FileNotFoundError("Session not found")
    query = QueryRequest(session_id="non_existent_session", query="test query")

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query.model_dump())

    assert response.status_code == 404
    assert "Resource not found" in response.json()["detail"]


def test_process_query_agent_fails(mock_agent_service):
    """
    Test processing a query when the agent investigation fails.
    """
    # Configure mock to raise RuntimeError for agent processing failure
    mock_agent_service.process_query.side_effect = RuntimeError("Agent processing failed")
    query = QueryRequest(session_id="active_session", query="test query")

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query.model_dump())

    assert response.status_code == 500
    assert "Service error during query processing" in response.json()["detail"]


def test_process_query_missing_session_id():
    """
    Test that missing session_id returns 422 validation error.
    """
    invalid_request = {"query": "test query"}  # Missing session_id field

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=invalid_request)

    assert response.status_code == 422
    error_detail = response.json()["detail"]
    assert any("session_id" in str(error) for error in error_detail)


def test_process_query_missing_query():
    """
    Test that missing query field returns 422 validation error.
    """
    invalid_request = {
        "session_id": "test_session"
        # Missing query field
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=invalid_request)

    assert response.status_code == 422
    error_detail = response.json()["detail"]
    assert any("query" in str(error) for error in error_detail)


def test_process_query_empty_query(mock_agent_service):
    """
    Test that empty query string returns 400 validation error.
    """
    # Configure mock to raise ValueError for empty query validation
    mock_agent_service.process_query.side_effect = ValueError("Query cannot be empty")
    
    invalid_request = {"session_id": "test_session", "query": ""}  # Empty query

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=invalid_request)

    # Should fail at query validation
    assert response.status_code == 400
    assert "Query cannot be empty" in response.json()["detail"]


def test_process_query_invalid_priority(mock_agent_service, sample_agent_response):
    """
    Test that invalid priority value is processed successfully (no strict validation).
    """
    # Configure mock to return successful response - priority validation is lenient
    mock_agent_service.process_query.return_value = sample_agent_response
    
    invalid_request = {
        "session_id": "test_session",
        "query": "test query",
        "priority": "invalid_priority",  # Invalid priority
    }
    
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=invalid_request)
    # Should process successfully since priority is not strictly validated
    assert response.status_code == 200
    assert response.json()["view_state"]["session_id"] == "test_session"


def test_process_query_invalid_context_type():
    """
    Test that invalid context type returns 422 validation error.
    """
    invalid_request = {
        "session_id": "test_session",
        "query": "test query",
        "context": "not_a_dict",  # Context should be dict, not string
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=invalid_request)

    assert response.status_code == 422
    error_detail = response.json()["detail"]
    assert any("context" in str(error) for error in error_detail)


def test_process_query_malformed_json():
    """
    Test that malformed JSON returns 422 validation error.
    """
    malformed_json = (
        '{"session_id": "test_session", "query": "test query", "priority":}'
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent/query",
            json=malformed_json,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422


def test_process_query_extra_fields(mock_agent_service, sample_agent_response):
    """
    Test that extra fields in request are handled gracefully.
    """
    # Configure mock to return successful response - extra fields should be ignored
    mock_agent_service.process_query.return_value = sample_agent_response
    
    request_with_extra = {
        "session_id": "test_session",
        "query": "test query",
        "extra_field": "should_be_ignored",
    }

    # Extra fields should be ignored by Pydantic, processing should succeed
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=request_with_extra)

    assert response.status_code == 200
    assert response.json()["view_state"]["session_id"] == "test_session"


def test_process_query_session_manager_exception(mock_agent_service):
    """
    Test that service layer exceptions are handled properly.
    """
    # Configure mock to raise generic Exception for service failure
    mock_agent_service.process_query.side_effect = Exception(
        "Database connection failed"
    )
    query = QueryRequest(session_id="test_session", query="test query")

    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query.model_dump())

    assert response.status_code == 500
    assert "Internal server error during query processing" in response.json()["detail"]


def test_process_query_data_sanitizer_exception(mock_agent_service):
    """
    Test that data sanitizer exceptions are handled properly.
    """
    # Configure mock to raise Exception for sanitization failure
    mock_agent_service.process_query.side_effect = RuntimeError(
        "Sanitization failed"
    )
    query = QueryRequest(session_id="test_session", query="test query")
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query.model_dump())
    
    assert response.status_code == 500
    assert "Service error during query processing" in response.json()["detail"]


def test_process_query_agent_not_initialized(mock_agent_service):
    """
    Test handling when core agent is not initialized.
    """
    # Configure mock to raise RuntimeError for agent not initialized
    mock_agent_service.process_query.side_effect = RuntimeError(
        "Agent not initialized"
    )
    
    query = QueryRequest(session_id="test_session", query="test query")
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query.model_dump())
    
    # Should return 500 error when agent is not initialized
    assert response.status_code == 500
    assert "Service error during query processing" in response.json()["detail"]


def test_process_query_with_context(mock_agent_service):
    """
    Test processing query with context data.
    """
    # Create a comprehensive response for context testing
    view_state = ViewState(
        session_id="test_session",
        case_id="context_test_investigation",
        running_summary="Test investigation with context completed",
        uploaded_data=[]
    )
    
    sources = [
        Source(
            type=SourceType.KNOWLEDGE_BASE,
            name="context_guide.md",
            snippet="Context-specific troubleshooting guidance"
        )
    ]
    
    expected_response = AgentResponse(
        content="Test root cause with context identified. Found test finding with context and context-aware analysis.",
        response_type=ResponseType.ANSWER,
        view_state=view_state,
        sources=sources
    )
    
    # Configure mock to return successful response with context
    mock_agent_service.process_query.return_value = expected_response
    
    query_with_context = {
        "session_id": "test_session",
        "query": "test query",
        "context": {
            "service_name": "test_service",
            "error_code": "ERR001",
            "environment": "production",
        },
        "priority": "high",
    }
    
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/query", json=query_with_context)
    
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["schema_version"] == "3.1.0"
    assert response_data["view_state"]["session_id"] == "test_session"
    assert response_data["view_state"]["case_id"] == "context_test_investigation"
    assert response_data["response_type"] == "answer"
    assert "context" in response_data["content"]
    assert len(response_data["sources"]) == 1
