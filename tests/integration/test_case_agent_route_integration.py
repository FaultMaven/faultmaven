"""Integration tests for case route integration with AgentService.

This module tests the critical integration between case routes and AgentService,
ensuring the submit_case_query endpoint properly calls real AI processing.
"""

import pytest
import json
import uuid
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi import FastAPI

from faultmaven.api.v1.routes.case import router as case_router
from faultmaven.services.agent import AgentService
from faultmaven.models import QueryRequest, AgentResponse, ResponseType, ViewState, Source, SourceType
from faultmaven.models.case import Case, CaseStatus, CasePriority, MessageType
from faultmaven.models.api import User, Case as APICase
from faultmaven.exceptions import ValidationException, ServiceException


@pytest.fixture
def app():
    """Create FastAPI app with case router for testing."""
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1/cases")
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_case_service():
    """Mock case service that provides required functionality."""
    service = AsyncMock()
    
    # Mock case retrieval
    sample_case = Mock()
    sample_case.case_id = "test-case-123"
    sample_case.title = "Test Case"
    sample_case.status = CaseStatus.ACTIVE
    sample_case.priority = CasePriority.MEDIUM
    sample_case.owner_id = "test-user"
    sample_case.message_count = 5
    
    service.get_case = AsyncMock(return_value=sample_case)
    service.add_case_query = AsyncMock()
    service.add_assistant_response = AsyncMock()
    service.check_idempotency_key = AsyncMock(return_value=None)
    service.store_idempotency_result = AsyncMock()
    
    return service


@pytest.fixture
def mock_agent_service():
    """Mock agent service with realistic behavior."""
    service = AsyncMock()
    
    # Create realistic AgentResponse
    view_state = ViewState(
        session_id="session_test-case-123",
        user=User(
            user_id="test-user",
            email="user@example.com",
            name="Test User",
            created_at="2025-08-30T00:00:00Z"
        ),
        active_case=APICase(
            case_id="test-case-123",
            title="Test Case",
            status="active",
            created_at="2025-08-30T00:00:00Z",
            updated_at="2025-08-30T00:00:00Z",
            session_id="session_test-case-123"
        ),
        cases=[],
        messages=[],
        uploaded_data=[],
        show_case_selector=False,
        show_data_upload=True,
        loading_state=None
    )
    
    agent_response = AgentResponse(
        content="Based on your query, I can help troubleshoot the login issue. Let me analyze the authentication flow.",
        response_type=ResponseType.ANSWER,
        view_state=view_state,
        sources=[
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                content="Authentication troubleshooting guide: Common login failures are often...",
                confidence=0.85,
                metadata={"source": "auth_troubleshooting.md"}
            )
        ],
        plan=None
    )
    
    service.process_query_for_case = AsyncMock(return_value=agent_response)
    
    return service


