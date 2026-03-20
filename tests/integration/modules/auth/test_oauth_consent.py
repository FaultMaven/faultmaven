"""Integration tests for OAuth 2.0 consent flow.

Tests the two-step authorization flow with user consent:
1. GET /auth/oauth/authorize - Returns consent request details
2. POST /auth/oauth/authorize - Processes user approval/denial

Tests both production mode (consent required) and dev mode (auto-approve).
"""

import os
import sys
from unittest.mock import AsyncMock

# Set environment variables FIRST - before ANY imports
os.environ["SKIP_SERVICE_CHECKS"] = "true"
os.environ["OAUTH_ENABLED"] = "true"

# Force reimport of app with OAuth enabled
if "faultmaven.main" in sys.modules:
    del sys.modules["faultmaven.main"]
if "faultmaven.config.settings" in sys.modules:
    del sys.modules["faultmaven.config.settings"]

import pytest
from fastapi import Request, status
from httpx import ASGITransport, AsyncClient

from faultmaven.main import app
from faultmaven.modules.auth.contracts import OAuthTokenDTO
from faultmaven.modules.auth.domain.models.auth import DevUser


@pytest.fixture
def mock_oauth_service():
    """Mock OAuth service for testing HTTP layer."""
    service = AsyncMock()

    # Mock successful authorization code generation
    service.create_authorization_code.return_value = "test_authorization_code_123"

    return service


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    from datetime import datetime

    return DevUser(
        user_id="test_user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.utcnow(),
    )


@pytest.fixture
async def client_with_mocked_oauth_and_auth(mock_oauth_service, mock_user):
    """HTTP client with mocked OAuth service and authentication."""
    # Reset rate limiter before each test to prevent interference
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.api.oauth import get_oauth_service

    # Override OAuth service dependency
    async def mock_get_oauth_service(request: Request):
        return mock_oauth_service

    # Override authentication dependency
    async def mock_require_authentication(request: Request = None):
        return mock_user

    app.dependency_overrides[get_oauth_service] = mock_get_oauth_service
    app.dependency_overrides[require_authentication] = mock_require_authentication

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        # Clean up dependency overrides
        app.dependency_overrides.clear()


class TestConsentFlowEnabled:
    """Test GET /authorize with consent required (production mode)."""

    @pytest.mark.asyncio
    async def test_get_authorize_returns_consent_request(
        self, client_with_mocked_oauth_and_auth, mock_oauth_service, monkeypatch
    ):
        """Test GET /authorize returns consent details when consent is required.

        Verifies:
        - Endpoint returns AuthorizationConsentRequest (not AuthorizationResponse)
        - Response contains client info, scopes, and user info for consent UI
        - Authorization code is NOT generated yet
        """
        # Enable consent requirement
        monkeypatch.setenv("OAUTH_REQUIRE_CONSENT", "true")

        # Force settings reload
        if "faultmaven.config.settings" in sys.modules:
            del sys.modules["faultmaven.config.settings"]

        response = await client_with_mocked_oauth_and_auth.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "state": "random_state_123",
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
                "scope": "openid profile email",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify response structure (consent request, NOT authorization code)
        data = response.json()
        assert "client_id" in data
        assert "client_name" in data
        assert "redirect_uri" in data
        assert "scope" in data
        assert "state" in data
        assert "user_id" in data
        assert "username" in data

        # Verify consent request details
        assert data["client_id"] == "faultmaven-copilot"
        assert data["client_name"] == "FaultMaven Copilot Browser Extension"
        assert data["state"] == "random_state_123"
        assert data["user_id"] == "test_user_123"
        assert data["username"] == "testuser"

        # Verify authorization code is NOT generated yet
        assert "code" not in data
        mock_oauth_service.create_authorization_code.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_authorize_approved(
        self, client_with_mocked_oauth_and_auth, mock_oauth_service
    ):
        """Test POST /authorize with user approval generates authorization code.

        Verifies:
        - User approval triggers authorization code generation
        - Response contains authorization code and state
        - Service layer is called with correct parameters
        """
        response = await client_with_mocked_oauth_and_auth.post(
            "/api/v1/auth/oauth/authorize",
            json={
                "approved": True,
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "scope": "openid profile email",
                "state": "random_state_123",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify response contains authorization code
        data = response.json()
        assert data["code"] == "test_authorization_code_123"
        assert data["state"] == "random_state_123"

        # Verify service layer was called correctly
        mock_oauth_service.create_authorization_code.assert_called_once()
        call_args = mock_oauth_service.create_authorization_code.call_args
        assert call_args[0][0] == "test_user_123"  # user_id
        assert call_args[0][1].client_id == "faultmaven-copilot"
        assert call_args[0][1].code_challenge == "test_challenge"

    @pytest.mark.asyncio
    async def test_post_authorize_denied(
        self, client_with_mocked_oauth_and_auth, mock_oauth_service
    ):
        """Test POST /authorize with user denial returns error.

        Verifies:
        - User denial returns 400 error
        - Authorization code is NOT generated
        - Error message indicates user denial
        """
        response = await client_with_mocked_oauth_and_auth.post(
            "/api/v1/auth/oauth/authorize",
            json={
                "approved": False,
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "scope": "openid profile email",
                "state": "random_state_123",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_400_BAD_REQUEST

        # Verify error message
        data = response.json()
        assert "denied" in data["detail"].lower()

        # Verify authorization code was NOT generated
        mock_oauth_service.create_authorization_code.assert_not_called()


class TestConsentFlowDisabled:
    """Test GET /authorize with auto-approve (dev/test mode)."""

    @pytest.mark.asyncio
    async def test_get_authorize_auto_approves(
        self, client_with_mocked_oauth_and_auth, mock_oauth_service, monkeypatch
    ):
        """Test GET /authorize auto-approves when consent is disabled.

        Verifies:
        - Endpoint returns AuthorizationResponse directly (not consent request)
        - Authorization code is generated immediately
        - No POST step required
        """
        # Disable consent requirement (auto-approve mode)
        monkeypatch.setenv("OAUTH_REQUIRE_CONSENT", "false")

        # Force settings reload
        if "faultmaven.config.settings" in sys.modules:
            del sys.modules["faultmaven.config.settings"]

        response = await client_with_mocked_oauth_and_auth.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "state": "random_state_123",
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
                "scope": "openid profile email",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify response contains authorization code (auto-approved)
        data = response.json()
        assert data["code"] == "test_authorization_code_123"
        assert data["state"] == "random_state_123"

        # Verify consent fields are NOT present (this is authorization response, not consent request)
        assert "client_name" not in data
        assert "user_id" not in data

        # Verify authorization code was generated
        mock_oauth_service.create_authorization_code.assert_called_once()


class TestAuthorizationValidation:
    """Test authorization endpoint validation."""

    @pytest.mark.asyncio
    async def test_authorize_invalid_response_type(
        self, client_with_mocked_oauth_and_auth
    ):
        """Test authorization endpoint rejects invalid response_type."""
        response = await client_with_mocked_oauth_and_auth.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "token",  # Invalid: only 'code' supported
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "state": "random_state_123",
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
            },
        )

        # Verify error response
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "response_type" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_authorize_missing_required_parameter(
        self, client_with_mocked_oauth_and_auth
    ):
        """Test authorization endpoint requires all parameters."""
        response = await client_with_mocked_oauth_and_auth.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                # Missing redirect_uri, state, code_challenge
            },
        )

        # Verify validation error
        assert response.status_code == 422
