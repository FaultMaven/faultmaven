"""Unit tests for AuthService (TASK-017)

Tests JWT token generation, verification, refresh, and revocation.

Test Categories:
1. Token Generation Tests - Access and refresh token creation
2. Token Verification Tests - Signature, expiration, claims validation
3. Token Refresh Tests - Token exchange and rotation
4. Token Revocation Tests - Redis-based revocation

Coverage Target: 90%+
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from faultmaven.models.auth import AuthenticatedUser, TokenClaims, TokenPair
from faultmaven.models.rbac import Permission, Role, get_permissions_for_roles
from faultmaven.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    settings = MagicMock()
    settings.security.jwt_algorithm = "RS256"
    settings.security.jwt_access_token_expire_minutes = 15
    settings.security.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = "faultmaven-api"
    settings.security.jwt_audience = "faultmaven-app"
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = None
    settings.security.jwt_public_key = None
    settings.security.jwt_private_key_path = None
    settings.security.jwt_public_key_path = None
    settings.security.jwt_secret_key = None
    return settings


@pytest.fixture
def mock_redis():
    """Mock Redis client for revocation testing."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


@pytest.fixture
def auth_service(mock_settings):
    """Create AuthService with mocked settings."""
    with patch(
        "faultmaven.services.auth_service.get_settings", return_value=mock_settings
    ):
        service = AuthService()
        return service


@pytest.fixture
def auth_service_with_redis(mock_settings, mock_redis):
    """Create AuthService with mocked settings and Redis."""
    with patch(
        "faultmaven.services.auth_service.get_settings", return_value=mock_settings
    ):
        service = AuthService(redis_client=mock_redis)
        return service


@pytest.fixture
def sample_user_data() -> Dict[str, Any]:
    """Sample user data for token generation."""
    return {
        "user_id": "user-123",
        "organization_id": "org-456",
        "email": "test@example.com",
        "roles": ["admin"],
    }


# ============================================================
# Token Generation Tests
# ============================================================


