"""Unit tests for Authentication Middleware (TASK-017)

Tests get_current_user, require_permission, and require_role dependencies.

Test Categories:
1. get_current_user Tests - Token extraction and verification
2. require_permission Tests - Permission checking
3. require_role Tests - Role checking

Coverage Target: 90%+
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from faultmaven.api.middleware.auth import (
    _extract_token,
    get_auth_service,
    get_current_user,
    get_current_user_optional,
    require_admin,
    require_all_permissions,
    require_any_permission,
    require_any_role,
    require_permission,
    require_role,
    set_auth_service,
)
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    TokenRevocationError,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_auth_service():
    """Create mock AuthService."""
    service = MagicMock()
    service.extract_user_from_token_with_revocation_check = AsyncMock()
    return service


@pytest.fixture
def sample_user() -> AuthenticatedUser:
    """Create sample authenticated user."""
    return AuthenticatedUser(
        user_id="user-123",
        organization_id="org-456",
        email="test@example.com",
        roles=["admin"],
        permissions=[
            "cases:read",
            "cases:write",
            "cases:delete",
            "sessions:read",
            "sessions:execute",
        ],
        token_jti="token-789",
    )


@pytest.fixture
def member_user() -> AuthenticatedUser:
    """Create sample member user (limited permissions)."""
    return AuthenticatedUser(
        user_id="user-456",
        organization_id="org-456",
        email="member@example.com",
        roles=["member"],
        permissions=[
            "cases:read",
            "cases:write",
            "sessions:read",
        ],
        token_jti="token-abc",
    )


@pytest.fixture
def viewer_user() -> AuthenticatedUser:
    """Create sample viewer user (read-only)."""
    return AuthenticatedUser(
        user_id="user-789",
        organization_id="org-456",
        email="viewer@example.com",
        roles=["viewer"],
        permissions=[
            "cases:read",
            "sessions:read",
        ],
        token_jti="token-def",
    )


# ============================================================
# Token Extraction Tests
# ============================================================


class TestTokenExtraction:
    """Tests for _extract_token helper."""

    def test_extract_from_bearer_header(self):
        """Extracts token from Bearer header."""
        token = _extract_token("Bearer abc123", None)
        assert token == "abc123"

    def test_extract_from_credentials(self):
        """Extracts token from HTTPBearer credentials."""
        credentials = MagicMock()
        credentials.credentials = "xyz789"

        token = _extract_token(None, credentials)
        assert token == "xyz789"

    def test_credentials_takes_precedence(self):
        """HTTPBearer credentials take precedence over raw header."""
        credentials = MagicMock()
        credentials.credentials = "from-credentials"

        token = _extract_token("Bearer from-header", credentials)
        assert token == "from-credentials"

    def test_returns_none_when_no_auth(self):
        """Returns None when no authentication provided."""
        token = _extract_token(None, None)
        assert token is None

    def test_returns_none_for_invalid_header_format(self):
        """Returns None for non-Bearer header format."""
        token = _extract_token("Basic abc123", None)
        assert token is None

    def test_returns_none_for_empty_bearer_token(self):
        """Returns None for empty Bearer token."""
        token = _extract_token("Bearer ", None)
        assert token is None

    def test_handles_bearer_with_extra_spaces(self):
        """Handles Bearer token with extra spaces."""
        token = _extract_token("Bearer   abc123  ", None)
        assert token == "abc123"


# ============================================================
# get_current_user Tests
# ============================================================


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_token(self, mock_auth_service, sample_user):
        """Returns AuthenticatedUser for valid token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.return_value = (
            sample_user
        )

        set_auth_service(mock_auth_service)

        user = await get_current_user(
            authorization="Bearer valid-token",
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user == sample_user
        assert user.user_id == "user-123"
        assert user.organization_id == "org-456"

    @pytest.mark.asyncio
    async def test_raises_401_on_missing_token(self, mock_auth_service):
        """Raises 401 when no token provided."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                authorization=None,
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_on_invalid_header_format(self, mock_auth_service):
        """Raises 401 for non-Bearer header."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                authorization="Basic abc123",
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_401_on_expired_token(self, mock_auth_service):
        """Raises 401 for expired token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Token has expired", error_code="TOKEN_EXPIRED")
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                authorization="Bearer expired-token",
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_raises_403_on_revoked_token(self, mock_auth_service):
        """Raises 403 for revoked token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            TokenRevocationError()
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                authorization="Bearer revoked-token",
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 403
        assert "revoked" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_raises_401_on_malformed_token(self, mock_auth_service):
        """Raises 401 for malformed token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Token decode error", error_code="DECODE_ERROR")
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(
                authorization="Bearer malformed-token",
                credentials=None,
                auth_service=mock_auth_service,
            )

        assert exc_info.value.status_code == 401


# ============================================================
# get_current_user_optional Tests
# ============================================================


class TestGetCurrentUserOptional:
    """Tests for get_current_user_optional dependency."""

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_token(self, mock_auth_service, sample_user):
        """Returns AuthenticatedUser for valid token."""
        mock_auth_service.extract_user_from_token_with_revocation_check.return_value = (
            sample_user
        )

        user = await get_current_user_optional(
            authorization="Bearer valid-token",
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user == sample_user

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_token(self, mock_auth_service):
        """Returns None when no token provided."""
        user = await get_current_user_optional(
            authorization=None,
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_token(self, mock_auth_service):
        """Returns None for invalid token (doesn't raise)."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            AuthenticationError("Invalid token", error_code="INVALID_TOKEN")
        )

        user = await get_current_user_optional(
            authorization="Bearer invalid-token",
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None

    @pytest.mark.asyncio
    async def test_returns_none_for_revoked_token(self, mock_auth_service):
        """Returns None for revoked token (doesn't raise)."""
        mock_auth_service.extract_user_from_token_with_revocation_check.side_effect = (
            TokenRevocationError()
        )

        user = await get_current_user_optional(
            authorization="Bearer revoked-token",
            credentials=None,
            auth_service=mock_auth_service,
        )

        assert user is None


# ============================================================
# require_permission Tests
# ============================================================


class TestRequirePermission:
    """Tests for require_permission dependency."""

    @pytest.mark.asyncio
    async def test_allows_user_with_permission(self, sample_user):
        """Allows user who has required permission."""
        checker = require_permission("cases:read")

        # The checker expects get_current_user to be called first
        # We simulate this by passing the user directly
        with patch(
            "faultmaven.api.middleware.auth.get_current_user",
            return_value=sample_user,
        ):
            # Create a dependency that returns sample_user
            async def mock_get_current_user():
                return sample_user

            # The actual checker function needs current_user injected
            result = await checker(current_user=sample_user)
            assert result == sample_user

    @pytest.mark.asyncio
    async def test_raises_403_when_missing_permission(self, member_user):
        """Raises 403 when user lacks permission."""
        checker = require_permission("cases:delete")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=member_user)

        assert exc_info.value.status_code == 403
        assert "cases:delete" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_admin_has_all_permissions(self, sample_user):
        """Admin user has all permissions."""
        # Note: This depends on how permissions are set
        # Our sample_user has cases:delete
        checker = require_permission("cases:delete")
        result = await checker(current_user=sample_user)
        assert result == sample_user


# ============================================================
# require_any_permission Tests
# ============================================================


class TestRequireAnyPermission:
    """Tests for require_any_permission dependency."""

    @pytest.mark.asyncio
    async def test_allows_user_with_any_matching_permission(self, member_user):
        """Allows user with any of the required permissions."""
        checker = require_any_permission("cases:delete", "cases:read")

        result = await checker(current_user=member_user)
        assert result == member_user  # Has cases:read

    @pytest.mark.asyncio
    async def test_raises_403_when_no_matching_permission(self, viewer_user):
        """Raises 403 when user has none of the required permissions."""
        checker = require_any_permission("cases:write", "cases:delete")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=viewer_user)

        assert exc_info.value.status_code == 403


