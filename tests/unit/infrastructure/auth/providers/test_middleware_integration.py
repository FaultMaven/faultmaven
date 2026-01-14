"""Unit tests for AuthProvider Middleware Integration

Tests that get_current_user middleware correctly uses AuthProvider.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from faultmaven.api.middleware.auth import get_auth_provider, get_current_user
from faultmaven.infrastructure.auth.providers.auth0 import Auth0Provider
from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.infrastructure.auth.providers.no_auth import NoAuthProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser


@pytest.fixture
def mock_request():
    """Create mock FastAPI request."""
    from types import SimpleNamespace
    
    request = MagicMock(spec=Request)
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace()
    # app.extra can be accessed via getattr (as used in actual code)
    request.app.extra = SimpleNamespace()
    return request


@pytest.fixture
def mock_container():
    """Create mock DI container."""
    container = MagicMock()
    return container


@pytest.fixture
def sample_user():
    """Create sample authenticated user."""
    return AuthenticatedUser(
        user_id="user-123",
        organization_id="org-456",
        email="test@example.com",
        roles=["admin"],
        permissions=["cases:read", "cases:write"],
        token_jti="token-789"
    )


class TestGetAuthProvider:
    """Tests for get_auth_provider dependency."""

    @pytest.mark.asyncio
    async def test_get_auth_provider_from_container(self, mock_request, mock_container):
        """Test that provider is retrieved from app.state (Composition Root)."""
        no_auth_provider = NoAuthProvider()
        # Set up app.state.auth_provider (Composition Root pattern)
        mock_request.app.state = MagicMock()
        mock_request.app.state.auth_provider = no_auth_provider
        
        provider = await get_auth_provider(mock_request)
        
        # Verify it's the same instance from app.state
        assert provider is no_auth_provider
        assert isinstance(provider, NoAuthProvider)

    @pytest.mark.asyncio
    async def test_get_auth_provider_fallback_to_no_auth(self, mock_request):
        """Test that fallback to NoAuthProvider when app.state.auth_provider unavailable."""
        from types import SimpleNamespace
        # No auth_provider in app.state - getattr will return None
        mock_request.app.state = SimpleNamespace()
        # Don't set auth_provider attribute, so getattr returns None
        
        provider = await get_auth_provider(mock_request)
        
        assert isinstance(provider, NoAuthProvider)

    @pytest.mark.asyncio
    async def test_get_auth_provider_fallback_when_container_has_none(self, mock_request, mock_container):
        """Test that fallback when app.state.auth_provider is None."""
        # Set app.state.auth_provider to None explicitly
        mock_request.app.state = MagicMock()
        mock_request.app.state.auth_provider = None
        
        provider = await get_auth_provider(mock_request)
        
        assert isinstance(provider, NoAuthProvider)


class TestGetCurrentUserWithAuthProvider:
    """Tests for get_current_user using AuthProvider."""

    @pytest.mark.asyncio
    async def test_get_current_user_no_auth_mode(self, mock_request, sample_user):
        """Test that no-auth mode returns user without token."""
        no_auth_provider = NoAuthProvider()
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=no_auth_provider):
            user = await get_current_user(
                authorization=None,
                credentials=None,
                request=mock_request,
                auth_provider=no_auth_provider
            )
            
            assert user is not None
            assert user.user_id == NoAuthProvider.DEFAULT_USER_ID

    @pytest.mark.asyncio
    async def test_get_current_user_auth_enabled_no_token_raises_error(self, mock_request):
        """Test that auth-enabled mode requires token."""
        auth0_provider = Auth0Provider(
            domain="test-tenant.auth0.com",
            audience="test-audience"
        )
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=auth0_provider):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    authorization=None,
                    credentials=None,
                    request=mock_request,
                    auth_provider=auth0_provider
                )
            
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_auth_enabled_valid_token(self, mock_request, sample_user):
        """Test that auth-enabled mode validates token."""
        auth0_provider = Auth0Provider(
            domain="test-tenant.auth0.com",
            audience="test-audience"
        )
        
        # Mock validate_token to return user
        auth0_provider.validate_token = AsyncMock(return_value=sample_user)
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=auth0_provider):
            user = await get_current_user(
                authorization="Bearer valid-token",
                credentials=None,
                request=mock_request,
                auth_provider=auth0_provider
            )
            
            assert user == sample_user
            auth0_provider.validate_token.assert_called_once_with("valid-token")

    @pytest.mark.asyncio
    async def test_get_current_user_auth_enabled_invalid_token_raises_error(self, mock_request):
        """Test that invalid token raises 401 error."""
        auth0_provider = Auth0Provider(
            domain="test-tenant.auth0.com",
            audience="test-audience"
        )
        
        # Mock validate_token to return None (invalid)
        auth0_provider.validate_token = AsyncMock(return_value=None)
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=auth0_provider):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    authorization="Bearer invalid-token",
                    credentials=None,
                    request=mock_request,
                    auth_provider=auth0_provider
                )
            
            assert exc_info.value.status_code == 401
            assert "Invalid or expired token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_current_user_extracts_token_from_header(self, mock_request, sample_user):
        """Test that token is extracted from Authorization header."""
        auth0_provider = Auth0Provider(
            domain="test-tenant.auth0.com",
            audience="test-audience"
        )
        
        auth0_provider.validate_token = AsyncMock(return_value=sample_user)
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=auth0_provider):
            user = await get_current_user(
                authorization="Bearer my-token-123",
                credentials=None,
                request=mock_request,
                auth_provider=auth0_provider
            )
            
            auth0_provider.validate_token.assert_called_once_with("my-token-123")

    @pytest.mark.asyncio
    async def test_get_current_user_handles_exception(self, mock_request):
        """Test that exceptions during validation are handled."""
        auth0_provider = Auth0Provider(
            domain="test-tenant.auth0.com",
            audience="test-audience"
        )
        
        # Mock validate_token to raise exception
        auth0_provider.validate_token = AsyncMock(side_effect=Exception("Unexpected error"))
        
        with patch('faultmaven.api.middleware.auth.get_auth_provider', return_value=auth0_provider):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(
                    authorization="Bearer token",
                    credentials=None,
                    request=mock_request,
                    auth_provider=auth0_provider
                )
            
            assert exc_info.value.status_code == 401
