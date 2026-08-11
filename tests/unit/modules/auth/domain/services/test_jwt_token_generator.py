"""Unit tests for JWT token generator implementation.

Tests cover:
- Access token generation (RS256 signing)
- Refresh token generation
- Token validation (signature, expiry, revocation)
- JTI extraction
- Token revocation checking
- Error handling (expired tokens, invalid signatures, revoked tokens)
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import jwt
import pytest

from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    RS256JWTTokenGenerator,
)

#: The configured pair, as production wires it (JWT_ISSUER/JWT_AUDIENCE
#: defaults). Deliberately not the literals the HS256 paths once hardcoded:
#: a fixture that matched those could not observe #938.
ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"


# Test RSA key pair generation
# Keys are generated dynamically for each test run to avoid hardcoding secrets
def _generate_test_rsa_keypair():
    """Generate ephemeral RSA key pair for testing.

    This function generates a new 2048-bit RSA key pair each time it's called.
    Keys are never persisted and exist only in memory during test execution.

    Returns:
        tuple: (private_key_pem, public_key_pem) as strings
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )

    # Serialize private key to PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    # Get public key
    public_key = private_key.public_key()

    # Serialize public key to PEM format
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return private_pem, public_pem


# Generate test keys once at module load
# This is acceptable for tests as they are ephemeral and never committed
TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_rsa_keypair()


@pytest.fixture
def mock_revocation_store():
    """Mock token revocation store.

    Every read method must be stubbed explicitly: a bare AsyncMock returns a
    truthy Mock for anything unstubbed, which a revocation check reads as
    "revoked".
    """
    store = AsyncMock()
    store.is_revoked = AsyncMock(return_value=False)
    store.is_user_revoked = AsyncMock(return_value=False)
    store.add_revoked_token = AsyncMock()
    store.revoke_user_tokens_before = AsyncMock()
    return store


# Lifetimes are explicit constructor arguments, not read from a settings object
# (#888), so there is nothing to stub here.
ACCESS_MINUTES = 60
REFRESH_DAYS = 7


@pytest.fixture
def token_generator(mock_revocation_store):
    """Create JWT token generator with test keys."""
    return RS256JWTTokenGenerator(
        private_key=TEST_PRIVATE_KEY,
        public_key=TEST_PUBLIC_KEY,
        revocation_store=mock_revocation_store,
        access_token_expire_minutes=ACCESS_MINUTES,
        refresh_token_expire_days=REFRESH_DAYS,
        issuer=ISSUER,
        audience=AUDIENCE,
    )


@pytest.fixture
def mock_user():
    """Mock user object."""
    user = Mock()
    user.user_id = "user_123"
    user.username = "testuser"
    user.email = "testuser@example.com"
    user.roles = ["user"]
    user.organization_id = "org_123"
    return user


class TestAccessTokenGeneration:
    """Test access token generation."""

    @pytest.mark.asyncio
    async def test_generate_access_token_success(self, token_generator, mock_user):
        """Test successful access token generation."""
        token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Verify token is non-empty string
        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token payload
        payload = jwt.decode(
            token, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )

        assert payload["sub"] == "user_123"
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

        # Verify expiry is approximately 1 hour from now
        exp_time = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        time_diff = (exp_time - now).total_seconds()
        assert 3590 <= time_diff <= 3610  # Allow 10 second tolerance

    @pytest.mark.asyncio
    async def test_generate_access_token_unique_jti(self, token_generator, mock_user):
        """Test that each access token has unique JTI."""
        token1 = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )
        token2 = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        payload1 = jwt.decode(
            token1, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )
        payload2 = jwt.decode(
            token2, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )

        # JTIs should be different
        assert payload1["jti"] != payload2["jti"]

    @pytest.mark.asyncio
    async def test_generate_access_token_signature_valid(
        self, token_generator, mock_user
    ):
        """Test that generated token has valid RS256 signature."""
        token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # This should not raise exception if signature is valid
        jwt.decode(
            token, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )


class TestRefreshTokenGeneration:
    """Test refresh token generation."""

    @pytest.mark.asyncio
    async def test_generate_refresh_token_success(self, token_generator, mock_user):
        """Test successful refresh token generation."""
        token = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Verify token is non-empty string
        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token payload
        payload = jwt.decode(
            token, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )

        assert payload["sub"] == "user_123"
        assert payload["type"] == "refresh"
        assert "jti" in payload
        assert "exp" in payload
        assert "iat" in payload

        # Verify expiry is approximately 7 days from now
        exp_time = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        time_diff = (exp_time - now).total_seconds()
        expected_seconds = 7 * 24 * 60 * 60  # 7 days
        assert expected_seconds - 10 <= time_diff <= expected_seconds + 10

    @pytest.mark.asyncio
    async def test_generate_refresh_token_unique_jti(self, token_generator, mock_user):
        """Test that each refresh token has unique JTI."""
        token1 = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )
        token2 = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        payload1 = jwt.decode(
            token1, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )
        payload2 = jwt.decode(
            token2, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )

        # JTIs should be different
        assert payload1["jti"] != payload2["jti"]