# ============================================================
# require_all_permissions Tests
# ============================================================


class TestRequireAllPermissions:
    """Tests for require_all_permissions dependency."""

    @pytest.mark.asyncio
    async def test_allows_user_with_all_permissions(self, sample_user):
        """Allows user with all required permissions."""
        checker = require_all_permissions("cases:read", "cases:write")

        result = await checker(current_user=sample_user)
        assert result == sample_user

    @pytest.mark.asyncio
    async def test_raises_403_when_missing_any_permission(self, member_user):
        """Raises 403 when user is missing any required permission."""
        checker = require_all_permissions("cases:read", "cases:delete")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=member_user)

        assert exc_info.value.status_code == 403
        assert "cases:delete" in exc_info.value.detail


# ============================================================
# require_role Tests
# ============================================================


class TestRequireRole:
    """Tests for require_role dependency."""

    @pytest.mark.asyncio
    async def test_allows_user_with_role(self, sample_user):
        """Allows user with required role."""
        checker = require_role("admin")

        result = await checker(current_user=sample_user)
        assert result == sample_user

    @pytest.mark.asyncio
    async def test_raises_403_when_missing_role(self, member_user):
        """Raises 403 when user lacks role."""
        checker = require_role("admin")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=member_user)

        assert exc_info.value.status_code == 403
        assert "admin" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_member_role_check(self, member_user):
        """Member role check passes for member user."""
        checker = require_role("member")

        result = await checker(current_user=member_user)
        assert result == member_user


