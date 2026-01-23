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


# Test RSA key pair (2048-bit) - for testing only
# Generated using scripts/generate_oauth_keys.py
TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11aDtan2X7odUK6UWc+nezcWteyl6Q
+pYjhtURJt59rDw7/gGpVC3kDxjmGttnc4UmEA1+Sxfa4ch7KXpaquAnGmZ8a70i
MKkYkNZ3IUb2R2TeBSko+aIfMLn5D5q/mIwHjwoOC/jtlmNCz/v4fT9bo1QnrB96
iuhFpC5BL1Y3ArAwZ8G6Oi2wnjdpLP0HlLWQ6sPBK6QmMARGi0Bec4aJnWtTnCAF
bKD4sRmUk5BX537OPg3ZqHOl2Lfjz8CHdOgNqq1avvguJl1woDUgLGX0KPz89P35
1byPbLSPmMdcdrs+sES1TAUwGrCv04Ml36nr+wIDAQABAoIBAE0gxN1QCtMIvjvM
QNDph+romhEgzBlB4oAIqWykdyUCKPz9p+knXs/2M4qXPlElWvuAfZ/UZM6jlLDi
MFxpivprBVJCV4EYM99mvjcJUQrTZg/8cOtZqb4nLNhOIRH99ZPtrOJPCkvktgfA
fGS/zp+Gu/FWfii42O8qGwP3ePic1CP3zV2TGyo6TGL9Tpc8zAliV/rn7afuHTdi
OBByfQHbAv8rdhQsxC+N6iBpv8kqD+SrV5UOhR1B4br1Ps4idsnvxNSK8nBf78X2
ES01VUwsy4ibLGmKISVDU2CFUZPBL5k73oWFLbmxWFlNxJhaaOhhVZStvlsj1V19
knKDSLECgYEA9k8ogMqghvySiFOFH1qQjyyjdpPAF7FRQQliUffXSP88mxqA1U0B
QuZSwQhVEbjaIYMbq47UhhZO2ZMNETvUJfDS9+9j6lXOkBA0Y+pgG0d/Q8oW+f5E
lAewUn7rFp+LWyCIHAm/B9rolnUZOQn8hjpZUoUlB9duTAJK5KTduxkCgYEAtjVS
YQZkKvaUkbyKLozTslRpCZ3aInnHmE7v/pAxhhOtZADVIvyud8isScT51SEGENKb
UEhguH/4J3KzwYWguQsa8nKX01kNyCYrRVTBy8mBpBbMivUeni8NnmZdN+4xWNxk
0klcR/IfD1JPaYn2/dT2xw/UQm3pN2u6SwNlljMCgYAg9OyFdxdNmIP+y7YXQOXw
0lc46YIdaXNm0VuffhsHQGealUxBviD6E5llDLldOq+tJj7QkLbtDhUU1bE86hVz
0ipYVGa5FywhaJmp+NVNMR7dMjAq2RPR/slf8G2CRGzAD7VGkyNFaMwQPbDXSffI
+guzl+8hgnjWoMPmngyxGQKBgQCi/+iGK7WISbZ2+XeUVR88tQ6SL8hodxD7TAKe
dFgZXZkLkUMaXXmmZrrZ84CcDULj4cRXeags5S4V/CKayPQTFcAxjBoOU+hkWOD0
0PHdYJK9PDgXR1jwoZ9JSMGzuz4iBGedR0tqgUsP5c5s16NBSDbLtXlpQ/ISRikN
2igSyQKBgQCkvycDCG3A1mXKF8045a4YTD6AJ1iJOxDdAcdsnIggzhNzmuzN9YK+
wlUdUYruhvuzYU1qoIk22GeuHMsSHGToFYs/bYvI5+zlK5bMMYfGFf/TmgLApT14
0OOftMOAdZjZmI3/+oXalmKGuCaaEpyKFj+mr/v1IIxJMx0Mc+6n3Q==
-----END RSA PRIVATE KEY-----"""

TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAr0+UcafV6BzHPkDETQ11
aDtan2X7odUK6UWc+nezcWteyl6Q+pYjhtURJt59rDw7/gGpVC3kDxjmGttnc4Um
EA1+Sxfa4ch7KXpaquAnGmZ8a70iMKkYkNZ3IUb2R2TeBSko+aIfMLn5D5q/mIwH
jwoOC/jtlmNCz/v4fT9bo1QnrB96iuhFpC5BL1Y3ArAwZ8G6Oi2wnjdpLP0HlLWQ
6sPBK6QmMARGi0Bec4aJnWtTnCAFbKD4sRmUk5BX537OPg3ZqHOl2Lfjz8CHdOgN
qq1avvguJl1woDUgLGX0KPz89P351byPbLSPmMdcdrs+sES1TAUwGrCv04Ml36nr
+wIDAQAB
-----END PUBLIC KEY-----"""


@pytest.fixture
def mock_revocation_store():
    """Mock token revocation store."""
    store = AsyncMock()
    store.is_revoked = AsyncMock(return_value=False)
    store.add_revoked_token = AsyncMock()
    return store


@pytest.fixture
def mock_settings():
    """Mock JWT settings."""
    settings = Mock()
    settings.jwt_access_token_expire_minutes = 60
    settings.jwt_refresh_token_expire_days = 7
    return settings


