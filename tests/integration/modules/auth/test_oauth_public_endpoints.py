"""Integration tests for OAuth 2.0 public endpoints.

Tests the HTTP wiring for public OAuth endpoints that do not require authentication:
- POST /auth/oauth/token (token exchange and refresh)
- POST /auth/oauth/revoke (token revocation)

These tests verify that:
1. Endpoints are publicly accessible (no 401/403 from global middleware)
2. FastAPI correctly serializes/deserializes requests and responses
3. Route registration and dependency injection work correctly

The OAuth service layer is mocked to isolate HTTP wiring from business logic
(business logic is thoroughly tested in unit tests).

Note: The GET /auth/oauth/authorize endpoint requires authentication and is
tested in E2E tests with the full Dashboard + Extension stack.
"""

import os
import sys
from unittest.mock import AsyncMock, Mock

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


@pytest.fixture
def mock_oauth_service():
    """Mock OAuth service for testing HTTP layer."""
    service = AsyncMock()

    # Mock successful token exchange
    service.exchange_code_for_token.return_value = OAuthTokenDTO(
        access_token="test_access_token_123",
        refresh_token="test_refresh_token_456",
        token_type="Bearer",
        expires_in=3600,
        refresh_expires_in=604800,
        user_id="test_user_123",
        username="testuser",
    )

    # Mock successful token refresh
    service.refresh_access_token.return_value = OAuthTokenDTO(
        access_token="new_access_token_789",
        refresh_token="new_refresh_token_012",
        token_type="Bearer",
        expires_in=3600,
        refresh_expires_in=604800,
        user_id="test_user_123",
        username="testuser",
    )

    # Mock token revocation (returns None)
    service.revoke_token.return_value = None
    service.revoke_refresh_token.return_value = None

    return service


@pytest.fixture
async def client_with_mocked_oauth(mock_oauth_service):
    """HTTP client with mocked OAuth service dependency."""
    # Reset rate limiter before each test to prevent interference
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    # Import the dependency function used in the route (from oauth.py)
    from faultmaven.modules.auth.api.oauth import get_oauth_service

    # Override the OAuth service dependency
    # IMPORTANT: request parameter must be typed as Request for FastAPI to recognize it
    async def mock_get_oauth_service(request: Request):
        return mock_oauth_service

    app.dependency_overrides[get_oauth_service] = mock_get_oauth_service

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        # Clean up dependency overrides
        app.dependency_overrides.clear()


class TestTokenEndpoint:
    """Test POST /auth/oauth/token endpoint (public endpoint)."""

    @pytest.mark.asyncio
    async def test_token_exchange_success(
        self, client_with_mocked_oauth, mock_oauth_service
    ):
        """Test successful authorization code exchange for tokens.

        Verifies:
        - Endpoint is publicly accessible (no authentication required)
        - Request deserialization works correctly
        - Response serialization matches OAuth 2.0 spec
        - Service layer is called with correct parameters
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "valid_authorization_code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "code_verifier": "valid_pkce_verifier",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify response structure (OAuth 2.0 compliance)
        data = response.json()
        assert data["access_token"] == "test_access_token_123"
        assert data["refresh_token"] == "test_refresh_token_456"
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] == 3600
        assert data["refresh_expires_in"] == 604800
        assert data["user_id"] == "test_user_123"
        assert data["username"] == "testuser"

        # Verify service layer was called correctly
        mock_oauth_service.exchange_code_for_token.assert_called_once_with(
            code="valid_authorization_code",
            code_verifier="valid_pkce_verifier",
            redirect_uri="chrome-extension://test123/callback",
        )

    @pytest.mark.asyncio
    async def test_token_refresh_success(
        self, client_with_mocked_oauth, mock_oauth_service
    ):
        """Test successful token refresh.

        Verifies:
        - Refresh token grant type works correctly
        - Token rotation response is properly serialized
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": "valid_refresh_token",
                "client_id": "faultmaven-copilot",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify response contains new tokens
        data = response.json()
        assert data["access_token"] == "new_access_token_789"
        assert data["refresh_token"] == "new_refresh_token_012"
        assert data["token_type"] == "Bearer"

        # Verify service layer was called correctly
        mock_oauth_service.refresh_access_token.assert_called_once_with(
            refresh_token="valid_refresh_token",
            client_id="faultmaven-copilot",
        )

    @pytest.mark.asyncio
    async def test_token_endpoint_missing_required_field(
        self, client_with_mocked_oauth
    ):
        """Test token endpoint with missing required field.

        Verifies FastAPI request validation works correctly.
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                # Missing 'code' field
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test123/callback",
                "code_verifier": "valid_pkce_verifier",
            },
        )

        # Verify validation error
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "code" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_token_endpoint_invalid_grant_type(self, client_with_mocked_oauth):
        """Test token endpoint with unsupported grant type.

        Verifies proper error handling for invalid grant types.
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "client_credentials",  # Not supported
                "client_id": "faultmaven-copilot",
            },
        )

        # Verify error response
        assert response.status_code in [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ]