# ============================================================
# require_any_role Tests
# ============================================================


class TestRequireAnyRole:
    """Tests for require_any_role dependency."""

    @pytest.mark.asyncio
    async def test_allows_user_with_any_matching_role(self, member_user):
        """Allows user with any of the required roles."""
        checker = require_any_role("admin", "member")

        result = await checker(current_user=member_user)
        assert result == member_user

    @pytest.mark.asyncio
    async def test_raises_403_when_no_matching_role(self, viewer_user):
        """Raises 403 when user has none of the required roles."""
        checker = require_any_role("admin", "member")

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=viewer_user)

        assert exc_info.value.status_code == 403


# ============================================================
# require_admin Tests
# ============================================================


class TestRequireAdmin:
    """Tests for require_admin dependency."""

    @pytest.mark.asyncio
    async def test_allows_admin_user(self, sample_user):
        """Allows admin user."""
        result = await require_admin(current_user=sample_user)
        assert result == sample_user

    @pytest.mark.asyncio
    async def test_raises_403_for_non_admin(self, member_user):
        """Raises 403 for non-admin user."""
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(current_user=member_user)

        assert exc_info.value.status_code == 403
        assert "Administrator" in exc_info.value.detail


# ============================================================
# Auth Service Singleton Tests
# ============================================================


class TestAuthServiceSingleton:
    """Tests for auth service singleton management."""

    @pytest.mark.asyncio
    async def test_set_and_get_auth_service(self, mock_auth_service):
        """set_auth_service and get_auth_service work correctly."""
        set_auth_service(mock_auth_service)
        retrieved = await get_auth_service(request=None)
        assert retrieved == mock_auth_service

    @pytest.mark.asyncio
    async def test_get_creates_default_service(self):
        """get_auth_service creates default service if none set."""
        # Clear any existing service
        set_auth_service(None)

        service = await get_auth_service(request=None)
        assert service is not None

    def teardown_method(self, method):
        """Reset auth service after each test."""
        set_auth_service(None)


# ============================================================
# AuthenticatedUser Model Tests
# ============================================================


class TestAuthenticatedUserModel:
    """Tests for AuthenticatedUser model methods."""

    def test_has_permission(self, sample_user):
        """has_permission returns True for existing permission."""
        assert sample_user.has_permission("cases:read") is True
        assert sample_user.has_permission("nonexistent:permission") is False

    def test_has_role(self, sample_user):
        """has_role returns True for existing role."""
        assert sample_user.has_role("admin") is True
        assert sample_user.has_role("viewer") is False

    def test_is_admin(self, sample_user, member_user):
        """is_admin returns True only for admin role."""
        assert sample_user.is_admin() is True
        assert member_user.is_admin() is False

    def test_has_any_permission(self, member_user):
        """has_any_permission returns True if any permission matches."""
        assert member_user.has_any_permission(["cases:delete", "cases:read"]) is True
        assert member_user.has_any_permission(["nonexistent"]) is False

    def test_has_all_permissions(self, sample_user):
        """has_all_permissions returns True only if all match."""
        assert sample_user.has_all_permissions(["cases:read", "cases:write"]) is True
        assert sample_user.has_all_permissions(["cases:read", "nonexistent"]) is False

    def test_to_dict(self, sample_user):
        """to_dict returns correct dictionary."""
        d = sample_user.to_dict()

        assert d["user_id"] == "user-123"
        assert d["organization_id"] == "org-456"
        assert d["email"] == "test@example.com"
        assert d["roles"] == ["admin"]
        assert "cases:read" in d["permissions"]
        assert d["token_jti"] == "token-789"

    def test_from_jwt_claims(self):
        """from_jwt_claims creates AuthenticatedUser correctly."""
        claims = {
            "sub": "user-abc",
            "organization_id": "org-def",
            "email": "jwt@example.com",
            "roles": ["member"],
            "permissions": ["cases:read"],
            "jti": "claim-jti",
        }

        user = AuthenticatedUser.from_jwt_claims(claims)

        assert user.user_id == "user-abc"
        assert user.organization_id == "org-def"
        assert user.email == "jwt@example.com"
        assert user.roles == ["member"]
        assert user.permissions == ["cases:read"]
        assert user.token_jti == "claim-jti"