@pytest.fixture
def token_generator(mock_revocation_store, mock_settings):
    """Create JWT token generator with test keys."""
    return RS256JWTTokenGenerator(
        private_key=TEST_PRIVATE_KEY,
        public_key=TEST_PUBLIC_KEY,
        revocation_store=mock_revocation_store,
        settings=mock_settings,
    )


@pytest.fixture
def mock_user():
    """Mock user object."""
    user = Mock()
    user.user_id = "user_123"
    user.username = "testuser"
    return user


class TestAccessTokenGeneration:
    """Test access token generation."""

    @pytest.mark.asyncio
    async def test_generate_access_token_success(self, token_generator, mock_user):
        """Test successful access token generation."""
        token = await token_generator.generate_access_token(mock_user)

        # Verify token is non-empty string
        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token payload
        payload = jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"])

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
        token1 = await token_generator.generate_access_token(mock_user)
        token2 = await token_generator.generate_access_token(mock_user)

        payload1 = jwt.decode(token1, TEST_PUBLIC_KEY, algorithms=["RS256"])
        payload2 = jwt.decode(token2, TEST_PUBLIC_KEY, algorithms=["RS256"])

        # JTIs should be different
        assert payload1["jti"] != payload2["jti"]

    @pytest.mark.asyncio
    async def test_generate_access_token_signature_valid(
        self, token_generator, mock_user
    ):
        """Test that generated token has valid RS256 signature."""
        token = await token_generator.generate_access_token(mock_user)

        # This should not raise exception if signature is valid
        jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"])


class TestRefreshTokenGeneration:
    """Test refresh token generation."""

    @pytest.mark.asyncio
    async def test_generate_refresh_token_success(self, token_generator, mock_user):
        """Test successful refresh token generation."""
        token = await token_generator.generate_refresh_token(mock_user)

        # Verify token is non-empty string
        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token payload
        payload = jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"])

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
        token1 = await token_generator.generate_refresh_token(mock_user)
        token2 = await token_generator.generate_refresh_token(mock_user)

        payload1 = jwt.decode(token1, TEST_PUBLIC_KEY, algorithms=["RS256"])
        payload2 = jwt.decode(token2, TEST_PUBLIC_KEY, algorithms=["RS256"])

        # JTIs should be different
        assert payload1["jti"] != payload2["jti"]


class TestTokenValidation:
    """Test token validation."""

    @pytest.mark.asyncio
    async def test_validate_access_token_success(self, token_generator, mock_user):
        """Test successful access token validation."""
        token = await token_generator.generate_access_token(mock_user)

        # Validate token
        payload = await token_generator.validate_access_token(token)

        assert payload is not None
        assert payload["sub"] == "user_123"
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"

    @pytest.mark.asyncio
    async def test_validate_access_token_expired(
        self, token_generator, mock_user, mock_settings
    ):
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
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
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
        token = await token_generator.generate_access_token(mock_user)

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
        token = await token_generator.generate_refresh_token(mock_user)

        # Validate token
        payload = await token_generator.validate_refresh_token(token)

        assert payload is not None
        assert payload["sub"] == "user_123"
        assert payload["type"] == "refresh"

    @pytest.mark.asyncio
    async def test_validate_refresh_token_wrong_type(
        self, token_generator, mock_user
    ):
        """Test validation fails when access token used as refresh token."""
        # Generate access token
        access_token = await token_generator.generate_access_token(mock_user)

        # Validation should return None
        result = await token_generator.validate_refresh_token(access_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_validate_refresh_token_expired(
        self, token_generator, mock_user, mock_settings
    ):
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
        token = await token_generator.generate_refresh_token(mock_user)

        # Mark token as revoked
        mock_revocation_store.is_revoked.return_value = True

        # Validation should return None
        result = await token_generator.validate_refresh_token(token)
        assert result is None


class TestTokenRevocation:
    """Test token revocation."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(
        self, token_generator, mock_user, mock_revocation_store, mock_settings
    ):
        """Test access token revocation."""
        token = await token_generator.generate_access_token(mock_user)

        await token_generator.revoke_access_token(token)

        # Verify add_revoked_token called with JTI and TTL
        mock_revocation_store.add_revoked_token.assert_called_once()
        call_args = mock_revocation_store.add_revoked_token.call_args[0]
        jti = call_args[0]
        ttl = call_args[1]

        # Verify JTI matches token
        payload = jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"])
        assert jti == payload["jti"]

        # Verify TTL is approximately 1 hour
        assert 3590 <= ttl <= 3610

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(
        self, token_generator, mock_user, mock_revocation_store, mock_settings
    ):
        """Test refresh token revocation."""
        token = await token_generator.generate_refresh_token(mock_user)

        await token_generator.revoke_refresh_token(token)

        # Verify add_revoked_token called with JTI and TTL
        mock_revocation_store.add_revoked_token.assert_called_once()
        call_args = mock_revocation_store.add_revoked_token.call_args[0]
        jti = call_args[0]
        ttl = call_args[1]

        # Verify JTI matches token
        payload = jwt.decode(token, TEST_PUBLIC_KEY, algorithms=["RS256"])
        assert jti == payload["jti"]

        # Verify TTL is approximately 7 days
        expected_seconds = 7 * 24 * 60 * 60
        assert expected_seconds - 10 <= ttl <= expected_seconds + 10

    @pytest.mark.asyncio
    async def test_revoke_expired_token(
        self, token_generator, mock_revocation_store, mock_settings
    ):
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
