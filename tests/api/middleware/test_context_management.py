"""
Tests for context management in LoggingMiddleware.

This test suite validates the enhanced LoggingMiddleware through proper integration
testing that exercises the full middleware stack with real HTTP requests.

Architecture Compliance:
- Uses integration testing approach with TestClient for real middleware execution
- Tests actual HTTP request/response cycles through middleware stack
- Validates context extraction, population, and propagation
- Ensures graceful degradation for error scenarios

Test Strategy:
- Real FastAPI app with middleware for authentic behavior
- Comprehensive context extraction from headers, query params, and body
- Priority ordering validation (header > query > body)
- Error handling and graceful degradation testing
- Context isolation between requests validation
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.infrastructure.logging.coordinator import request_context, RequestContext
from faultmaven.models import SessionContext


@pytest.fixture
def app():
    """Create test FastAPI app with LoggingMiddleware."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)
    
    @app.get("/test")
    async def test_endpoint():
        # Return current context for testing
        ctx = request_context.get()
        return {
            "correlation_id": ctx.correlation_id if ctx else None,
            "session_id": ctx.session_id if ctx else None,
            "user_id": ctx.user_id if ctx else None,
            "case_id": ctx.case_id if ctx else None,
        }
    
    @app.post("/test")
    async def test_post_endpoint():
        # Return current context for testing
        ctx = request_context.get()
        return {
            "correlation_id": ctx.correlation_id if ctx else None,
            "session_id": ctx.session_id if ctx else None,
            "user_id": ctx.user_id if ctx else None,
            "case_id": ctx.case_id if ctx else None,
        }
    
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_session():
    """Mock session object."""
    session = Mock(spec=SessionContext)
    session.session_id = "test-session-123"
    session.user_id = "test-user-456"
    return session