class TestTokenValidation:
    """Test token validation."""

    @pytest.mark.asyncio
    async def test_validate_access_token_success(self, token_generator, mock_user):
        """Test successful access token validation."""
        token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Validate token
        payload = await token_generator.validate_access_token(token)

        assert payload is not None
        assert payload["sub"] == "user_123"
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"

    @pytest.mark.asyncio
    async def test_validate_access_token_expired(self, token_generator, mock_user):
        """Test validation of expired access token."""
        # Create token with past expiry
        now = datetime.now(timezone.utc)
        expired_time = now - timedelta(hours=1)

        payload = {
            "sub": mock_user.user_id,
            "username": mock_user.username,
            "exp": int(expired_time.timestamp()),
            "iat": int((expired_time - timedelta(hours=1)).timestamp()),
            "jti": "expired_jti_123",
            "type": "access",
        }

        expired_token = jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")

        # Validation should return None
        result = await token_generator.validate_access_token(expired_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_validate_access_token_invalid_signature(self, token_generator):
        """Test validation of token with invalid signature."""
        # Create token with malformed signature
        # Use a valid token and corrupt it
        valid_token = jwt.encode(
            {
                "sub": "user_123",
                "username": "testuser",
                "exp": int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                ),
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "jti": "invalid_sig_jti",
                "type": "access",
            },
            TEST_PRIVATE_KEY,
            algorithm="RS256",
        )
        # Corrupt the signature by modifying last character
        invalid_token = valid_token[:-5] + "xxxxx"

        # Validation should return None
        result = await token_generator.validate_access_token(invalid_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_validate_access_token_revoked(
        self, token_generator, mock_user, mock_revocation_store
    ):
        """Test validation of revoked access token."""
        token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Mark token as revoked
        mock_revocation_store.is_revoked.return_value = True

        # Validation should return None
        result = await token_generator.validate_access_token(token)
        assert result is None


class TestRefreshTokenValidation:
    """Test refresh token validation."""

    @pytest.mark.asyncio
    async def test_validate_refresh_token_success(self, token_generator, mock_user):
        """Test successful refresh token validation."""
        token = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Validate token
        payload = await token_generator.validate_refresh_token(token)

        assert payload is not None
        assert payload["sub"] == "user_123"
        assert payload["type"] == "refresh"

    @pytest.mark.asyncio
    async def test_validate_refresh_token_wrong_type(self, token_generator, mock_user):
        """Test validation fails when access token used as refresh token."""
        # Generate access token
        access_token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Validation should return None
        result = await token_generator.validate_refresh_token(access_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_validate_refresh_token_expired(self, token_generator, mock_user):
        """Test validation of expired refresh token."""
        # Create token with past expiry
        now = datetime.now(timezone.utc)
        expired_time = now - timedelta(days=1)

        payload = {
            "sub": mock_user.user_id,
            "username": mock_user.username,
            "exp": int(expired_time.timestamp()),
            "iat": int((expired_time - timedelta(days=7)).timestamp()),
            "jti": "expired_refresh_jti",
            "type": "refresh",
        }

        expired_token = jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")

        # Validation should return None
        result = await token_generator.validate_refresh_token(expired_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_validate_refresh_token_revoked(
        self, token_generator, mock_user, mock_revocation_store
    ):
        """Test validation of revoked refresh token."""
        token = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        # Mark token as revoked
        mock_revocation_store.is_revoked.return_value = True

        # Validation should return None
        result = await token_generator.validate_refresh_token(token)
        assert result is None


class TestTokenRevocation:
    """Test token revocation."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(
        self, token_generator, mock_user, mock_revocation_store
    ):
        """Test access token revocation."""
        token = await token_generator.generate_access_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        await token_generator.revoke_access_token(token)

        # Verify add_revoked_token called with JTI and TTL
        mock_revocation_store.add_revoked_token.assert_called_once()
        call_args = mock_revocation_store.add_revoked_token.call_args[0]
        jti = call_args[0]
        ttl = call_args[1]

        # Verify JTI matches token
        payload = jwt.decode(
            token, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )
        assert jti == payload["jti"]

        # Verify TTL is approximately 1 hour
        assert 3590 <= ttl <= 3610

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(
        self, token_generator, mock_user, mock_revocation_store
    ):
        """Test refresh token revocation."""
        token = await token_generator.generate_refresh_token(
            mock_user, state_read_at=datetime.now(timezone.utc)
        )

        await token_generator.revoke_refresh_token(token)

        # Verify add_revoked_token called with JTI and TTL
        mock_revocation_store.add_revoked_token.assert_called_once()
        call_args = mock_revocation_store.add_revoked_token.call_args[0]
        jti = call_args[0]
        ttl = call_args[1]

        # Verify JTI matches token
        payload = jwt.decode(
            token, TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
        )
        assert jti == payload["jti"]

        # Verify TTL is approximately 7 days
        expected_seconds = 7 * 24 * 60 * 60
        assert expected_seconds - 10 <= ttl <= expected_seconds + 10

    @pytest.mark.asyncio
    async def test_revoke_expired_token(self, token_generator, mock_revocation_store):
        """Test revoking expired token (should not add to revocation store)."""
        # Create expired token
        now = datetime.now(timezone.utc)
        expired_time = now - timedelta(hours=1)

        payload = {
            "sub": "user_123",
            "username": "testuser",
            "exp": int(expired_time.timestamp()),
            "iat": int((expired_time - timedelta(hours=1)).timestamp()),
            "jti": "expired_revoke_jti",
            "type": "access",
        }

        expired_token = jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")

        # Revocation should succeed but not add to store (TTL <= 0)
        await token_generator.revoke_access_token(expired_token)

        # Verify add_revoked_token NOT called (token already expired)
        mock_revocation_store.add_revoked_token.assert_not_called()
