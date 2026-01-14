"""Unit tests for NoAuthProvider (Local Deployment)

Tests the no-authentication provider that always returns an authenticated user
without token validation. Used for local/single-user deployment.
"""

import pytest

from faultmaven.infrastructure.auth.providers.no_auth import NoAuthProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser


@pytest.fixture
def no_auth_provider():
    """Create NoAuthProvider instance."""
    return NoAuthProvider()


@pytest.fixture
def custom_no_auth_provider():
    """Create NoAuthProvider with custom user ID."""
    return NoAuthProvider(default_user_id="custom-user-123")


class TestNoAuthProvider:
    """Tests for NoAuthProvider."""

    @pytest.mark.asyncio
    async def test_validate_token_returns_user(self, no_auth_provider):
        """Test that validate_token always returns authenticated user."""
        user = await no_auth_provider.validate_token("")
        
        assert user is not None
        assert isinstance(user, AuthenticatedUser)
        assert user.user_id == NoAuthProvider.DEFAULT_USER_ID
        assert user.email == NoAuthProvider.DEFAULT_EMAIL
        assert user.organization_id == NoAuthProvider.DEFAULT_ORG_ID
        assert user.roles == NoAuthProvider.DEFAULT_ROLES
        assert user.permissions == NoAuthProvider.DEFAULT_PERMISSIONS

    @pytest.mark.asyncio
    async def test_validate_token_ignores_token(self, no_auth_provider):
        """Test that validate_token ignores the token parameter."""
        # Should return same user regardless of token
        user1 = await no_auth_provider.validate_token("")
        user2 = await no_auth_provider.validate_token("fake-token")
        user3 = await no_auth_provider.validate_token("another-token")
        
        assert user1.user_id == user2.user_id == user3.user_id
        assert user1.email == user2.email == user3.email

    @pytest.mark.asyncio
    async def test_validate_token_custom_user_id(self, custom_no_auth_provider):
        """Test that custom user ID is used when provided."""
        user = await custom_no_auth_provider.validate_token("")
        
        assert user.user_id == "custom-user-123"
        assert user.email == NoAuthProvider.DEFAULT_EMAIL
        assert user.organization_id == NoAuthProvider.DEFAULT_ORG_ID

    def test_is_enabled_returns_false(self, no_auth_provider):
        """Test that is_enabled returns False (auth disabled)."""
        assert no_auth_provider.is_enabled() is False

    def test_default_constants(self):
        """Test that default constants are correct."""
        assert NoAuthProvider.DEFAULT_USER_ID == "local-user"
        assert NoAuthProvider.DEFAULT_EMAIL == "local@faultmaven.local"
        assert NoAuthProvider.DEFAULT_ORG_ID == "local-org"
        assert NoAuthProvider.DEFAULT_ROLES == ["admin"]
        assert NoAuthProvider.DEFAULT_PERMISSIONS == ["*"]

    @pytest.mark.asyncio
    async def test_user_has_admin_role(self, no_auth_provider):
        """Test that returned user has admin role."""
        user = await no_auth_provider.validate_token("")
        
        assert "admin" in user.roles
        assert user.has_role("admin")
        assert user.is_admin()

    @pytest.mark.asyncio
    async def test_user_has_all_permissions(self, no_auth_provider):
        """Test that returned user has all permissions."""
        user = await no_auth_provider.validate_token("")
        
        assert "*" in user.permissions
        assert user.has_permission("cases:read")
        assert user.has_permission("cases:write")
        assert user.has_permission("sessions:execute")
