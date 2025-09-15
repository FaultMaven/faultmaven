"""
Test session timeout and cleanup functionality for frontend crash recovery support.

These tests verify the timeout behavior requested by the frontend team:
- Session timeout enforcement with configurable timeouts (1-8 hours)
- Proper error responses (404/410) for expired session resumption attempts  
- Session cleanup background job functionality
- Timeout parameter validation and clamping
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient

from faultmaven.main import app
from faultmaven.api.v1.routes.session import validate_session_timeout, SESSION_MIN_TIMEOUT_MINUTES, SESSION_MAX_TIMEOUT_MINUTES, SESSION_DEFAULT_TIMEOUT_MINUTES
from faultmaven.services.session import SessionService


class TestSessionTimeoutValidation:
    """Test session timeout parameter validation and clamping."""
    
    def test_default_timeout_when_none_provided(self):
        """Test that default timeout is used when none provided."""
        result = validate_session_timeout(None)
        assert result == SESSION_DEFAULT_TIMEOUT_MINUTES
    
    def test_default_timeout_when_zero_provided(self):
        """Test that default timeout is used when zero provided."""
        result = validate_session_timeout(0)
        assert result == SESSION_DEFAULT_TIMEOUT_MINUTES
    
    def test_timeout_clamping_below_minimum(self):
        """Test that timeout below minimum is clamped to minimum."""
        result = validate_session_timeout(30)  # Below 60 min minimum
        assert result == SESSION_MIN_TIMEOUT_MINUTES  # Should be 60
    
    def test_timeout_clamping_above_maximum(self):
        """Test that timeout above maximum is clamped to maximum."""
        result = validate_session_timeout(600)  # Above 480 min maximum
        assert result == SESSION_MAX_TIMEOUT_MINUTES  # Should be 480
    
    def test_valid_timeout_unchanged(self):
        """Test that valid timeout in range is unchanged."""
        valid_timeout = 120  # 2 hours - should be in valid range
        result = validate_session_timeout(valid_timeout)
        assert result == valid_timeout
    
    def test_boundary_values(self):
        """Test timeout validation at boundary values."""
        # Test minimum boundary
        result = validate_session_timeout(SESSION_MIN_TIMEOUT_MINUTES)
        assert result == SESSION_MIN_TIMEOUT_MINUTES
        
        # Test maximum boundary
        result = validate_session_timeout(SESSION_MAX_TIMEOUT_MINUTES)
        assert result == SESSION_MAX_TIMEOUT_MINUTES


class TestSessionCreationWithTimeout:
    """Test session creation with timeout parameters."""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_session_creation_with_custom_timeout(self, mock_get_service, client):
        """Test session creation with custom timeout parameter."""
        # Mock the session service
        mock_service = AsyncMock()
        mock_session = MagicMock()
        mock_session.session_id = "test-session-123"
        mock_session.user_id = "user-456"
        mock_session.created_at = datetime.utcnow()
        
        mock_service.create_session.return_value = (mock_session, False)
        mock_get_service.return_value = mock_service
        
        # Create session with custom timeout
        response = client.post("/api/v1/sessions", json={
            "timeout_minutes": 240,  # 4 hours
            "session_type": "troubleshooting",
            "client_id": "test-client-123"
        })
        
        assert response.status_code == 201
        data = response.json()
        assert data["session_id"] == "test-session-123"
        assert data["timeout_minutes"] == 240  # Should return validated timeout
        assert data["session_resumed"] is False
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_session_creation_timeout_clamping(self, mock_get_service, client):
        """Test session creation with timeout that gets clamped."""
        # Mock the session service
        mock_service = AsyncMock()
        mock_session = MagicMock()
        mock_session.session_id = "test-session-456"
        mock_session.user_id = "user-789"  
        mock_session.created_at = datetime.utcnow()
        
        mock_service.create_session.return_value = (mock_session, False)
        mock_get_service.return_value = mock_service
        
        # Create session with timeout that should be clamped
        response = client.post("/api/v1/sessions", json={
            "timeout_minutes": 30,  # Below minimum, should be clamped to 60
            "session_type": "troubleshooting",
            "client_id": "test-client-456"
        })
        
        assert response.status_code == 201
        data = response.json()
        assert data["timeout_minutes"] == 60  # Should be clamped to minimum


class TestSessionExpiration:
    """Test session expiration and error responses."""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_expired_session_returns_404(self, mock_get_service, client):
        """Test that accessing expired session returns 404."""
        # Mock the session service to return None (expired session)
        mock_service = AsyncMock()
        mock_service.get_session.return_value = None
        mock_get_service.return_value = mock_service
        
        response = client.get("/api/v1/sessions/expired-session-123")
        
        assert response.status_code == 404
        assert "Session not found" in response.json()["detail"]
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_client_resume_expired_session_creates_new(self, mock_get_service, client):
        """Test that client resuming expired session gets a new session."""
        # Mock the session service to simulate expired session behavior
        mock_service = AsyncMock()
        mock_new_session = MagicMock()
        mock_new_session.session_id = "new-session-789"
        mock_new_session.user_id = "user-123"
        mock_new_session.created_at = datetime.utcnow()
        
        # Return (session, False) indicating new session created 
        mock_service.create_session.return_value = (mock_new_session, False)
        mock_get_service.return_value = mock_service
        
        # Attempt to resume with client_id (would normally resume if not expired)
        response = client.post("/api/v1/sessions", json={
            "timeout_minutes": 180,
            "session_type": "troubleshooting",
            "client_id": "client-with-expired-session"
        })
        
        assert response.status_code == 201
        data = response.json()
        assert data["session_id"] == "new-session-789"
        assert data["session_resumed"] is False  # Should be False since expired session was cleaned up


class TestSessionCleanup:
    """Test session cleanup functionality."""
    
    @pytest.fixture
    def session_service(self):
        """Create a SessionService instance for testing."""
        service = SessionService()
        service.session_manager = AsyncMock()
        service.session_store = AsyncMock()
        return service
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions(self, session_service):
        """Test cleanup of expired sessions."""
        # Mock expired sessions
        expired_session1 = MagicMock()
        expired_session1.session_id = "expired-1"
        expired_session2 = MagicMock()
        expired_session2.session_id = "expired-2"
        active_session = MagicMock()
        active_session.session_id = "active-1"
        
        session_service.session_manager.get_all_sessions.return_value = [
            expired_session1, expired_session2, active_session
        ]
        
        # Mock _is_active to return False for expired, True for active
        async def mock_is_active(session):
            return session.session_id == "active-1"
        
        session_service._is_active = mock_is_active
        session_service.delete_session = AsyncMock()
        
        # Run cleanup
        cleaned_count = await session_service.cleanup_expired_sessions()
        
        # Verify cleanup results
        assert cleaned_count == 2
        assert session_service.delete_session.call_count == 2
        session_service.delete_session.assert_any_call("expired-1")
        session_service.delete_session.assert_any_call("expired-2")
    
    @pytest.mark.asyncio
    async def test_cleanup_handles_errors_gracefully(self, session_service):
        """Test that cleanup handles individual session deletion errors gracefully."""
        # Mock sessions
        problematic_session = MagicMock()
        problematic_session.session_id = "problematic"
        good_session = MagicMock()
        good_session.session_id = "good"
        
        session_service.session_manager.get_all_sessions.return_value = [
            problematic_session, good_session
        ]
        
        # Mock _is_active to return False (both expired)
        session_service._is_active = AsyncMock(return_value=False)
        
        # Mock delete_session to fail for problematic session
        async def mock_delete(session_id):
            if session_id == "problematic":
                raise Exception("Deletion failed")
            return True
        
        session_service.delete_session = AsyncMock(side_effect=mock_delete)
        
        # Run cleanup - should handle error gracefully
        cleaned_count = await session_service.cleanup_expired_sessions()
        
        # Should still clean up the good session
        assert cleaned_count == 1
        session_service.delete_session.assert_any_call("good")


class TestSessionCleanupAPI:
    """Test session cleanup API endpoint."""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_cleanup_endpoint(self, mock_get_service, client):
        """Test manual session cleanup endpoint."""
        # Mock the session service
        mock_service = AsyncMock()
        mock_service.cleanup_expired_sessions.return_value = 5
        mock_get_service.return_value = mock_service
        
        response = client.post("/api/v1/sessions/cleanup")
        
        assert response.status_code == 200
        data = response.json()
        assert data["cleaned_sessions"] == 5
        assert "Successfully cleaned up 5 expired sessions" in data["message"]
        
        # Verify cleanup was called
        mock_service.cleanup_expired_sessions.assert_called_once()


class TestIntegrationScenarios:
    """Integration tests for frontend crash recovery scenarios."""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)
    
    @patch('faultmaven.api.v1.routes.session.get_session_service')
    def test_frontend_crash_recovery_flow(self, mock_get_service, client):
        """Test complete frontend crash recovery flow."""
        mock_service = AsyncMock()
        
        # Step 1: Frontend creates session with client_id
        session1 = MagicMock()
        session1.session_id = "session-123"
        session1.user_id = "user-456"
        session1.created_at = datetime.utcnow()
        
        mock_service.create_session.return_value = (session1, False)
        mock_get_service.return_value = mock_service
        
        response1 = client.post("/api/v1/sessions", json={
            "timeout_minutes": 180,
            "session_type": "troubleshooting",
            "client_id": "browser-client-abc123"
        })
        
        assert response1.status_code == 201
        assert response1.json()["session_resumed"] is False
        
        # Step 2: Frontend crashes and restarts, tries to resume
        # Mock service to return resumed session
        mock_service.create_session.return_value = (session1, True)  # Same session, resumed=True
        
        response2 = client.post("/api/v1/sessions", json={
            "timeout_minutes": 180,
            "session_type": "troubleshooting", 
            "client_id": "browser-client-abc123"  # Same client_id
        })
        
        assert response2.status_code == 201
        data2 = response2.json()
        assert data2["session_id"] == "session-123"  # Same session ID
        assert data2["session_resumed"] is True  # Should be resumed
        
        # Step 3: After session expires, frontend should get new session
        mock_new_session = MagicMock()
        mock_new_session.session_id = "session-456"  # Different session
        mock_new_session.user_id = "user-456"
        mock_new_session.created_at = datetime.utcnow()
        
        mock_service.create_session.return_value = (mock_new_session, False)  # New session
        
        response3 = client.post("/api/v1/sessions", json={
            "timeout_minutes": 180,
            "session_type": "troubleshooting",
            "client_id": "browser-client-abc123"  # Same client_id, but expired session
        })
        
        assert response3.status_code == 201
        data3 = response3.json()
        assert data3["session_id"] == "session-456"  # New session ID
        assert data3["session_resumed"] is False  # Should be new session