class TestTokenGeneration:
    """Tests for JWT token generation."""

    def test_generate_access_token_creates_valid_jwt(
        self, auth_service, sample_user_data
    ):
        """Access token generation creates a valid JWT."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 50  # JWTs are long
        assert token.count(".") == 2  # JWT has 3 parts

    def test_access_token_contains_required_claims(
        self, auth_service, sample_user_data
    ):
        """Access token contains all required JWT claims."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")

        assert claims["sub"] == sample_user_data["user_id"]
        assert claims["org_id"] == sample_user_data["organization_id"]
        assert claims["email"] == sample_user_data["email"]
        assert claims["roles"] == sample_user_data["roles"]
        assert claims["iss"] == "faultmaven-api"
        assert claims["aud"] == "faultmaven-app"
        assert "jti" in claims  # Unique token ID
        assert "iat" in claims  # Issued at
        assert "exp" in claims  # Expiration
        assert claims["token_type"] == "access"

    def test_access_token_permissions_auto_derived_from_roles(
        self, auth_service, sample_user_data
    ):
        """Permissions are auto-derived from roles when not explicitly provided."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=["admin"],
            permissions=None,  # Should be auto-derived
        )

        claims = auth_service.verify_token(token, token_type="access")

        # Admin should have all permissions
        expected_permissions = [p.value for p in get_permissions_for_roles(["admin"])]
        assert set(claims["permissions"]) == set(expected_permissions)

    def test_access_token_explicit_permissions(self, auth_service, sample_user_data):
        """Explicit permissions override auto-derived ones."""
        explicit_permissions = ["cases:read", "sessions:read"]

        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=["admin"],
            permissions=explicit_permissions,
        )

        claims = auth_service.verify_token(token, token_type="access")

        assert claims["permissions"] == explicit_permissions

    def test_access_token_expires_in_15_minutes(self, auth_service, sample_user_data):
        """Access token expires in configured minutes (default 15)."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")

        now = int(datetime.now(timezone.utc).timestamp())
        exp = claims["exp"]

        # Should expire roughly 15 minutes from now (with some tolerance)
        expected_exp = now + (15 * 60)
        assert abs(exp - expected_exp) < 5  # 5 second tolerance

    def test_generate_refresh_token_creates_valid_jwt(
        self, auth_service, sample_user_data
    ):
        """Refresh token generation creates a valid JWT."""
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        assert token is not None
        assert isinstance(token, str)
        assert token.count(".") == 2

    def test_refresh_token_contains_minimal_claims(
        self, auth_service, sample_user_data
    ):
        """Refresh token contains only minimal claims for security."""
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        claims = auth_service.verify_token(token, token_type="refresh")

        assert claims["sub"] == sample_user_data["user_id"]
        assert claims["org_id"] == sample_user_data["organization_id"]
        assert "jti" in claims
        assert claims["token_type"] == "refresh"
        # Refresh tokens don't have email, roles, permissions
        assert "email" not in claims
        assert "roles" not in claims
        assert "permissions" not in claims

    def test_refresh_token_expires_in_7_days(self, auth_service, sample_user_data):
        """Refresh token expires in configured days (default 7)."""
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        claims = auth_service.verify_token(token, token_type="refresh")

        now = int(datetime.now(timezone.utc).timestamp())
        exp = claims["exp"]

        # Should expire roughly 7 days from now
        expected_exp = now + (7 * 24 * 60 * 60)
        assert abs(exp - expected_exp) < 5

    def test_different_users_get_different_tokens(self, auth_service):
        """Different users get different tokens."""
        token1 = auth_service.generate_access_token(
            user_id="user-1",
            organization_id="org-1",
            email="user1@example.com",
            roles=["member"],
        )

        token2 = auth_service.generate_access_token(
            user_id="user-2",
            organization_id="org-1",
            email="user2@example.com",
            roles=["member"],
        )

        assert token1 != token2

    def test_token_includes_jti_for_revocation(self, auth_service, sample_user_data):
        """Each token has a unique jti for revocation tracking."""
        token1 = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        token2 = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims1 = auth_service.verify_token(token1, token_type="access")
        claims2 = auth_service.verify_token(token2, token_type="access")

        assert claims1["jti"] != claims2["jti"]
        assert len(claims1["jti"]) == 36  # UUID format

    def test_generate_token_pair_creates_both_tokens(
        self, auth_service, sample_user_data
    ):
        """generate_token_pair creates both access and refresh tokens."""
        pair = auth_service.generate_token_pair(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        assert isinstance(pair, TokenPair)
        assert pair.access_token is not None
        assert pair.refresh_token is not None
        assert pair.token_type == "Bearer"
        assert pair.expires_in == 15 * 60  # 15 minutes in seconds

    def test_token_pair_tokens_are_different(self, auth_service, sample_user_data):
        """Access and refresh tokens in a pair are different."""
        pair = auth_service.generate_token_pair(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        assert pair.access_token != pair.refresh_token


# ============================================================
# Token Verification Tests
# ============================================================


class TestTokenVerification:
    """Tests for JWT token verification."""

    def test_verify_valid_access_token(self, auth_service, sample_user_data):
        """verify_token decodes a valid access token."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")

        assert claims is not None
        assert claims["sub"] == sample_user_data["user_id"]

    def test_verify_valid_refresh_token(self, auth_service, sample_user_data):
        """verify_token decodes a valid refresh token."""
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        claims = auth_service.verify_token(token, token_type="refresh")

        assert claims is not None
        assert claims["sub"] == sample_user_data["user_id"]

    def test_verify_raises_on_expired_token(self, auth_service, mock_settings):
        """verify_token raises AuthenticationError on expired token."""
        # Create an already-expired token
        now = datetime.now(timezone.utc)
        expired_claims = {
            "sub": "user-123",
            "org_id": "org-456",
            "email": "test@example.com",
            "roles": ["admin"],
            "permissions": [],
            "iss": "faultmaven-api",
            "aud": "faultmaven-app",
            "iat": int((now - timedelta(hours=1)).timestamp()),
            "exp": int((now - timedelta(minutes=1)).timestamp()),  # Already expired
            "jti": str(uuid.uuid4()),
            "token_type": "access",
        }

        # Encode with the service's key
        expired_token = auth_service._encode_token(expired_claims)

        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(expired_token, token_type="access")

        assert exc_info.value.error_code == "TOKEN_EXPIRED"

    def test_verify_raises_on_invalid_signature(self, auth_service):
        """verify_token raises AuthenticationError on wrong signature."""
        # Create a token with wrong key
        fake_claims = {
            "sub": "user-123",
            "org_id": "org-456",
            "iss": "faultmaven-api",
            "aud": "faultmaven-app",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int(
                (datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()
            ),
            "jti": str(uuid.uuid4()),
            "token_type": "access",
        }

        # Encode with a different secret (using HS256 with wrong secret raises InvalidTokenError, not DecodeError)
        fake_token = jwt.encode(fake_claims, "wrong-secret", algorithm="HS256")

        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(fake_token, token_type="access")

        # Using wrong secret with HS256 raises InvalidTokenError (caught by generic handler), not DecodeError
        assert exc_info.value.error_code in ["INVALID_TOKEN", "DECODE_ERROR"]

    def test_verify_raises_on_wrong_token_type_refresh_as_access(
        self, auth_service, sample_user_data
    ):
        """verify_token raises AuthenticationError when refresh token used as access."""
        # Generate refresh token
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        # Try to verify as access token
        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(token, token_type="access")

        assert exc_info.value.error_code == "INVALID_TOKEN_TYPE"

    def test_verify_raises_on_wrong_token_type(self, auth_service, sample_user_data):
        """verify_token raises AuthenticationError on wrong token type."""
        # Generate refresh token
        token = auth_service.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        # Try to verify as access token
        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(token, token_type="access")

        assert exc_info.value.error_code == "INVALID_TOKEN_TYPE"

    def test_verify_raises_on_malformed_token(self, auth_service):
        """verify_token raises AuthenticationError on malformed token."""
        with pytest.raises(AuthenticationError):
            auth_service.verify_token("not.a.valid.token", token_type="access")

    def test_verify_raises_on_empty_token(self, auth_service):
        """verify_token raises AuthenticationError on empty token."""
        with pytest.raises(AuthenticationError):
            auth_service.verify_token("", token_type="access")

    def test_verify_handles_missing_claims_gracefully(self, auth_service):
        """verify_token handles tokens with missing required claims."""
        # Create token without required claims
        incomplete_claims = {
            "iss": "faultmaven-api",
            "aud": "faultmaven-app",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int(
                (datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()
            ),
        }

        incomplete_token = auth_service._encode_token(incomplete_claims)

        with pytest.raises(AuthenticationError):
            auth_service.verify_token(incomplete_token, token_type="access")


# ============================================================
# Token Verification with Revocation Tests
# ============================================================


class TestTokenVerificationWithRevocation:
    """Tests for token verification with revocation checking."""

    @pytest.mark.asyncio
    async def test_verify_with_revocation_allows_valid_token(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Valid non-revoked token passes verification."""
        token = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        mock_redis.get.return_value = None  # Not revoked

        claims = await auth_service_with_redis.verify_token_with_revocation_check(
            token, token_type="access"
        )

        assert claims is not None
        assert claims["sub"] == sample_user_data["user_id"]

    @pytest.mark.asyncio
    async def test_verify_with_revocation_raises_on_revoked_token(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Revoked token raises TokenRevocationError."""
        token = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        mock_redis.get.return_value = b"revoked"  # Token is revoked

        with pytest.raises(TokenRevocationError):
            await auth_service_with_redis.verify_token_with_revocation_check(
                token, token_type="access"
            )


# ============================================================
# Token Refresh Tests
# ============================================================


class TestTokenRefresh:
    """Tests for token refresh functionality."""

    @pytest.mark.asyncio
    async def test_refresh_exchanges_for_new_tokens(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """refresh_access_token exchanges refresh token for new tokens."""
        refresh_token = auth_service_with_redis.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        mock_redis.get.return_value = None  # Not revoked

        async def mock_user_loader(user_id):
            return ("test@example.com", ["admin"], None)

        new_access, new_refresh = await auth_service_with_redis.refresh_access_token(
            refresh_token=refresh_token,
            user_loader=mock_user_loader,
        )

        assert new_access is not None
        assert new_refresh is not None
        assert new_access != refresh_token
        assert new_refresh != refresh_token

    @pytest.mark.asyncio
    async def test_refresh_new_token_has_updated_permissions(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """New access token from refresh has current permissions."""
        refresh_token = auth_service_with_redis.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        mock_redis.get.return_value = None

        # User loader returns updated roles
        async def mock_user_loader(user_id):
            return (
                "test@example.com",
                ["member"],
                None,
            )  # Changed from admin to member

        new_access, _ = await auth_service_with_redis.refresh_access_token(
            refresh_token=refresh_token,
            user_loader=mock_user_loader,
        )

        claims = auth_service_with_redis.verify_token(new_access, token_type="access")
        assert "member" in claims["roles"]

    @pytest.mark.asyncio
    async def test_refresh_revokes_old_refresh_token(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Old refresh token is revoked after refresh."""
        refresh_token = auth_service_with_redis.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        # Get the jti of old token
        old_claims = auth_service_with_redis.verify_token(
            refresh_token, token_type="refresh"
        )
        old_jti = old_claims["jti"]

        mock_redis.get.return_value = None

        async def mock_user_loader(user_id):
            return ("test@example.com", ["admin"], None)

        await auth_service_with_redis.refresh_access_token(
            refresh_token=refresh_token,
            user_loader=mock_user_loader,
        )

        # Verify revoke was called with old jti
        mock_redis.setex.assert_called()
        call_args = mock_redis.setex.call_args
        assert f"revoked:token:{old_jti}" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_refresh_raises_on_expired_refresh_token(
        self, auth_service_with_redis, sample_user_data
    ):
        """Expired refresh token raises AuthenticationError."""
        # Create expired refresh token
        now = datetime.now(timezone.utc)
        expired_claims = {
            "sub": sample_user_data["user_id"],
            "org_id": sample_user_data["organization_id"],
            "iss": "faultmaven-api",
            "aud": "faultmaven-app",
            "iat": int((now - timedelta(days=10)).timestamp()),
            "exp": int((now - timedelta(days=1)).timestamp()),
            "jti": str(uuid.uuid4()),
            "token_type": "refresh",
        }

        expired_token = auth_service_with_redis._encode_token(expired_claims)

        with pytest.raises(AuthenticationError) as exc_info:
            await auth_service_with_redis.refresh_access_token(
                refresh_token=expired_token,
                user_loader=lambda x: ("test@example.com", ["admin"], None),
            )

        assert exc_info.value.error_code == "TOKEN_EXPIRED"

    @pytest.mark.asyncio
    async def test_refresh_raises_on_revoked_refresh_token(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Revoked refresh token raises TokenRevocationError."""
        refresh_token = auth_service_with_redis.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        mock_redis.get.return_value = b"revoked"

        with pytest.raises(TokenRevocationError):
            await auth_service_with_redis.refresh_access_token(
                refresh_token=refresh_token,
                user_loader=lambda x: ("test@example.com", ["admin"], None),
            )

    @pytest.mark.asyncio
    async def test_refresh_raises_when_user_not_found(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """refresh_access_token raises when user no longer exists."""
        refresh_token = auth_service_with_redis.generate_refresh_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
        )

        mock_redis.get.return_value = None

        async def mock_user_loader(user_id):
            return None  # User not found

        with pytest.raises(AuthenticationError) as exc_info:
            await auth_service_with_redis.refresh_access_token(
                refresh_token=refresh_token,
                user_loader=mock_user_loader,
            )

        assert exc_info.value.error_code == "USER_NOT_FOUND"


# ============================================================
# Token Revocation Tests
# ============================================================


class TestTokenRevocation:
    """Tests for token revocation functionality."""

    @pytest.mark.asyncio
    async def test_revoke_token_adds_jti_to_revocation_list(
        self, auth_service_with_redis, mock_redis
    ):
        """revoke_token adds jti to Redis revocation list."""
        jti = str(uuid.uuid4())
        exp = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())

        await auth_service_with_redis.revoke_token(jti, exp)

        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args[0]
        assert f"revoked:token:{jti}" in call_args[0]
        assert call_args[2] == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_token_sets_correct_ttl(
        self, auth_service_with_redis, mock_redis
    ):
        """revoke_token sets TTL matching token expiration."""
        jti = str(uuid.uuid4())
        now = int(datetime.now(timezone.utc).timestamp())
        exp = now + 600  # 10 minutes from now

        await auth_service_with_redis.revoke_token(jti, exp)

        call_args = mock_redis.setex.call_args[0]
        ttl = call_args[1]
        assert 595 <= ttl <= 605  # ~600 seconds with tolerance

    @pytest.mark.asyncio
    async def test_revoked_tokens_fail_verification(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Revoked tokens fail verification check."""
        token = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = auth_service_with_redis.verify_token(token, token_type="access")
        jti = claims["jti"]
        exp = claims["exp"]

        # Revoke the token
        await auth_service_with_redis.revoke_token(jti, exp)

        # Now set redis to return revoked
        mock_redis.get.return_value = b"revoked"

        with pytest.raises(TokenRevocationError):
            await auth_service_with_redis.verify_token_with_revocation_check(
                token, token_type="access"
            )

    @pytest.mark.asyncio
    async def test_multiple_tokens_can_be_revoked(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """Multiple tokens can be revoked independently."""
        token1 = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        token2 = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims1 = auth_service_with_redis.verify_token(token1, token_type="access")
        claims2 = auth_service_with_redis.verify_token(token2, token_type="access")

        await auth_service_with_redis.revoke_token(claims1["jti"], claims1["exp"])
        await auth_service_with_redis.revoke_token(claims2["jti"], claims2["exp"])

        assert mock_redis.setex.call_count == 2


# ============================================================
# Extract User Tests
# ============================================================


class TestExtractUser:
    """Tests for extracting AuthenticatedUser from token."""

    def test_extract_user_from_token(self, auth_service, sample_user_data):
        """extract_user_from_token returns AuthenticatedUser."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        user = auth_service.extract_user_from_token(token)

        assert isinstance(user, AuthenticatedUser)
        assert user.user_id == sample_user_data["user_id"]
        assert user.organization_id == sample_user_data["organization_id"]
        assert user.email == sample_user_data["email"]
        assert user.roles == sample_user_data["roles"]

    def test_extract_user_includes_jti(self, auth_service, sample_user_data):
        """Extracted user includes token jti for revocation."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        user = auth_service.extract_user_from_token(token)

        assert user.token_jti is not None
        assert len(user.token_jti) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_extract_user_with_revocation_check(
        self, auth_service_with_redis, sample_user_data, mock_redis
    ):
        """extract_user_from_token_with_revocation_check works correctly."""
        token = auth_service_with_redis.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        mock_redis.get.return_value = None

        user = (
            await auth_service_with_redis.extract_user_from_token_with_revocation_check(
                token
            )
        )

        assert user.user_id == sample_user_data["user_id"]


# ============================================================
# Edge Cases and Error Handling Tests
# ============================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_roles_list(self, auth_service, sample_user_data):
        """Token can be generated with empty roles list."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=[],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["roles"] == []

    def test_special_characters_in_email(self, auth_service, sample_user_data):
        """Token handles special characters in email."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email="test+tag@example.com",
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["email"] == "test+tag@example.com"

    def test_unicode_in_claims(self, auth_service, sample_user_data):
        """Token handles unicode characters."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email="тест@example.com",
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["email"] == "тест@example.com"

    @pytest.mark.asyncio
    async def test_revocation_without_redis_logs_warning(
        self, auth_service, sample_user_data, caplog
    ):
        """revoke_token without Redis logs a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            await auth_service.revoke_token("test-jti", 12345)

        assert "Redis not configured" in caplog.text

    def test_multiple_roles(self, auth_service, sample_user_data):
        """Token handles multiple roles."""
        token = auth_service.generate_access_token(
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=["admin", "member", "viewer"],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert set(claims["roles"]) == {"admin", "member", "viewer"}


# ============================================================
# Key Loading Tests
# ============================================================


class TestKeyLoading:
    """Tests for _load_keys() functionality."""

    def test_load_keys_from_environment_variables(self, mock_settings):
        """Keys are loaded from environment variables (SecretStr)."""
        # Generate a test key pair
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

        # Configure mock settings to return keys from env
        mock_settings.security.jwt_private_key = MagicMock()
        mock_settings.security.jwt_private_key.get_secret_value.return_value = (
            private_pem
        )
        mock_settings.security.jwt_public_key = public_pem

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService()

            # Verify keys were loaded
            assert service._private_key == private_pem
            assert service._public_key == public_pem

    def test_load_keys_from_file_paths(self, mock_settings, tmp_path):
        """Keys are loaded from file paths."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate a test key pair
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

        # Write keys to temp files
        private_key_path = tmp_path / "private.pem"
        public_key_path = tmp_path / "public.pem"
        private_key_path.write_text(private_pem)
        public_key_path.write_text(public_pem)

        # Configure mock settings to use file paths
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = str(private_key_path)
        mock_settings.security.jwt_public_key_path = str(public_key_path)

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService()

            # Verify keys were loaded from files
            assert service._private_key == private_pem
            assert service._public_key == public_pem

    def test_load_keys_generates_dev_keys_when_not_configured(
        self, mock_settings, caplog
    ):
        """Development RSA keys are generated when no keys are configured."""
        import logging

        # No keys configured
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            with caplog.at_level(logging.WARNING):
                service = AuthService()

            # Verify dev keys were generated
            assert service._private_key is not None
            assert service._public_key is not None
            assert "-----BEGIN PRIVATE KEY-----" in service._private_key
            assert "-----BEGIN PUBLIC KEY-----" in service._public_key
            assert "Generated development RSA keys" in caplog.text

    def test_load_keys_warns_on_missing_key_file(self, mock_settings, caplog, tmp_path):
        """Warning is logged when key file does not exist."""
        import logging

        # Point to non-existent file
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = "/nonexistent/private.pem"
        mock_settings.security.jwt_public_key_path = "/nonexistent/public.pem"

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            with caplog.at_level(logging.WARNING):
                service = AuthService()

            # Warnings should be logged for missing files
            assert "Private key file not found" in caplog.text
            assert "Public key file not found" in caplog.text
            # Dev keys should still be generated as fallback
            assert service._private_key is not None

    def test_generated_keys_are_valid_rsa_2048(self, mock_settings):
        """Generated development keys are valid 2048-bit RSA keys."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
            load_pem_public_key,
        )

        # No keys configured - will generate dev keys
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService()

            # Load and validate private key
            private_key = load_pem_private_key(
                service._private_key.encode(),
                password=None,
                backend=default_backend(),
            )
            assert private_key.key_size == 2048

            # Load and validate public key
            public_key = load_pem_public_key(
                service._public_key.encode(),
                backend=default_backend(),
            )
            assert public_key.key_size == 2048

    def test_provided_keys_override_settings(self, mock_settings):
        """Keys provided to constructor override settings-loaded keys."""
        # Generate test keys
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        provided_private = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        provided_public = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService(
                private_key=provided_private,
                public_key=provided_public,
            )

            # Verify provided keys are used
            assert service._private_key == provided_private
            assert service._public_key == provided_public

    def test_tokens_can_be_generated_and_verified_with_loaded_keys(self, mock_settings):
        """Full token roundtrip works with properly loaded keys."""
        # No keys configured - will generate dev keys
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService()

            # Generate a token
            token = service.generate_access_token(
                user_id="user-123",
                organization_id="org-456",
                email="test@example.com",
                roles=["admin"],
            )

            # Verify the token
            claims = service.verify_token(token, token_type="access")

            assert claims["sub"] == "user-123"
            assert claims["org_id"] == "org-456"
            assert claims["email"] == "test@example.com"


# ============================================================
# Token Verification Edge Cases
# ============================================================


class TestTokenVerificationEdgeCases:
    """Additional token verification edge case tests."""

    def test_verify_raises_on_wrong_issuer(self, mock_settings):
        """verify_token raises AuthenticationError on wrong issuer."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test keys
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

        # Create token with wrong issuer
        now = datetime.now(timezone.utc)
        wrong_issuer_claims = {
            "sub": "user-123",
            "org_id": "org-456",
            "email": "test@example.com",
            "roles": ["admin"],
            "permissions": [],
            "iss": "wrong-issuer",  # Wrong issuer
            "aud": "faultmaven-app",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": str(uuid.uuid4()),
            "token_type": "access",
        }

        # Encode with the test private key
        token_with_wrong_issuer = jwt.encode(
            wrong_issuer_claims,
            private_pem,
            algorithm="RS256",
        )

        # Configure service with test keys
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = public_pem

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService(private_key=private_pem, public_key=public_pem)

            with pytest.raises(AuthenticationError) as exc_info:
                service.verify_token(token_with_wrong_issuer, token_type="access")

            assert exc_info.value.error_code == "INVALID_ISSUER"

    def test_verify_raises_on_wrong_audience(self, mock_settings):
        """verify_token raises AuthenticationError on wrong audience."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test keys
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

        # Create token with wrong audience
        now = datetime.now(timezone.utc)
        wrong_audience_claims = {
            "sub": "user-123",
            "org_id": "org-456",
            "email": "test@example.com",
            "roles": ["admin"],
            "permissions": [],
            "iss": "faultmaven-api",
            "aud": "wrong-audience",  # Wrong audience
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": str(uuid.uuid4()),
            "token_type": "access",
        }

        # Encode with the test private key
        token_with_wrong_audience = jwt.encode(
            wrong_audience_claims,
            private_pem,
            algorithm="RS256",
        )

        with patch(
            "faultmaven.services.auth_service.get_settings", return_value=mock_settings
        ):
            service = AuthService(private_key=private_pem, public_key=public_pem)

            with pytest.raises(AuthenticationError) as exc_info:
                service.verify_token(token_with_wrong_audience, token_type="access")

            assert exc_info.value.error_code == "INVALID_AUDIENCE"