class TestCaseQueryRouteIntegration:
    """Integration tests for case query route with AgentService."""
    
    @pytest.mark.integration
    async def test_submit_case_query_calls_agent_service(self, client, mock_case_service, mock_agent_service):
        """Test that submit_case_query properly calls AgentService.process_query_for_case."""
        case_id = "test-case-123"
        query_data = {
            "query": "My users are experiencing login failures. What could be causing this?"
        }
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    # Verify HTTP response
                    assert response.status_code == 201
                    
                    # Verify AgentService was called with correct parameters
                    mock_agent_service.process_query_for_case.assert_called_once()
                    call_args = mock_agent_service.process_query_for_case.call_args
                    
                    assert call_args[0][0] == case_id  # case_id parameter
                    query_request = call_args[0][1]  # QueryRequest parameter
                    assert isinstance(query_request, QueryRequest)
                    assert query_request.query == query_data["query"]
                    assert query_request.session_id == f"session_{case_id}"
                    assert query_request.context["case_id"] == case_id
                    assert query_request.context["user_id"] == "test-user"
    
    @pytest.mark.integration
    async def test_submit_case_query_returns_agent_response_format(self, client, mock_case_service, mock_agent_service):
        """Test that the response follows the correct AgentResponse format."""
        case_id = "test-case-456"
        query_data = {
            "query": "How do I debug memory leaks in my application?"
        }
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    assert response.status_code == 201
                    
                    # Verify response structure matches AgentResponse schema
                    response_data = response.json()
                    assert response_data["schema_version"] == "3.1.0"
                    assert "content" in response_data
                    assert "response_type" in response_data
                    assert "view_state" in response_data
                    assert "sources" in response_data
                    
                    # Verify content comes from AgentService
                    assert "troubleshoot the login issue" in response_data["content"]
                    
                    # Verify view_state structure
                    view_state = response_data["view_state"]
                    assert "session_id" in view_state
                    assert "user" in view_state
                    assert "active_case" in view_state
                    assert view_state["active_case"]["case_id"] == case_id
                    
                    # Verify sources are included
                    assert len(response_data["sources"]) == 1
                    source = response_data["sources"][0]
                    assert source["type"] == "KNOWLEDGE_BASE"
                    assert "Authentication troubleshooting" in source["content"]
    
    @pytest.mark.integration
    async def test_submit_case_query_with_complex_query_async_processing(self, client, mock_case_service, mock_agent_service):
        """Test async processing for complex queries."""
        case_id = "test-case-complex"
        
        # Complex query that should trigger async processing
        complex_query_data = {
            "query": "analyze logs" + " complex investigation" + " " + "x" * 1500  # Over 1000 chars
        }
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=complex_query_data)
                    
                    # Should return 202 for async processing
                    assert response.status_code == 202
                    
                    response_data = response.json()
                    assert response_data["status"] == "processing"
                    assert "job_id" in response_data
                    assert response_data["case_id"] == case_id
                    assert response_data["query"] == complex_query_data["query"]
                    
                    # Verify Location header is set
                    assert "Location" in response.headers
                    assert f"/api/v1/cases/{case_id}/queries/" in response.headers["Location"]
                    assert "Retry-After" in response.headers
    
    @pytest.mark.integration
    async def test_submit_case_query_agent_service_failure_graceful_degradation(self, client, mock_case_service):
        """Test graceful degradation when AgentService fails."""
        case_id = "test-case-failure"
        query_data = {
            "query": "What's causing the system to crash?"
        }
        
        # Create failing agent service
        failing_agent_service = AsyncMock()
        failing_agent_service.process_query_for_case.side_effect = Exception("LLM service unavailable")
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = failing_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    # Should still return 201 with graceful fallback
                    assert response.status_code == 201
                    
                    response_data = response.json()
                    assert response_data["schema_version"] == "3.1.0"
                    assert "unable to fully process" in response_data["content"]
                    assert "technical issue" in response_data["content"]
                    assert query_data["query"] in response_data["content"]  # Query should be mentioned
    
    @pytest.mark.integration
    async def test_submit_case_query_case_not_found_error(self, client, mock_agent_service):
        """Test error handling when case is not found."""
        case_id = "non-existent-case"
        query_data = {
            "query": "Test query for non-existent case"
        }
        
        # Mock case service that returns None for get_case
        case_service_not_found = AsyncMock()
        case_service_not_found.get_case = AsyncMock(return_value=None)
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = case_service_not_found
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    # Should return 404 for non-existent case
                    assert response.status_code == 404
                    assert "Case not found" in response.json()["detail"]
    
    @pytest.mark.integration
    async def test_submit_case_query_invalid_request_validation(self, client, mock_case_service, mock_agent_service):
        """Test request validation for invalid inputs."""
        case_id = "test-case-123"
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    # Test empty query
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json={"query": ""})
                    assert response.status_code == 400
                    assert "Query text is required" in response.json()["detail"]
                    
                    # Test missing query field
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json={})
                    assert response.status_code == 400
                    assert "Query text is required" in response.json()["detail"]
                    
                    # Test invalid case_id
                    response = client.post("/api/v1/cases//queries", json={"query": "test"})
                    assert response.status_code == 404  # FastAPI routing will handle this
    
    @pytest.mark.integration
    async def test_submit_case_query_idempotency_key_handling(self, client, mock_case_service, mock_agent_service):
        """Test idempotency key handling for duplicate requests."""
        case_id = "test-case-idempotent"
        query_data = {
            "query": "This is an idempotent test query"
        }
        idempotency_key = "test-key-12345"
        
        # Mock existing result for idempotency key
        existing_result = {
            "status_code": 201,
            "content": {"content": "Previous response", "response_type": "ANSWER"},
            "headers": {"Location": f"/api/v1/cases/{case_id}/queries/previous_query"}
        }
        mock_case_service.check_idempotency_key = AsyncMock(return_value=existing_result)
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(
                        f"/api/v1/cases/{case_id}/queries", 
                        json=query_data,
                        headers={"idempotency-key": idempotency_key}
                    )
                    
                    # Should return the cached result
                    assert response.status_code == 201
                    response_data = response.json()
                    assert response_data["content"] == "Previous response"
                    
                    # AgentService should NOT be called
                    mock_agent_service.process_query_for_case.assert_not_called()
                    
                    # Idempotency check should be called
                    mock_case_service.check_idempotency_key.assert_called_once_with(idempotency_key)
    
    @pytest.mark.integration
    async def test_submit_case_query_updates_case_metadata(self, client, mock_case_service, mock_agent_service):
        """Test that case metadata is properly updated during query processing."""
        case_id = "test-case-metadata"
        query_data = {
            "query": "Update case metadata test query"
        }
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    assert response.status_code == 201
                    
                    # Verify case query count was updated
                    mock_case_service.add_case_query.assert_called_once_with(
                        case_id, 
                        query_data["query"], 
                        "test-user"
                    )
                    
                    # Verify assistant response was recorded
                    mock_case_service.add_assistant_response.assert_called_once()
                    assistant_call = mock_case_service.add_assistant_response.call_args
                    assert assistant_call[0][0] == case_id  # case_id
                    assert "troubleshoot the login issue" in assistant_call[0][1]  # content
                    assert assistant_call[0][2] == "ANSWER"  # response_type
                    assert assistant_call[0][3] == "test-user"  # user_id
    
    @pytest.mark.integration
    async def test_submit_case_query_location_header_format(self, client, mock_case_service, mock_agent_service):
        """Test that Location header is properly formatted for sync responses."""
        case_id = "test-case-location"
        query_data = {
            "query": "Test query for location header validation"
        }
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_agent_service_dependency') as mock_agent_dep:
                with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                    
                    mock_case_dep.return_value = mock_case_service
                    mock_agent_dep.return_value = mock_agent_service
                    mock_user_dep.return_value = "test-user"
                    
                    response = client.post(f"/api/v1/cases/{case_id}/queries", json=query_data)
                    
                    assert response.status_code == 201
                    
                    # Verify Location header format
                    assert "Location" in response.headers
                    location = response.headers["Location"]
                    assert location.startswith(f"/api/v1/cases/{case_id}/queries/")
                    assert "query_" in location  # Should contain query ID