class TestContextExtractionFromHeaders:
    """Test session context extraction from HTTP headers."""
    
    def test_extract_session_id_from_header(self, client):
        """Test session_id extraction from X-Session-ID header."""
        response = client.get("/test", headers={"X-Session-ID": "header-session-123"})
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "header-session-123"
        assert data["correlation_id"] is not None
    
    def test_extract_case_id_from_header(self, client):
        """Test case_id extraction from X-Case-ID header (and legacy header)."""
        response = client.get("/test", headers={"X-Case-ID": "case-789"})
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "case-789"
        # Legacy header support
        response = client.get("/test", headers={"X-Investigation-ID": "legacy-789"})
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "legacy-789"
    
    def test_extract_both_ids_from_headers(self, client):
        """Test extraction of both session_id and case_id from headers."""
        headers = {
            "X-Session-ID": "header-session-123",
            "X-Case-ID": "case-789"
        }
        response = client.get("/test", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "header-session-123"
        assert data["case_id"] == "case-789"


class TestContextExtractionFromQueryParams:
    """Test session context extraction from query parameters."""
    
    def test_extract_session_id_from_query(self, client):
        """Test session_id extraction from query parameter."""
        response = client.get("/test?session_id=query-session-456")
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "query-session-456"
    
    def test_extract_case_id_from_query(self, client):
        """Test case_id extraction from query parameter (legacy supported)."""
        response = client.get("/test?case_id=query-case-789")
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "query-case-789"
        # Legacy query param
        response = client.get("/test?investigation_id=query-legacy-789")
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "query-legacy-789"


class TestContextExtractionFromBody:
    """Test session context extraction from request body."""
    
    def test_extract_session_id_from_json_body(self, client):
        """Test session_id extraction from JSON request body."""
        payload = {"session_id": "body-session-789", "query": "test query"}
        response = client.post("/test", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "body-session-789"
    
    def test_extract_case_id_from_json_body(self, client):
        """Test case_id extraction from JSON request body (legacy supported)."""
        payload = {"case_id": "body-case-123", "data": "test"}
        response = client.post("/test", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "body-case-123"
        # Legacy body
        payload = {"investigation_id": "body-legacy-123", "data": "test"}
        response = client.post("/test", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["case_id"] == "body-legacy-123"
    
    def test_extract_both_ids_from_json_body(self, client):
        """Test extraction of both IDs from JSON request body."""
        payload = {
            "session_id": "body-session-789",
            "case_id": "body-case-123",
            "query": "test query"
        }
        response = client.post("/test", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "body-session-789"
        assert data["case_id"] == "body-case-123"


class TestContextExtractionPriority:
    """Test priority order of context extraction sources."""
    
    def test_header_takes_priority_over_query(self, client):
        """Test that header value takes priority over query parameter."""
        headers = {"X-Session-ID": "header-session"}
        response = client.get("/test?session_id=query-session", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "header-session"
    
    def test_query_takes_priority_over_body(self, client):
        """Test that query parameter takes priority over body."""
        payload = {"session_id": "body-session"}
        response = client.post("/test?session_id=query-session", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "query-session"
    
    def test_header_takes_priority_over_body(self, client):
        """Test that header takes priority over body."""
        headers = {"X-Session-ID": "header-session"}
        payload = {"session_id": "body-session"}
        response = client.post("/test", headers=headers, json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "header-session"


class TestUserIdLookup:
    """Test user_id lookup from session_id through integration testing."""
    
    def test_successful_user_id_lookup(self, client, mock_session):
        """Test successful user_id lookup from session_id through full HTTP cycle."""
        # Import here to avoid circular imports
        from faultmaven.container import DIContainer
        
        # Mock session service to return test session
        mock_session_service = AsyncMock()
        mock_session_service.get_session.return_value = mock_session
        
        # Create a complete mock container that bypasses initialization
        with patch.object(DIContainer, '__new__') as mock_container_new:
            # Create a mock container instance
            mock_container_instance = Mock()
            mock_container_instance._initialized = True
            mock_container_instance._initializing = False
            mock_container_instance.get_session_service.return_value = mock_session_service
            
            # Configure the container singleton to return our mock
            mock_container_new.return_value = mock_container_instance
            
            # Make HTTP request with session header
            response = client.get("/test", headers={"X-Session-ID": "test-session-123"})
            assert response.status_code == 200
            data = response.json()
            
            # Verify context was populated correctly
            assert data["session_id"] == "test-session-123"
            assert data["user_id"] == "test-user-456"
            assert data["correlation_id"] is not None
            
            # Verify session service was called through middleware
            mock_session_service.get_session.assert_called_with(
                "test-session-123", validate=False
            )
    
    def test_user_id_lookup_session_not_found(self, client):
        """Test graceful handling when session is not found."""
        # Import here to avoid circular imports
        from faultmaven.container import DIContainer
        
        # Mock session service returning None
        mock_session_service = AsyncMock()
        mock_session_service.get_session.return_value = None
        
        # Create a complete mock container that bypasses initialization
        with patch.object(DIContainer, '__new__') as mock_container_new:
            # Create a mock container instance
            mock_container_instance = Mock()
            mock_container_instance._initialized = True
            mock_container_instance._initializing = False
            mock_container_instance.get_session_service.return_value = mock_session_service
            
            # Configure the container singleton to return our mock
            mock_container_new.return_value = mock_container_instance
            
            response = client.get("/test", headers={"X-Session-ID": "nonexistent-session"})
            assert response.status_code == 200
            data = response.json()
            assert data["session_id"] == "nonexistent-session"
            assert data["user_id"] is None  # Should be None, not cause failure
    
    def test_user_id_lookup_service_failure(self, client):
        """Test graceful handling when session service fails."""
        # Import here to avoid circular imports
        from faultmaven.container import DIContainer
        
        # Mock session service raising exception
        mock_session_service = AsyncMock()
        mock_session_service.get_session.side_effect = Exception("Service unavailable")
        
        # Create a complete mock container that bypasses initialization
        with patch.object(DIContainer, '__new__') as mock_container_new:
            # Create a mock container instance
            mock_container_instance = Mock()
            mock_container_instance._initialized = True
            mock_container_instance._initializing = False
            mock_container_instance.get_session_service.return_value = mock_session_service
            
            # Configure the container singleton to return our mock
            mock_container_new.return_value = mock_container_instance
            
            response = client.get("/test", headers={"X-Session-ID": "test-session-123"})
            assert response.status_code == 200  # Should not fail
            data = response.json()
            assert data["session_id"] == "test-session-123"
            assert data["user_id"] is None  # Should gracefully degrade


class TestContextContinuity:
    """Test that context is properly maintained and cleared."""
    
    def test_context_continuity_within_request(self, client, mock_session):
        """Test that context is maintained throughout request processing."""
        # Import here to avoid circular imports
        from faultmaven.container import DIContainer
        
        # Mock session service to return test session
        mock_session_service = AsyncMock()
        mock_session_service.get_session.return_value = mock_session
        
        # Create a complete mock container that bypasses initialization
        with patch.object(DIContainer, '__new__') as mock_container_new:
            # Create a mock container instance
            mock_container_instance = Mock()
            mock_container_instance._initialized = True
            mock_container_instance._initializing = False
            mock_container_instance.get_session_service.return_value = mock_session_service
            
            # Configure the container singleton to return our mock
            mock_container_new.return_value = mock_container_instance
            
            # Make request with both session and investigation IDs
            headers = {
                "X-Session-ID": "test-session-123",
                "X-Investigation-ID": "test-investigation-789"
            }
            response = client.get("/test", headers=headers)
            
            assert response.status_code == 200
            data = response.json()
            
            # Verify all context components are properly populated
            assert data["session_id"] == "test-session-123"
            assert data["user_id"] == "test-user-456"
            assert data["case_id"] == "test-investigation-789"
            assert data["correlation_id"] is not None
    
    def test_context_cleared_between_requests(self, client):
        """Test that context is cleared between different requests."""
        # First request with session
        response1 = client.get("/test", headers={"X-Session-ID": "session-1"})
        assert response1.status_code == 200
        data1 = response1.json()
        correlation_id_1 = data1["correlation_id"]
        
        # Second request with different session
        response2 = client.get("/test", headers={"X-Session-ID": "session-2"})
        assert response2.status_code == 200
        data2 = response2.json()
        correlation_id_2 = data2["correlation_id"]
        
        # Should have different correlation IDs and session IDs
        assert correlation_id_1 != correlation_id_2
        assert data1["session_id"] == "session-1"
        assert data2["session_id"] == "session-2"


class TestErrorHandling:
    """Test error handling in context extraction."""
    
    def test_invalid_json_body_handling(self, client):
        """Test graceful handling of invalid JSON in request body."""
        # Send invalid JSON
        response = client.post(
            "/test",
            data="invalid json content",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200  # Should not fail
        data = response.json()
        assert data["session_id"] is None  # Should gracefully handle invalid JSON
    
    def test_non_dict_json_body_handling(self, client):
        """Test graceful handling of non-dict JSON in request body."""
        # Send array instead of object
        response = client.post("/test", json=["not", "a", "dict"])
        assert response.status_code == 200  # Should not fail
        data = response.json()
        assert data["session_id"] is None  # Should handle non-dict JSON
    
    def test_missing_context_handling(self, client):
        """Test handling when no session context is provided."""
        response = client.get("/test")  # No session context
        assert response.status_code == 200
        data = response.json()
        assert data["correlation_id"] is not None  # Should still have correlation ID
        assert data["session_id"] is None
        assert data["user_id"] is None
        assert data["case_id"] is None


class TestTargetedTracingIntegration:
    """Test integration with targeted tracing functionality."""
    
    def test_context_available_for_targeted_tracing(self, client, mock_session):
        """Test that context is properly populated for targeted tracing to work."""
        # Import here to avoid circular imports
        from faultmaven.container import DIContainer
        
        # Mock session service to return test session
        mock_session_service = AsyncMock()
        mock_session_service.get_session.return_value = mock_session
        
        # Create a complete mock container that bypasses initialization
        with patch.object(DIContainer, '__new__') as mock_container_new:
            # Create a mock container instance
            mock_container_instance = Mock()
            mock_container_instance._initialized = True
            mock_container_instance._initializing = False
            mock_container_instance.get_session_service.return_value = mock_session_service
            
            # Configure the container singleton to return our mock
            mock_container_new.return_value = mock_container_instance
            
            headers = {"X-Session-ID": "test-session-123"}
            
            # Make request and verify context is set during processing
            with patch('faultmaven.infrastructure.logging.coordinator.request_context') as mock_context:
                response = client.get("/test", headers=headers)
                assert response.status_code == 200
                
                # Verify context was set with session data
                mock_context.set.assert_called()
                # The context should have been set with a RequestContext containing session_id
                call_args = mock_context.set.call_args_list
                assert len(call_args) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
