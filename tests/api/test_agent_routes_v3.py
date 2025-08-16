"""Test module for v3.1.0 API routes implementation."""

import pytest
import json
from datetime import datetime
from unittest.mock import Mock, AsyncMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from faultmaven.api.v1.routes.agent import router
from faultmaven.api.v1.dependencies import get_agent_service
from faultmaven.models.api import (
    QueryRequest,
    AgentResponse,
    ErrorResponse,
    ResponseType,
    SourceType,
    Source,
    PlanStep,
    ViewState,
    UploadedData,
    ErrorDetail
)
from faultmaven.models import TroubleshootingResponse
from faultmaven.services.agent_service import AgentService
from faultmaven.exceptions import ValidationException


# Create test app
test_app = FastAPI()
test_app.include_router(router)


class TestQueryEndpointV3:
    """Test the new /query endpoint with v3.1.0 schema."""
    
    @pytest.fixture
    def mock_agent_service(self):
        """Mock AgentService for testing."""
        service = Mock(spec=AgentService)
        return service
    
    @pytest.fixture
    def client_with_mocked_service(self, mock_agent_service):
        """TestClient with mocked AgentService dependency."""
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    @pytest.fixture
    def sample_agent_response(self):
        """Sample AgentResponse for testing."""
        view_state = ViewState(
            session_id="test-session-123",
            case_id="case-456",
            running_summary="Test investigation in progress",
            uploaded_data=[
                UploadedData(id="data1", name="test.log", type="log_file")
            ]
        )
        
        sources = [
            Source(
                type=SourceType.KNOWLEDGE_BASE,
                name="troubleshooting_guide.md",
                snippet="Database connection troubleshooting steps..."
            )
        ]
        
        return AgentResponse(
            content="The database connection issue is caused by timeout settings.",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=sources
        )
    
    @pytest.fixture
    def sample_plan_response(self):
        """Sample AgentResponse with plan for testing."""
        view_state = ViewState(
            session_id="test-session-123",
            case_id="case-789",
            running_summary="Multi-step plan created",
            uploaded_data=[]
        )
        
        plan = [
            PlanStep(description="Step 1: Check database connectivity"),
            PlanStep(description="Step 2: Review connection pool settings"),
            PlanStep(description="Step 3: Restart database service")
        ]
        
        return AgentResponse(
            content="Here's a plan to resolve the database issues:",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=[],
            plan=plan
        )
    
    def test_query_endpoint_success_answer(self, client_with_mocked_service, sample_agent_response):
        """Test /query endpoint returns successful AgentResponse for ANSWER type."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(return_value=sample_agent_response)
        
        request_data = {
            "session_id": "test-session-123",
            "query": "What's causing the database errors?"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Parse response JSON
        response_data = response.json()
        
        # Verify AgentResponse structure
        assert response_data["schema_version"] == "3.1.0"
        assert response_data["content"] == "The database connection issue is caused by timeout settings."
        assert response_data["response_type"] == "answer"
        assert response_data["view_state"]["session_id"] == "test-session-123"
        assert response_data["view_state"]["case_id"] == "case-456"
        assert len(response_data["sources"]) == 1
        assert response_data["plan"] is None
        
        # Verify service was called correctly
        mock_agent_service.process_query.assert_called_once()
        call_args = mock_agent_service.process_query.call_args[0][0]
        assert call_args.session_id == "test-session-123"
        assert call_args.query == "What's causing the database errors?"
    
    def test_query_endpoint_success_plan_proposal(self, client_with_mocked_service, sample_plan_response):
        """Test /query endpoint returns successful AgentResponse for PLAN_PROPOSAL type."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(return_value=sample_plan_response)
        
        request_data = {
            "session_id": "test-session-123", 
            "query": "How do I fix the database connection issues?"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify response
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Verify plan proposal structure
        assert response_data["response_type"] == "plan_proposal"
        assert response_data["plan"] is not None
        assert len(response_data["plan"]) == 3
        assert response_data["plan"][0]["description"] == "Step 1: Check database connectivity"
        assert response_data["plan"][2]["description"] == "Step 3: Restart database service"
    
    def test_query_endpoint_validation_error_422(self, client_with_mocked_service):
        """Test /query endpoint returns 422 for validation errors."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=ValidationException("Query cannot be empty"))
        
        request_data = {
            "session_id": "test-session-123",
            "query": ""  # Empty query should cause validation error
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 422
        response_data = response.json()
        assert "Query cannot be empty" in response_data["detail"]
    
    def test_query_endpoint_value_error_400(self, client_with_mocked_service):
        """Test /query endpoint returns 400 for business logic validation errors."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=ValueError("Invalid session format"))
        
        request_data = {
            "session_id": "invalid-session",
            "query": "test query"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 400
        response_data = response.json()
        assert "Invalid session format" in response_data["detail"]
    
    def test_query_endpoint_not_found_404(self, client_with_mocked_service):
        """Test /query endpoint returns 404 for resource not found errors."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=FileNotFoundError("Session not found"))
        
        request_data = {
            "session_id": "nonexistent-session",
            "query": "test query"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 404
        response_data = response.json()
        assert "Resource not found" in response_data["detail"]
    
    def test_query_endpoint_permission_error_403(self, client_with_mocked_service):
        """Test /query endpoint returns 403 for permission errors."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=PermissionError("Access denied"))
        
        request_data = {
            "session_id": "unauthorized-session",
            "query": "test query"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 403
        response_data = response.json()
        assert "Access denied" in response_data["detail"]
    
    def test_query_endpoint_runtime_error_500(self, client_with_mocked_service):
        """Test /query endpoint returns 500 for unexpected runtime errors."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=RuntimeError("Unexpected service error"))
        
        request_data = {
            "session_id": "test-session-123",
            "query": "test query"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 500
        response_data = response.json()
        assert "Service error during query processing" in response_data["detail"]
    
    def test_query_endpoint_wrapped_validation_error_422(self, client_with_mocked_service):
        """Test /query endpoint handles wrapped validation errors from service layer."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(
            side_effect=RuntimeError("Validation failed: Query is too long")
        )
        
        request_data = {
            "session_id": "test-session-123",
            "query": "x" * 10000  # Very long query
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Should be treated as validation error
        assert response.status_code == 422
        response_data = response.json()
        assert "Validation failed" in response_data["detail"]
    
    def test_query_endpoint_generic_exception_500(self, client_with_mocked_service):
        """Test /query endpoint returns 500 for unexpected exceptions."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=Exception("Unexpected error"))
        
        request_data = {
            "session_id": "test-session-123",
            "query": "test query"
        }
        
        response = client.post("/agent/query", json=request_data)
        
        # Verify error response
        assert response.status_code == 500
        response_data = response.json()
        assert "Internal server error" in response_data["detail"]
    
    def test_query_endpoint_invalid_json_422(self):
        """Test /query endpoint returns 422 for invalid JSON."""
        client = TestClient(test_app)
        
        # Send invalid JSON
        response = client.post(
            "/agent/query",
            data="invalid json",
            headers={"content-type": "application/json"}
        )
        
        assert response.status_code == 422
    
    def test_query_endpoint_missing_fields_422(self):
        """Test /query endpoint returns 422 for missing required fields."""
        client = TestClient(test_app)
        
        # Missing session_id
        response = client.post("/agent/query", json={"query": "test query"})
        assert response.status_code == 422
        
        # Missing query
        response = client.post("/agent/query", json={"session_id": "test-session"})
        assert response.status_code == 422
        
        # Empty JSON
        response = client.post("/agent/query", json={})
        assert response.status_code == 422


class TestLegacyTroubleshootEndpoint:
    """Test backward compatibility with legacy /troubleshoot endpoint."""
    
    @pytest.fixture
    def mock_agent_service(self):
        """Mock AgentService for testing."""
        service = Mock(spec=AgentService)
        return service
    
    @pytest.fixture
    def client_with_mocked_service(self, mock_agent_service):
        """TestClient with mocked AgentService dependency."""
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    @pytest.fixture
    def sample_agent_response_for_legacy(self):
        """Sample AgentResponse that will be converted to legacy format."""
        view_state = ViewState(
            session_id="legacy-session-123",
            case_id="legacy-case-456",
            running_summary="Legacy test",
            uploaded_data=[]
        )
        
        return AgentResponse(
            content="Root Cause: Database connection timeout\n\nKey Findings:\n• High connection count\n• Network latency detected\n\nRecommendations:\n• Increase timeout settings\n• Optimize queries",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
    
    @pytest.fixture 
    def sample_plan_response_for_legacy(self):
        """Sample AgentResponse with plan for legacy conversion."""
        view_state = ViewState(
            session_id="legacy-session-123",
            case_id="legacy-case-789",
            running_summary="Legacy plan test",
            uploaded_data=[]
        )
        
        plan = [
            PlanStep(description="Check database status"),
            PlanStep(description="Restart database service"),
            PlanStep(description="Monitor performance")
        ]
        
        return AgentResponse(
            content="Multi-step resolution plan",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=[],
            plan=plan
        )
    
    def test_troubleshoot_legacy_endpoint_success(self, client_with_mocked_service, sample_agent_response_for_legacy):
        """Test legacy /troubleshoot endpoint returns TroubleshootingResponse."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(return_value=sample_agent_response_for_legacy)
        
        request_data = {
            "session_id": "legacy-session-123",
            "query": "Legacy troubleshooting query"
        }
        
        response = client.post("/agent/troubleshoot", json=request_data)
        
        # Verify response
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Verify TroubleshootingResponse structure (legacy format)
        assert "investigation_id" in response_data
        assert response_data["investigation_id"] == "legacy-case-456"
        assert response_data["session_id"] == "legacy-session-123"
        assert response_data["status"] == "completed"
        assert "findings" in response_data
        assert "recommendations" in response_data
        assert "root_cause" in response_data
        assert "confidence_score" in response_data
        assert "estimated_mttr" in response_data
        assert "next_steps" in response_data
        
        # Verify content parsing
        assert len(response_data["findings"]) > 0
        assert len(response_data["recommendations"]) > 0
        assert "Database connection timeout" in response_data["root_cause"]
    
    def test_troubleshoot_legacy_with_plan_conversion(self, client_with_mocked_service, sample_plan_response_for_legacy):
        """Test legacy endpoint converts plan to next_steps."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(return_value=sample_plan_response_for_legacy)
        
        request_data = {
            "session_id": "legacy-session-123", 
            "query": "Legacy plan query"
        }
        
        response = client.post("/agent/troubleshoot", json=request_data)
        
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Verify plan was converted to next_steps
        assert len(response_data["next_steps"]) == 3
        assert "Check database status" in response_data["next_steps"]
        assert "Restart database service" in response_data["next_steps"]
        assert "Monitor performance" in response_data["next_steps"]
    
    def test_troubleshoot_legacy_content_parsing(self, client_with_mocked_service):
        """Test legacy endpoint parses different content formats correctly."""
        client, mock_agent_service = client_with_mocked_service
        
        # Test with unstructured content
        view_state = ViewState(
            session_id="test-session",
            case_id="test-case",
            running_summary="Test",
            uploaded_data=[]
        )
        
        unstructured_response = AgentResponse(
            content="This is just plain text without structure",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        mock_agent_service.process_query = AsyncMock(return_value=unstructured_response)
        
        request_data = {
            "session_id": "test-session",
            "query": "Unstructured query"
        }
        
        response = client.post("/agent/troubleshoot", json=request_data)
        
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Should create default structure
        assert len(response_data["findings"]) == 1
        assert "This is just plain text" in response_data["findings"][0]["message"]
        assert response_data["findings"][0]["type"] == "general"
        assert response_data["recommendations"] == ["Review the analysis above"]
    
    def test_troubleshoot_legacy_error_handling(self, client_with_mocked_service):
        """Test legacy endpoint error handling matches new endpoint."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.process_query = AsyncMock(side_effect=ValidationException("Legacy validation error"))
        
        request_data = {
            "session_id": "test-session",
            "query": "test query"
        }
        
        response = client.post("/agent/troubleshoot", json=request_data)
        
        # Should have same error handling as new endpoint
        assert response.status_code == 422
        response_data = response.json()
        assert "Legacy validation error" in response_data["detail"]


class TestLegacyConversionFunction:
    """Test the _convert_to_legacy_response function."""
    
    def test_convert_structured_content(self):
        """Test conversion of structured AgentResponse content."""
        from faultmaven.api.v1.routes.agent import _convert_to_legacy_response
        
        view_state = ViewState(
            session_id="test-session",
            case_id="test-case-123",
            running_summary="Test conversion",
            uploaded_data=[]
        )
        
        agent_response = AgentResponse(
            content="Root Cause: Network timeout\n\nKey Findings:\n• Connection errors\n• High latency\n\nRecommendations:\n• Check network\n• Restart service",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        legacy_response = _convert_to_legacy_response(agent_response, "test-session")
        
        # Verify conversion
        assert legacy_response.investigation_id == "test-case-123"
        assert legacy_response.session_id == "test-session"
        assert legacy_response.status == "completed"
        assert legacy_response.root_cause == "Network timeout"
        
        # Verify findings conversion
        assert len(legacy_response.findings) == 2
        assert "Connection errors" in legacy_response.findings[0]["message"]
        assert "High latency" in legacy_response.findings[1]["message"]
        
        # Verify recommendations conversion
        assert len(legacy_response.recommendations) == 2
        assert "Check network" in legacy_response.recommendations
        assert "Restart service" in legacy_response.recommendations
    
    def test_convert_plan_to_next_steps(self):
        """Test conversion of plan to next_steps in legacy format."""
        from faultmaven.api.v1.routes.agent import _convert_to_legacy_response
        
        view_state = ViewState(
            session_id="test-session",
            case_id="test-case-456",
            running_summary="Plan conversion test",
            uploaded_data=[]
        )
        
        plan = [
            PlanStep(description="First step"),
            PlanStep(description="Second step"),
            PlanStep(description="Third step")
        ]
        
        agent_response = AgentResponse(
            content="Plan content",
            response_type=ResponseType.PLAN_PROPOSAL,
            view_state=view_state,
            sources=[],
            plan=plan
        )
        
        legacy_response = _convert_to_legacy_response(agent_response, "test-session")
        
        # Verify plan conversion to next_steps
        assert len(legacy_response.next_steps) == 3
        assert "First step" in legacy_response.next_steps
        assert "Second step" in legacy_response.next_steps
        assert "Third step" in legacy_response.next_steps
    
    def test_convert_unstructured_content(self):
        """Test conversion of unstructured content."""
        from faultmaven.api.v1.routes.agent import _convert_to_legacy_response
        
        view_state = ViewState(
            session_id="test-session",
            case_id="test-case-789",
            running_summary="Unstructured test",
            uploaded_data=[]
        )
        
        agent_response = AgentResponse(
            content="This is just plain unstructured text without sections",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        legacy_response = _convert_to_legacy_response(agent_response, "test-session")
        
        # Should create default structure
        assert len(legacy_response.findings) == 1
        assert "plain unstructured text" in legacy_response.findings[0]["message"]
        assert legacy_response.findings[0]["type"] == "general"
        assert legacy_response.recommendations == ["Review the analysis above"]
        assert legacy_response.root_cause == "Analysis completed"


class TestGetInvestigationEndpoint:
    """Test the /investigations/{investigation_id} endpoint."""
    
    @pytest.fixture
    def mock_agent_service(self):
        """Mock AgentService for testing."""
        service = Mock(spec=AgentService)
        return service
    
    @pytest.fixture
    def client_with_mocked_service(self, mock_agent_service):
        """TestClient with mocked AgentService dependency."""
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    @pytest.fixture
    def sample_troubleshooting_response(self):
        """Sample TroubleshootingResponse for testing."""
        return TroubleshootingResponse(
            investigation_id="inv-123",
            session_id="session-456",
            status="completed",
            findings=[
                {
                    "type": "error",
                    "message": "Database connection failed",
                    "severity": "high",
                    "timestamp": "2024-01-01T12:00:00Z",
                    "source": "log_analysis"
                }
            ],
            root_cause="Connection pool exhausted",
            recommendations=["Increase pool size", "Optimize queries"],
            confidence_score=0.9,
            estimated_mttr="20 minutes",
            next_steps=["Monitor connections", "Review metrics"],
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow()
        )
    
    def test_get_investigation_success(self, client_with_mocked_service, sample_troubleshooting_response):
        """Test successful investigation retrieval."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.get_investigation_results = AsyncMock(return_value=sample_troubleshooting_response)
        
        response = client.get("/agent/investigations/inv-123?session_id=session-456")
        
        assert response.status_code == 200
        
        response_data = response.json()
        assert response_data["investigation_id"] == "inv-123"
        assert response_data["session_id"] == "session-456"
        assert response_data["status"] == "completed"
        assert len(response_data["findings"]) == 1
        assert len(response_data["recommendations"]) == 2
    
    def test_get_investigation_not_found(self, client_with_mocked_service):
        """Test investigation not found error."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.get_investigation_results = AsyncMock(
            side_effect=FileNotFoundError("Investigation not found")
        )
        
        response = client.get("/agent/investigations/nonexistent?session_id=session-123")
        
        assert response.status_code == 404
        response_data = response.json()
        assert "Investigation not found" in response_data["detail"]
    
    def test_get_investigation_validation_error(self, client_with_mocked_service):
        """Test investigation validation error."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.get_investigation_results = AsyncMock(
            side_effect=ValueError("Invalid investigation ID")
        )
        
        response = client.get("/agent/investigations/invalid?session_id=session-123")
        
        assert response.status_code == 400
        response_data = response.json()
        assert "Invalid investigation ID" in response_data["detail"]


class TestListSessionInvestigationsEndpoint:
    """Test the /sessions/{session_id}/investigations endpoint."""
    
    @pytest.fixture
    def mock_agent_service(self):
        """Mock AgentService for testing."""
        service = Mock(spec=AgentService)
        return service
    
    @pytest.fixture
    def client_with_mocked_service(self, mock_agent_service):
        """TestClient with mocked AgentService dependency."""
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    @pytest.fixture
    def sample_investigations_list(self):
        """Sample investigations list for testing."""
        return [
            {
                "investigation_id": "inv-1",
                "query": "Database errors query",
                "status": "completed",
                "priority": "high",
                "findings_count": 3,
                "recommendations_count": 2,
                "confidence_score": 0.85,
                "created_at": "2024-01-01T12:00:00Z",
                "completed_at": "2024-01-01T12:15:00Z",
                "estimated_mttr": "15 minutes"
            },
            {
                "investigation_id": "inv-2",
                "query": "Performance issues query",
                "status": "completed",
                "priority": "medium",
                "findings_count": 2,
                "recommendations_count": 3,
                "confidence_score": 0.75,
                "created_at": "2024-01-01T11:00:00Z",
                "completed_at": "2024-01-01T11:20:00Z",
                "estimated_mttr": "20 minutes"
            }
        ]
    
    def test_list_investigations_success(self, client_with_mocked_service, sample_investigations_list):
        """Test successful investigations listing."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.list_session_investigations = AsyncMock(return_value=sample_investigations_list)
        
        response = client.get("/agent/sessions/session-123/investigations")
        
        assert response.status_code == 200
        
        response_data = response.json()
        assert response_data["session_id"] == "session-123"
        assert len(response_data["investigations"]) == 2
        assert response_data["limit"] == 10  # Default limit
        assert response_data["offset"] == 0   # Default offset
        assert response_data["total"] == 2
        
        # Verify investigation data
        assert response_data["investigations"][0]["investigation_id"] == "inv-1"
        assert response_data["investigations"][1]["investigation_id"] == "inv-2"
    
    def test_list_investigations_with_pagination(self, client_with_mocked_service, sample_investigations_list):
        """Test investigations listing with pagination parameters."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.list_session_investigations = AsyncMock(return_value=sample_investigations_list)
        
        response = client.get("/agent/sessions/session-123/investigations?limit=5&offset=10")
        
        assert response.status_code == 200
        
        response_data = response.json()
        assert response_data["limit"] == 5
        assert response_data["offset"] == 10
        
        # Verify service was called with correct parameters
        mock_agent_service.list_session_investigations.assert_called_once_with(
            session_id="session-123",
            limit=5,
            offset=10
        )
    
    def test_list_investigations_session_not_found(self, client_with_mocked_service):
        """Test investigations listing when session not found."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.list_session_investigations = AsyncMock(
            side_effect=FileNotFoundError("Session not found")
        )
        
        response = client.get("/agent/sessions/nonexistent/investigations")
        
        assert response.status_code == 404
        response_data = response.json()
        assert "Session not found" in response_data["detail"]
    
    def test_list_investigations_validation_error(self, client_with_mocked_service):
        """Test investigations listing with validation error."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.list_session_investigations = AsyncMock(
            side_effect=ValueError("Invalid pagination parameters")
        )
        
        response = client.get("/agent/sessions/session-123/investigations?limit=-1")
        
        assert response.status_code == 400
        response_data = response.json()
        assert "Invalid pagination parameters" in response_data["detail"]


class TestHealthCheckEndpoint:
    """Test the /health endpoint."""
    
    @pytest.fixture
    def mock_agent_service(self):
        """Mock AgentService for testing."""
        service = Mock(spec=AgentService)
        return service
    
    @pytest.fixture
    def client_with_mocked_service(self, mock_agent_service):
        """TestClient with mocked AgentService dependency."""
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    def test_health_check_success(self, client_with_mocked_service):
        """Test successful health check."""
        client, mock_agent_service = client_with_mocked_service
        
        mock_health_status = {
            "status": "healthy",
            "service": "agent_service",
            "components": {
                "llm_provider": "healthy",
                "sanitizer": "healthy",
                "tracer": "healthy",
                "tools": "healthy (2 tools available)"
            }
        }
        
        mock_agent_service.health_check = AsyncMock(return_value=mock_health_status)
        
        response = client.get("/agent/health")
        
        assert response.status_code == 200
        
        response_data = response.json()
        assert response_data["status"] == "healthy"
        assert response_data["service"] == "agent"
        assert "details" in response_data
        assert response_data["details"]["components"]["llm_provider"] == "healthy"
    
    def test_health_check_failure(self, client_with_mocked_service):
        """Test health check failure."""
        client, mock_agent_service = client_with_mocked_service
        mock_agent_service.health_check = AsyncMock(side_effect=Exception("Health check failed"))
        
        response = client.get("/agent/health")
        
        assert response.status_code == 503
        response_data = response.json()
        assert "Agent service unavailable" in response_data["detail"]


class TestRequestResponseHeaders:
    """Test HTTP headers and content types."""
    
    @pytest.fixture
    def sample_agent_response(self):
        """Sample AgentResponse for testing."""
        view_state = ViewState(
            session_id="test-session",
            case_id="test-case",
            running_summary="Test response",
            uploaded_data=[]
        )
        
        return AgentResponse(
            content="Test response for headers",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
    
    @pytest.fixture
    def client_with_mocked_service(self, sample_agent_response):
        """TestClient with mocked AgentService dependency."""
        mock_agent_service = Mock(spec=AgentService)
        mock_agent_service.process_query = AsyncMock(return_value=sample_agent_response)
        
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    def test_query_endpoint_content_type(self, client_with_mocked_service):
        """Test query endpoint sets correct content type."""
        client, mock_agent_service = client_with_mocked_service
        
        response = client.post("/agent/query", json={
            "session_id": "test-session",
            "query": "test query"
        })
        
        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
    
    def test_query_endpoint_accepts_json_only(self):
        """Test query endpoint requires JSON content type."""
        client = TestClient(test_app)
        
        # Send form data instead of JSON
        response = client.post(
            "/agent/query",
            data={"session_id": "test", "query": "test"},
            headers={"content-type": "application/x-www-form-urlencoded"}
        )
        
        # Should reject non-JSON requests
        assert response.status_code == 422


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    @pytest.fixture
    def client_with_unicode_response(self):
        """TestClient with mocked AgentService that returns unicode response."""
        mock_agent_service = Mock(spec=AgentService)
        
        view_state = ViewState(
            session_id="unicode-session",
            case_id="unicode-case",
            running_summary="Unicode test 🚀",
            uploaded_data=[]
        )
        
        unicode_response = AgentResponse(
            content="Response with émojis 🔧 and unicode: 测试中文",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        mock_agent_service.process_query = AsyncMock(return_value=unicode_response)
        
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    def test_query_endpoint_unicode_content(self, client_with_unicode_response):
        """Test query endpoint handles unicode content correctly."""
        client, mock_agent_service = client_with_unicode_response
        
        response = client.post("/agent/query", json={
            "session_id": "unicode-session",
            "query": "Query with unicode: 测试查询 🔍"
        })
        
        assert response.status_code == 200
        
        response_data = response.json()
        assert "🔧" in response_data["content"]
        assert "测试中文" in response_data["content"]
    
    @pytest.fixture
    def client_with_large_response(self):
        """TestClient with mocked AgentService that handles large responses."""
        mock_agent_service = Mock(spec=AgentService)
        
        view_state = ViewState(
            session_id="large-session",
            case_id="large-case",
            running_summary="Large request test",
            uploaded_data=[]
        )
        
        large_response = AgentResponse(
            content="Response to large query",
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        mock_agent_service.process_query = AsyncMock(return_value=large_response)
        
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    def test_query_endpoint_large_request(self, client_with_large_response):
        """Test query endpoint with large request payload."""
        client, mock_agent_service = client_with_large_response
        
        # Create large query (but not too large to cause issues)
        large_query = "Describe the issue: " + "x" * 5000
        
        response = client.post("/agent/query", json={
            "session_id": "large-session",
            "query": large_query
        })
        
        assert response.status_code == 200
        
        # Verify service received the large query
        call_args = mock_agent_service.process_query.call_args[0][0]
        assert len(call_args.query) > 5000
    
    @pytest.fixture
    def client_with_empty_response(self):
        """TestClient with mocked AgentService that returns empty content."""
        mock_agent_service = Mock(spec=AgentService)
        
        view_state = ViewState(
            session_id="empty-session",
            case_id="empty-case",
            running_summary="Empty content test",
            uploaded_data=[]
        )
        
        empty_response = AgentResponse(
            content="",  # Empty content
            response_type=ResponseType.ANSWER,
            view_state=view_state,
            sources=[]
        )
        
        mock_agent_service.process_query = AsyncMock(return_value=empty_response)
        
        # Override the dependency
        test_app.dependency_overrides[get_agent_service] = lambda: mock_agent_service
        client = TestClient(test_app)
        
        yield client, mock_agent_service
        
        # Clean up dependency override
        test_app.dependency_overrides.clear()
    
    def test_troubleshoot_legacy_empty_response_content(self, client_with_empty_response):
        """Test legacy endpoint handles empty response content."""
        client, mock_agent_service = client_with_empty_response
        
        response = client.post("/agent/troubleshoot", json={
            "session_id": "empty-session",
            "query": "test query"
        })
        
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Should create default structure for empty content
        assert len(response_data["findings"]) == 1
        assert response_data["findings"][0]["type"] == "general"
        assert response_data["recommendations"] == ["Review the analysis above"]