@pytest.mark.integration 
class TestCaseQueryPollingEndpoints:
    """Test the query polling endpoints for async query processing."""
    
    @pytest.mark.integration
    async def test_get_case_query_processing_status(self, client, mock_case_service):
        """Test getting query processing status."""
        case_id = "test-case-status"
        query_id = "processing_job_12345"
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                
                mock_case_dep.return_value = mock_case_service
                mock_user_dep.return_value = "test-user"
                
                response = client.get(f"/api/v1/cases/{case_id}/queries/{query_id}")
                
                assert response.status_code == 200
                response_data = response.json()
                assert response_data["job_id"] == query_id
                assert response_data["case_id"] == case_id
                assert response_data["status"] == "processing"
                assert "Retry-After" in response.headers
    
    @pytest.mark.integration
    async def test_get_case_query_completed_redirect(self, client, mock_case_service):
        """Test redirect when query processing is completed."""
        case_id = "test-case-completed"
        query_id = "done_job_67890"  # 'done_' prefix triggers completion logic
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                
                mock_case_dep.return_value = mock_case_service
                mock_user_dep.return_value = "test-user"
                
                response = client.get(f"/api/v1/cases/{case_id}/queries/{query_id}")
                
                assert response.status_code == 303  # See Other redirect
                assert "Location" in response.headers
                assert response.headers["Location"] == f"/api/v1/cases/{case_id}/queries/{query_id}/result"
    
    @pytest.mark.integration
    async def test_get_case_query_result_final_response(self, client, mock_case_service):
        """Test getting final query result."""
        case_id = "test-case-result"
        query_id = "completed_query_99999"
        
        # Mock case with proper attributes for result generation
        mock_case = Mock()
        mock_case.case_id = case_id
        mock_case.title = "Test Case"
        mock_case.status = CaseStatus.ACTIVE
        mock_case.priority = CasePriority.MEDIUM
        mock_case.message_count = 3
        mock_case_service.get_case = AsyncMock(return_value=mock_case)
        
        with patch('faultmaven.api.v1.routes.case._di_get_case_service_dependency') as mock_case_dep:
            with patch('faultmaven.api.v1.routes.case._di_get_user_id_dependency') as mock_user_dep:
                
                mock_case_dep.return_value = mock_case_service
                mock_user_dep.return_value = "test-user"
                
                response = client.get(f"/api/v1/cases/{case_id}/queries/{query_id}/result")
                
                assert response.status_code == 200
                response_data = response.json()
                
                # Verify AgentResponse structure
                assert response_data["schema_version"] == "3.1.0"
                assert "content" in response_data
                assert "response_type" in response_data
                assert "view_state" in response_data
                assert response_data["view_state"]["active_case"]["case_id"] == case_id