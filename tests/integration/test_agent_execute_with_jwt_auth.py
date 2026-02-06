"""Integration test for Agent Execute endpoint with JWT authentication.

This test verifies that the agent execute endpoint works correctly with
JWT tokens in local auth mode (HS256 algorithm). This addresses the issue
where tokens worked for /api/v1/cases but failed for agent execution.

Test Focus:
- Real JWT token generation and validation (not mocked)
- Local auth mode with HS256 algorithm
- End-to-end authentication flow through agent execute endpoint
- Ensures consistent algorithm selection across all endpoints
"""

import json
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.config.settings import get_settings
from faultmaven.main import app as main_app
from faultmaven.models.investigation_session import InvestigationSession, SessionStatus
from faultmaven.modules.agent.domain.events.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentType,
    ExecutionStatus,
)
from faultmaven.services.auth_service import AuthService

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def auth_service():
    """Create AuthService with local auth mode for HS256 tokens."""
    settings = get_settings()

    # Patch settings to ensure local auth mode
    with patch("faultmaven.services.auth_service.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.auth.auth_mode = "local"
        mock_settings.security.jwt_algorithm = "HS256"
        mock_settings.security.jwt_secret_key = MagicMock()
        mock_settings.security.jwt_secret_key.get_secret_value.return_value = (
            "test-secret-key-for-integration-testing"
        )
        mock_settings.security.jwt_access_token_expire_minutes = 15
        mock_settings.security.jwt_refresh_token_expire_days = 7
        mock_settings.security.jwt_issuer = "faultmaven-api"
        mock_settings.security.jwt_audience = "faultmaven-app"
        mock_settings.security.token_revocation_prefix = "revoked:token:"
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        mock_get_settings.return_value = mock_settings

        service = AuthService()
        yield service


@pytest.fixture
def jwt_token(auth_service):
    """Generate a real JWT token for testing."""
    token = auth_service.generate_access_token(
        user_id="user_123",
        organization_id="org_456",
        email="test@example.com",
        roles=["admin"],
        permissions=["sessions:execute", "executions:read"],
    )
    return token


@pytest.fixture
def mock_session():
    """Create a mock InvestigationSession."""
    session = MagicMock(spec=InvestigationSession)
    session.session_id = "session_test_auth"
    session.case_id = "case_test_auth"
    session.user_id = "user_123"
    session.organization_id = "org_456"
    session.status = SessionStatus.ACTIVE
    session.total_token_usage = 500
    session.token_budget_limit = 50000
    session.session_goal = "Test authentication flow"
    session.started_at = datetime.now(timezone.utc)
    session.created_at = datetime.now(timezone.utc)
    session.updated_at = datetime.now(timezone.utc)
    return session


@pytest.fixture
def client(auth_service, mock_session):
    """Create TestClient with mocked services but REAL auth."""
    app = main_app

    # Mock the agent orchestration service (not testing agent logic)
    mock_agent_service = AsyncMock()

    async def mock_execute(*args, **kwargs):
        """Simple mock execution that returns success."""
        yield ExecutionEvent.started(execution_id="exec_auth_test")
        yield ExecutionEvent.status_update(
            execution_id="exec_auth_test",
            status=ExecutionStatus.RUNNING,
            message="Processing request",
        )
        yield ExecutionEvent.response_chunk(
            execution_id="exec_auth_test",
            chunk="Test response",
        )
        yield ExecutionEvent.completed(
            execution_id="exec_auth_test",
            total_tokens=150,
            duration_ms=1000,
        )

    mock_agent_service.execute_investigation_step = mock_execute

    def get_mock_agent_service():
        return mock_agent_service

    # Mock session service to return our test session
    mock_session_service = AsyncMock()
    mock_session_service.get_session.return_value = mock_session

    def get_mock_session_service():
        return mock_session_service

    # Override dependencies
    from faultmaven.api.dependencies import (
        get_agent_orchestration_service,
        get_session_service,
    )

    # IMPORTANT: Do NOT override get_current_user - we want real auth
    app.dependency_overrides[get_agent_orchestration_service] = get_mock_agent_service
    app.dependency_overrides[get_session_service] = get_mock_session_service

    # Patch AuthService in the middleware to use our test auth service
    with patch(
        "faultmaven.api.middleware.auth.get_settings"
    ) as mock_get_settings_middleware:
        # Configure middleware to use same settings as our auth service
        mock_settings = MagicMock()
        mock_settings.auth.auth_mode = "local"
        mock_settings.security.jwt_algorithm = "HS256"
        mock_settings.security.jwt_secret_key = MagicMock()
        mock_settings.security.jwt_secret_key.get_secret_value.return_value = (
            "test-secret-key-for-integration-testing"
        )
        mock_settings.security.jwt_access_token_expire_minutes = 15
        mock_settings.security.jwt_refresh_token_expire_days = 7
        mock_settings.security.jwt_issuer = "faultmaven-api"
        mock_settings.security.jwt_audience = "faultmaven-app"
        mock_settings.security.token_revocation_prefix = "revoked:token:"
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        mock_get_settings_middleware.return_value = mock_settings

        yield TestClient(app)

    # Cleanup
    app.dependency_overrides.clear()


# ============================================================
# Tests
# ============================================================


class TestAgentExecuteWithJWTAuth:
    """Test agent execute endpoint with real JWT authentication."""

    def test_agent_execute_with_valid_hs256_token_streaming(
        self, client, jwt_token, mock_session
    ):
        """Agent execute endpoint accepts valid HS256 JWT token in streaming mode."""
        headers = {"Authorization": f"Bearer {jwt_token}"}

        response = client.post(
            f"/api/v1/cases/{mock_session.case_id}/sessions/{mock_session.session_id}/execute",
            json={
                "user_prompt": "What is the issue?",
                "stream": True,
            },
            headers=headers,
        )

        # Should succeed with 200 OK
        assert response.status_code == status.HTTP_200_OK
        assert "text/event-stream" in response.headers["content-type"]

        # Verify we got SSE events
        content = response.text
        assert "event: started" in content or "data:" in content

    def test_agent_execute_with_valid_hs256_token_non_streaming(
        self, client, jwt_token, mock_session
    ):
        """Agent execute endpoint accepts valid HS256 JWT token in non-streaming mode."""
        headers = {"Authorization": f"Bearer {jwt_token}"}

        response = client.post(
            f"/api/v1/cases/{mock_session.case_id}/sessions/{mock_session.session_id}/execute",
            json={
                "user_prompt": "What is the issue?",
                "stream": False,
            },
            headers=headers,
        )

        # Should succeed with 200 OK
        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"] == "application/json"

        # Verify response structure
        data = response.json()
        assert "execution_id" in data
        assert "status" in data

    def test_agent_execute_rejects_invalid_token(self, client, mock_session):
        """Agent execute endpoint rejects invalid JWT token."""
        headers = {"Authorization": "Bearer invalid.token.here"}

        response = client.post(
            f"/api/v1/cases/{mock_session.case_id}/sessions/{mock_session.session_id}/execute",
            json={
                "user_prompt": "What is the issue?",
                "stream": False,
            },
            headers=headers,
        )

        # Should fail with 401 Unauthorized
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_agent_execute_rejects_missing_token(self, client, mock_session):
        """Agent execute endpoint rejects requests without token."""
        response = client.post(
            f"/api/v1/cases/{mock_session.case_id}/sessions/{mock_session.session_id}/execute",
            json={
                "user_prompt": "What is the issue?",
                "stream": False,
            },
        )

        # Should fail with 401 Unauthorized
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_token_algorithm_is_hs256_in_local_mode(self, auth_service, jwt_token):
        """Verify token is generated with HS256 algorithm in local mode."""
        import jwt as pyjwt

        # Decode token header without verification
        header = pyjwt.get_unverified_header(jwt_token)

        # Should be HS256, not RS256
        assert header["alg"] == "HS256"

    def test_auth_service_uses_hs256_in_local_mode(self, auth_service):
        """AuthService._algorithm property returns HS256 in local mode."""
        assert auth_service._algorithm == "HS256"
