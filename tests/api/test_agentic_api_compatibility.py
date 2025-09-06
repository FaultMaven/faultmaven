"""API Compatibility Tests for Agentic Framework Integration

This module contains comprehensive tests to ensure that the Agentic Framework
integration maintains 100% API compatibility with existing FaultMaven endpoints
and contracts. No external behavior should change for API consumers.
"""

import pytest
import json
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
from typing import Dict, Any

import httpx
from fastapi.testclient import TestClient

from faultmaven.main import app
from faultmaven.models import (
    QueryRequest,
    AgentResponse,
    ViewState,
    ResponseType,
    DataType,
    UploadedData
)


class TestAgenticAPICompatibility:
    """Test cases ensuring API compatibility with Agentic Framework integration."""

    @pytest.fixture
    def test_client(self):
        """Test client for API testing."""
        return TestClient(app)

    @pytest.fixture
    def mock_agentic_container(self):
        """Mock container with Agentic Framework for API tests."""
        with patch('faultmaven.container.container') as mock_container:
            # Mock agent service with Agentic Framework
            mock_agent_service = Mock()
            mock_agent_service.process_query = AsyncMock()
            mock_container.get_agent_service.return_value = mock_agent_service
            
            # Mock other services
            mock_container.get_session_service.return_value = Mock()
            mock_container.get_knowledge_service.return_value = Mock()
            mock_container.get_data_service.return_value = Mock()
            
            yield mock_container

    @pytest.mark.api
    def test_agent_query_endpoint_signature_unchanged(self, test_client, mock_agentic_container):
        """Test that agent query endpoint signature remains unchanged."""
        # Mock Agentic Framework response
        mock_response = AgentResponse(
            response="Test response from Agentic Framework",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.89,
            view_state=ViewState(),
            session_id="test-session-123"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Test request payload structure (should be unchanged)
        request_payload = {
            "query": "Test query for API compatibility",
            "session_id": "test-session-123",
            "user_id": "test-user-456"
        }
        
        # Make API request
        response = test_client.post("/api/v1/agent/query", json=request_payload)
        
        # Verify response structure unchanged
        assert response.status_code == 200
        response_data = response.json()
        
        # Verify all expected fields are present
        expected_fields = ["response", "response_type", "confidence_score", "view_state", "session_id"]
        for field in expected_fields:
            assert field in response_data, f"Missing field: {field}"

    @pytest.mark.api
    def test_response_schema_compatibility(self, test_client, mock_agentic_container):
        """Test that response schema remains compatible."""
        # Mock comprehensive Agentic Framework response
        mock_view_state = ViewState(
            current_phase="analysis",
            findings=["Agentic Framework finding 1", "Agentic Framework finding 2"],
            recommendations=["Use Agentic Framework recommendation"],
            tools_used=["agentic_tool_1", "agentic_tool_2"],
            confidence_breakdown={"analysis": 0.9, "recommendations": 0.85}
        )
        
        mock_response = AgentResponse(
            response="Comprehensive analysis from Agentic Framework",
            response_type=ResponseType.INVESTIGATION_UPDATE,
            confidence_score=0.93,
            view_state=mock_view_state,
            session_id="compatibility-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        request_payload = {
            "query": "Complex troubleshooting query",
            "session_id": "compatibility-test-session"
        }
        
        response = test_client.post("/api/v1/agent/query", json=request_payload)
        
        assert response.status_code == 200
        response_data = response.json()
        
        # Verify response schema structure
        assert "response" in response_data
        assert "response_type" in response_data
        assert "confidence_score" in response_data
        assert "view_state" in response_data
        assert "session_id" in response_data
        
        # Verify view_state structure unchanged
        view_state = response_data["view_state"]
        assert "current_phase" in view_state
        assert "findings" in view_state
        assert "recommendations" in view_state
        assert "tools_used" in view_state
        
        # Verify response types are valid
        assert response_data["response_type"] in ["initial_analysis", "clarification_request", "investigation_update", "final_recommendation"]

    @pytest.mark.api
    def test_http_status_codes_unchanged(self, test_client, mock_agentic_container):
        """Test that HTTP status codes remain unchanged."""
        # Test success case
        mock_response = AgentResponse(
            response="Success response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.8,
            view_state=ViewState(),
            session_id="status-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Valid query",
            "session_id": "status-test-session"
        })
        assert response.status_code == 200
        
        # Test validation error case
        response = test_client.post("/api/v1/agent/query", json={
            "query": "",  # Invalid empty query
            "session_id": "status-test-session"
        })
        assert response.status_code == 422  # Validation error
        
        # Test missing required fields
        response = test_client.post("/api/v1/agent/query", json={})
        assert response.status_code == 422  # Validation error

    @pytest.mark.api
    def test_error_response_format_unchanged(self, test_client, mock_agentic_container):
        """Test that error response format remains unchanged."""
        # Mock service exception
        from faultmaven.exceptions import ServiceException
        mock_agentic_container.get_agent_service().process_query.side_effect = ServiceException("Agentic Framework error")
        
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Test query",
            "session_id": "error-test-session"
        })
        
        # Should return error in expected format
        assert response.status_code == 500
        error_data = response.json()
        assert "detail" in error_data

    @pytest.mark.api
    def test_query_processing_time_acceptable(self, test_client, mock_agentic_container):
        """Test that query processing time remains acceptable with Agentic Framework."""
        import time
        
        # Mock fast Agentic Framework response
        mock_response = AgentResponse(
            response="Fast Agentic Framework response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.87,
            view_state=ViewState(),
            session_id="performance-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        request_payload = {
            "query": "Performance test query",
            "session_id": "performance-test-session"
        }
        
        start_time = time.time()
        response = test_client.post("/api/v1/agent/query", json=request_payload)
        end_time = time.time()
        
        # Verify response time is acceptable
        response_time = end_time - start_time
        assert response_time < 30.0  # Should complete within 30 seconds
        assert response.status_code == 200

    @pytest.mark.api
    def test_session_id_handling_unchanged(self, test_client, mock_agentic_container):
        """Test that session ID handling remains unchanged."""
        test_session_id = "session-handling-test-123"
        
        mock_response = AgentResponse(
            response="Session test response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.82,
            view_state=ViewState(),
            session_id=test_session_id
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Test with provided session ID
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Session test query",
            "session_id": test_session_id
        })
        
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["session_id"] == test_session_id
        
        # Verify session ID was passed to agent service correctly
        mock_agentic_container.get_agent_service().process_query.assert_called_once()
        call_args = mock_agentic_container.get_agent_service().process_query.call_args[0][0]
        assert call_args.session_id == test_session_id

    @pytest.mark.api
    def test_user_id_handling_unchanged(self, test_client, mock_agentic_container):
        """Test that user ID handling remains unchanged."""
        test_user_id = "user-handling-test-456"
        
        mock_response = AgentResponse(
            response="User test response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.84,
            view_state=ViewState(),
            session_id="user-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Test with provided user ID
        response = test_client.post("/api/v1/agent/query", json={
            "query": "User test query",
            "session_id": "user-test-session",
            "user_id": test_user_id
        })
        
        assert response.status_code == 200
        
        # Verify user ID was passed correctly
        call_args = mock_agentic_container.get_agent_service().process_query.call_args[0][0]
        assert call_args.user_id == test_user_id

    @pytest.mark.api
    def test_content_type_handling_unchanged(self, test_client, mock_agentic_container):
        """Test that content type handling remains unchanged."""
        mock_response = AgentResponse(
            response="Content type test response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.86,
            view_state=ViewState(),
            session_id="content-type-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Test JSON content type
        response = test_client.post(
            "/api/v1/agent/query",
            json={"query": "Content type test", "session_id": "content-type-session"},
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    @pytest.mark.api
    def test_enhanced_features_delivered_through_existing_api(self, test_client, mock_agentic_container):
        """Test that enhanced Agentic Framework features are delivered through existing API."""
        # Mock enhanced Agentic Framework response with advanced capabilities
        enhanced_view_state = ViewState(
            current_phase="advanced_analysis",
            findings=[
                "Advanced ML-based pattern detection identified root cause",
                "Predictive analysis suggests 23% failure probability in next 24h",
                "Cross-system correlation analysis complete"
            ],
            recommendations=[
                "Implement predictive monitoring based on ML model",
                "Scale connection pool proactively",
                "Enable cascade failure prevention"
            ],
            tools_used=["ml_analyzer", "predictive_engine", "correlation_analyzer"],
            confidence_breakdown={
                "ml_analysis": 0.96,
                "predictive_accuracy": 0.89,
                "correlation_strength": 0.94
            }
        )
        
        mock_response = AgentResponse(
            response="Advanced AI analysis complete. Using machine learning models and predictive analytics, I've identified not only the current issue but potential future problems.",
            response_type=ResponseType.INVESTIGATION_UPDATE,
            confidence_score=0.95,  # Higher confidence from advanced AI
            view_state=enhanced_view_state,
            session_id="enhanced-features-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Standard API request - no changes needed by client
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Need comprehensive system analysis with advanced AI capabilities",
            "session_id": "enhanced-features-session"
        })
        
        assert response.status_code == 200
        response_data = response.json()
        
        # Verify enhanced capabilities delivered through standard API
        assert response_data["confidence_score"] > 0.9  # Higher confidence
        assert "Advanced AI analysis" in response_data["response"]
        assert "ml_analyzer" in response_data["view_state"]["tools_used"]
        assert len(response_data["view_state"]["findings"]) >= 3  # More comprehensive findings

    @pytest.mark.api 
    def test_backward_compatibility_with_existing_clients(self, test_client, mock_agentic_container):
        """Test backward compatibility with existing API clients."""
        # Mock response that includes all fields existing clients expect
        mock_response = AgentResponse(
            response="Backward compatible response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.83,
            view_state=ViewState(
                current_phase="analysis",
                findings=["Compatible finding"],
                recommendations=["Compatible recommendation"],
                tools_used=["compatible_tool"],
                confidence_breakdown={"overall": 0.83}
            ),
            session_id="backward-compat-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Simulate old client request format
        old_client_request = {
            "query": "Legacy client query",
            "session_id": "backward-compat-session"
            # Older clients might not send user_id or other new fields
        }
        
        response = test_client.post("/api/v1/agent/query", json=old_client_request)
        
        assert response.status_code == 200
        response_data = response.json()
        
        # Verify all expected fields are present for backward compatibility
        required_fields = ["response", "response_type", "confidence_score", "view_state", "session_id"]
        for field in required_fields:
            assert field in response_data
            assert response_data[field] is not None

    @pytest.mark.api
    def test_api_versioning_unchanged(self, test_client):
        """Test that API versioning remains unchanged."""
        # Test that v1 endpoints still exist
        response = test_client.get("/api/v1/")
        # Should not return 404
        assert response.status_code != 404
        
        # Test that agent endpoints are still under v1
        test_request = {"query": "Version test", "session_id": "version-test"}
        response = test_client.post("/api/v1/agent/query", json=test_request)
        # Should not return 404 (may return other errors but endpoint should exist)
        assert response.status_code != 404

    @pytest.mark.api
    def test_cors_headers_unchanged(self, test_client):
        """Test that CORS headers remain unchanged."""
        response = test_client.options("/api/v1/agent/query")
        
        # Check for CORS headers (if they were present before)
        # This test ensures CORS configuration isn't broken by Agentic Framework
        assert response.status_code in [200, 405]  # OPTIONS should be handled

    @pytest.mark.api
    def test_rate_limiting_compatibility(self, test_client, mock_agentic_container):
        """Test that rate limiting (if present) works with Agentic Framework."""
        mock_response = AgentResponse(
            response="Rate limit test response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.81,
            view_state=ViewState(),
            session_id="rate-limit-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Make multiple rapid requests
        responses = []
        for i in range(5):
            response = test_client.post("/api/v1/agent/query", json={
                "query": f"Rate limit test {i}",
                "session_id": f"rate-limit-session-{i}"
            })
            responses.append(response)
        
        # Should handle requests appropriately (either succeed or rate limit)
        for response in responses:
            assert response.status_code in [200, 429]  # Success or rate limited

    @pytest.mark.api
    def test_authentication_compatibility(self, test_client):
        """Test that authentication (if present) remains compatible."""
        # Test with various auth scenarios that might exist
        
        # Test without auth headers
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Auth test",
            "session_id": "auth-test-session"
        })
        
        # Should handle authentication appropriately
        assert response.status_code in [200, 401, 403, 422]  # Various valid responses

    @pytest.mark.api
    def test_request_validation_unchanged(self, test_client):
        """Test that request validation remains unchanged."""
        # Test various invalid requests
        invalid_requests = [
            {},  # Empty request
            {"query": ""},  # Empty query
            {"query": "valid", "session_id": ""},  # Empty session ID
            {"session_id": "valid"},  # Missing query
            {"query": None, "session_id": "valid"},  # None query
        ]
        
        for invalid_request in invalid_requests:
            response = test_client.post("/api/v1/agent/query", json=invalid_request)
            # Should return validation error
            assert response.status_code == 422

    @pytest.mark.api
    def test_response_headers_unchanged(self, test_client, mock_agentic_container):
        """Test that response headers remain unchanged."""
        mock_response = AgentResponse(
            response="Headers test response",
            response_type=ResponseType.INITIAL_ANALYSIS,
            confidence_score=0.88,
            view_state=ViewState(),
            session_id="headers-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        response = test_client.post("/api/v1/agent/query", json={
            "query": "Headers test",
            "session_id": "headers-test-session"
        })
        
        # Verify expected headers are present
        assert "content-type" in response.headers
        assert response.headers["content-type"] == "application/json"

    @pytest.mark.api
    def test_no_breaking_changes_in_api_contract(self, test_client, mock_agentic_container):
        """Comprehensive test ensuring no breaking changes in API contract."""
        # Mock comprehensive response that tests all aspects
        comprehensive_view_state = ViewState(
            current_phase="comprehensive_test",
            findings=["Test finding 1", "Test finding 2"],
            recommendations=["Test recommendation 1", "Test recommendation 2"],
            tools_used=["test_tool_1", "test_tool_2"],
            confidence_breakdown={"test": 0.9}
        )
        
        mock_response = AgentResponse(
            response="Comprehensive API contract test response",
            response_type=ResponseType.FINAL_RECOMMENDATION,
            confidence_score=0.92,
            view_state=comprehensive_view_state,
            session_id="contract-test-session"
        )
        mock_agentic_container.get_agent_service().process_query.return_value = mock_response
        
        # Test comprehensive request
        request_payload = {
            "query": "Comprehensive API contract test query with all features",
            "session_id": "contract-test-session",
            "user_id": "contract-test-user"
        }
        
        response = test_client.post("/api/v1/agent/query", json=request_payload)
        
        # Verify complete API contract compliance
        assert response.status_code == 200
        
        response_data = response.json()
        
        # Verify response structure is complete and unchanged
        assert isinstance(response_data["response"], str)
        assert response_data["response_type"] in ["initial_analysis", "clarification_request", "investigation_update", "final_recommendation"]
        assert isinstance(response_data["confidence_score"], (int, float))
        assert 0 <= response_data["confidence_score"] <= 1
        assert isinstance(response_data["view_state"], dict)
        assert isinstance(response_data["session_id"], str)
        
        # Verify view_state structure is complete
        view_state = response_data["view_state"]
        assert "current_phase" in view_state
        assert "findings" in view_state
        assert "recommendations" in view_state
        assert "tools_used" in view_state
        
        # Verify agent service was called with correct parameters
        mock_agentic_container.get_agent_service().process_query.assert_called_once()
        call_args = mock_agentic_container.get_agent_service().process_query.call_args[0][0]
        assert isinstance(call_args, QueryRequest)
        assert call_args.query == request_payload["query"]
        assert call_args.session_id == request_payload["session_id"]
        assert call_args.user_id == request_payload["user_id"]