class TestRevokeEndpoint:
    """Test POST /auth/oauth/revoke endpoint (public endpoint)."""

    @pytest.mark.asyncio
    async def test_revoke_access_token_success(
        self, client_with_mocked_oauth, mock_oauth_service
    ):
        """Test successful access token revocation.

        Verifies:
        - Endpoint is publicly accessible
        - Revocation succeeds with 200 OK (per RFC 7009)
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": "access_token_to_revoke",
                "token_type_hint": "access_token",
                "client_id": "faultmaven-copilot",
            },
        )

        # Verify HTTP status (RFC 7009: revocation returns 200 OK)
        assert response.status_code == status.HTTP_200_OK

        # Verify service layer was called
        mock_oauth_service.revoke_token.assert_called_once_with(
            "access_token_to_revoke"
        )

    @pytest.mark.asyncio
    async def test_revoke_refresh_token_success(
        self, client_with_mocked_oauth, mock_oauth_service
    ):
        """Test successful refresh token revocation."""
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": "refresh_token_to_revoke",
                "token_type_hint": "refresh_token",
                "client_id": "faultmaven-copilot",
            },
        )

        # Verify HTTP status
        assert response.status_code == status.HTTP_200_OK

        # Verify service layer was called
        mock_oauth_service.revoke_refresh_token.assert_called_once_with(
            "refresh_token_to_revoke"
        )

    @pytest.mark.asyncio
    async def test_revoke_without_hint(
        self, client_with_mocked_oauth, mock_oauth_service
    ):
        """Test revocation without token_type_hint.

        Per RFC 7009, token_type_hint is optional. Server should attempt
        to revoke the token regardless.
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": "some_token",
                "client_id": "faultmaven-copilot",
            },
        )

        # Verify HTTP status (should succeed even without hint)
        assert response.status_code == status.HTTP_200_OK


class TestGlobalMiddlewareCompatibility:
    """Test that OAuth public endpoints work with global middleware.

    Critical regression tests - ensures global middleware (CORS, logging,
    protection, etc.) doesn't accidentally block public OAuth endpoints.
    """

    @pytest.mark.asyncio
    async def test_token_endpoint_no_authentication_required(
        self, client_with_mocked_oauth
    ):
        """Verify /token endpoint does NOT require authentication.

        This is a CRITICAL test - if global authentication middleware
        accidentally protects this endpoint, the Extension cannot get tokens.

        Regression test for: AttributeError: 'Request' object has no attribute 'user_id'
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "test_code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://test/callback",
                "code_verifier": "test_verifier",
            },
        )

        # Critical: Must NOT return 401 Unauthorized
        assert response.status_code != status.HTTP_401_UNAUTHORIZED
        assert response.status_code != status.HTTP_403_FORBIDDEN

        # Should return 200 OK (or 400 if validation fails, but NOT auth errors)
        assert response.status_code in [
            status.HTTP_200_OK,
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ]

    @pytest.mark.asyncio
    async def test_revoke_endpoint_no_authentication_required(
        self, client_with_mocked_oauth
    ):
        """Verify /revoke endpoint does NOT require authentication.

        Per OAuth 2.0 spec, token revocation should be available to clients
        without requiring separate authentication (the token itself is proof).
        """
        response = await client_with_mocked_oauth.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": "test_token",
                "client_id": "faultmaven-copilot",
            },
        )

        # Critical: Must NOT return 401/403
        assert response.status_code != status.HTTP_401_UNAUTHORIZED
        assert response.status_code != status.HTTP_403_FORBIDDEN

        # Should return 200 OK
        assert response.status_code == status.HTTP_200_OK
