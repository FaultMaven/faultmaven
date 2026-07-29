"""Integration tests for OAuth 2.0 Authorization Code Flow with PKCE.

Tests the complete OAuth flow end-to-end:
1. Authorization code generation (Dashboard)
2. Code exchange for tokens (Browser Extension)
3. Token refresh
4. Token revocation

These tests use real implementations with in-memory storage to verify
that all components work together correctly.
"""

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import Request, status
from httpx import ASGITransport, AsyncClient

# Import app after setting OAuth-specific environment variables
# IMPORTANT: These must be set before any faultmaven imports
os.environ["SKIP_SERVICE_CHECKS"] = "true"
os.environ["OAUTH_ENABLED"] = "true"
os.environ["OAUTH_REQUIRE_CONSENT"] = "false"  # Auto-approve for E2E flow tests

import sys

from faultmaven.config.settings import reset_settings

# Force the app below to be built with OAuth enabled.
#
# Clear the settings *singleton*; never drop faultmaven.config.settings from
# sys.modules. Doing that leaves a second module object in the process, each
# with its own `_settings_instance`, so a `reset_settings` captured by an
# earlier import clears a singleton nothing reads while the freshly imported
# module keeps handing production code a stale cached settings object.
reset_settings()
if "faultmaven.main" in sys.modules:
    del sys.modules["faultmaven.main"]

from faultmaven.main import app


@pytest.fixture
def pkce_pair():
    """Generate valid PKCE code_verifier and code_challenge pair."""
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    )
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )
    return code_verifier, code_challenge


@pytest.fixture
def test_user():
    """Create a test user for OAuth flow."""
    from faultmaven.modules.auth.domain.models.auth import DevUser

    user = DevUser(
        user_id="test_user_123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.utcnow(),
    )
    return user


@pytest.fixture
async def authenticated_client(test_user):
    """Create an authenticated HTTP client with real OAuth service."""
    # Reset rate limiter
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.persistence.user_repository import (
        InMemoryUserRepository,
    )
    from faultmaven.modules.auth.api.oauth import get_oauth_service
    from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
    from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
        InMemoryOAuthCodeRepository,
    )

    settings = get_settings()

    # Create real repositories
    code_repository = InMemoryOAuthCodeRepository()
    user_repository = InMemoryUserRepository()

    # Mock token generator
    mock_token_generator = AsyncMock()
    mock_token_generator.generate_access_token = AsyncMock(
        return_value="test_access_token_123"
    )
    mock_token_generator.generate_refresh_token = AsyncMock(
        return_value="test_refresh_token_456"
    )
    mock_token_generator.validate_refresh_token = AsyncMock(
        return_value={
            "sub": test_user.user_id,
            "username": test_user.username,
            "type": "refresh",
        }
    )

    # Add test user to repository (convert DevUser to User)
    from faultmaven.infrastructure.persistence.user_repository import User

    repo_user = User(
        user_id=test_user.user_id,
        username=test_user.username,
        email=test_user.email,
        display_name=test_user.display_name,
        created_at=test_user.created_at,
        updated_at=test_user.created_at,
    )
    await user_repository.save(repo_user)

    # Create OAuth service
    oauth_service = OAuthServiceImpl(
        code_repository=code_repository,
        user_repository=user_repository,
        token_generator=mock_token_generator,
        settings=settings.auth,
    )

    # Mock authentication
    async def mock_require_authentication(user=None):
        return test_user

    async def mock_get_oauth_service(request: Request):
        return oauth_service

    # Override dependencies
    app.dependency_overrides[require_authentication] = mock_require_authentication
    app.dependency_overrides[get_oauth_service] = mock_get_oauth_service

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestAuthorizationCodeGeneration:
    """Test authorization code generation endpoint."""

    @pytest.mark.asyncio
    async def test_generate_authorization_code_success(
        self, authenticated_client, pkce_pair, test_user
    ):
        """Test successful authorization code generation."""
        code_verifier, code_challenge = pkce_pair

        response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email",
            },
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "code" in data
        assert data["state"] == "random_state_123"
        assert len(data["code"]) == 43

    @pytest.mark.asyncio
    async def test_generate_authorization_code_invalid_client(
        self, authenticated_client, pkce_pair
    ):
        """Test authorization code generation with invalid client."""
        code_verifier, code_challenge = pkce_pair

        response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "invalid-client",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "invalid" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_generate_authorization_code_unauthenticated(self, pkce_pair):
        """Test that unauthenticated requests are rejected."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            code_verifier, code_challenge = pkce_pair

            response = await client.get(
                "/api/v1/auth/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "faultmaven-copilot",
                    "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                    "state": "random_state_123",
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                },
            )

            assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestCompleteOAuthFlow:
    """Test complete end-to-end OAuth flow."""

    @pytest.mark.asyncio
    async def test_complete_oauth_flow_success(
        self, authenticated_client, pkce_pair, test_user
    ):
        """Test complete OAuth flow."""
        code_verifier, code_challenge = pkce_pair

        # Get authorization code
        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        assert auth_response.status_code == status.HTTP_200_OK
        code = auth_response.json()["code"]

        # Exchange code for tokens
        token_response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        assert token_response.status_code == status.HTTP_200_OK
        token_data = token_response.json()
        assert "access_token" in token_data
        assert "refresh_token" in token_data
        assert token_data["user_id"] == test_user.user_id

    @pytest.mark.asyncio
    async def test_code_replay_attack_prevention(self, authenticated_client, pkce_pair):
        """Test that authorization codes can only be used once."""
        code_verifier, code_challenge = pkce_pair

        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        code = auth_response.json()["code"]

        # First exchange should succeed
        first_response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        assert first_response.status_code == status.HTTP_200_OK

        # Second exchange should fail
        second_response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        assert second_response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_pkce_verification_prevents_code_interception(
        self, authenticated_client, pkce_pair
    ):
        """Test PKCE verification."""
        code_verifier, code_challenge = pkce_pair

        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        code = auth_response.json()["code"]

        # Wrong verifier
        wrong_verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode("utf-8")
            .rstrip("=")
        )

        response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": wrong_verifier,
            },
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestTokenRevocation:
    """Test token revocation."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(self, authenticated_client, pkce_pair):
        """Test access token revocation."""
        code_verifier, code_challenge = pkce_pair

        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        code = auth_response.json()["code"]

        token_response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        access_token = token_response.json()["access_token"]

        revoke_response = await authenticated_client.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": access_token,
                "token_type_hint": "access_token",
                "client_id": "faultmaven-copilot",
            },
        )

        assert revoke_response.status_code == status.HTTP_200_OK

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self, authenticated_client, pkce_pair):
        """Test refresh token revocation."""
        code_verifier, code_challenge = pkce_pair

        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        code = auth_response.json()["code"]

        token_response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        refresh_token = token_response.json()["refresh_token"]

        revoke_response = await authenticated_client.post(
            "/api/v1/auth/oauth/revoke",
            json={
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": "faultmaven-copilot",
            },
        )

        assert revoke_response.status_code == status.HTTP_200_OK


class TestOAuthErrorHandling:
    """Test OAuth error handling."""

    @pytest.mark.asyncio
    async def test_expired_authorization_code(self, authenticated_client, pkce_pair):
        """Test invalid authorization code."""
        code_verifier, code_challenge = pkce_pair

        response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "invalid_code_123",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "code_verifier": code_verifier,
            },
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_redirect_uri_mismatch(self, authenticated_client, pkce_pair):
        """Test redirect_uri mismatch."""
        code_verifier, code_challenge = pkce_pair

        auth_response = await authenticated_client.get(
            "/api/v1/auth/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef/callback.html",
                "state": "random_state_123",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

        code = auth_response.json()["code"]

        response = await authenticated_client.post(
            "/api/v1/auth/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "faultmaven-copilot",
                "redirect_uri": "chrome-extension://different1abcdefghijklmnopqrstuv/callback.html",
                "code_verifier": code_verifier,
            },
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
