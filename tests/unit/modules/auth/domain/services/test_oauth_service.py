"""Unit tests for OAuth service implementation.

Tests cover:
- Authorization code generation
- PKCE challenge/verifier verification
- Code exchange for tokens
- Refresh token flow
- Token revocation
- Error handling
"""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from faultmaven.models.exceptions import InvalidGrantError, InvalidRequestError
from faultmaven.modules.auth.contracts import (
    OAuthAuthorizationDTO,
    OAuthCodeDTO,
    OAuthTokenDTO,
)
from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl


@pytest.fixture
def mock_code_repository():
    """Mock OAuth code repository."""
    repo = AsyncMock()
    repo.save_code = AsyncMock()
    repo.get_code = AsyncMock()
    repo.mark_code_used = AsyncMock()
    return repo


@pytest.fixture
def mock_user_repository():
    """Mock user repository."""
    repo = AsyncMock()
    repo.get = AsyncMock()
    repo.get_by_user_id = AsyncMock()
    return repo


@pytest.fixture
def mock_token_generator():
    """Mock JWT token generator."""
    generator = AsyncMock()
    generator.generate_access_token = AsyncMock(return_value="access_token_123")
    generator.generate_refresh_token = AsyncMock(return_value="refresh_token_456")
    generator.validate_refresh_token = AsyncMock(
        return_value={"sub": "user_123", "type": "refresh"}
    )
    generator.revoke_access_token = AsyncMock()
    generator.revoke_refresh_token = AsyncMock()
    return generator


@pytest.fixture
def mock_settings():
    """Mock OAuth settings."""
    settings = Mock()
    settings.oauth_allowed_clients = ["faultmaven-copilot", "test-client"]
    settings.oauth_code_expiry_seconds = 600  # 10 minutes
    settings.oauth_redirect_uri_patterns = [
        r"^chrome-extension://[a-z0-9]+/callback\.html$",
        r"^https://dashboard\.faultmaven\.ai/auth/callback$",
    ]
    settings.jwt_access_token_expire_minutes = 60
    settings.jwt_refresh_token_expire_days = 7
    return settings


@pytest.fixture
def oauth_service(
    mock_code_repository, mock_user_repository, mock_token_generator, mock_settings
):
    """Create OAuth service with mocked dependencies."""
    return OAuthServiceImpl(
        code_repository=mock_code_repository,
        user_repository=mock_user_repository,
        token_generator=mock_token_generator,
        settings=mock_settings,
    )


@pytest.fixture
def mock_user():
    """Mock user object."""
    user = Mock()
    user.user_id = "user_123"
    user.username = "testuser"
    return user


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


class TestAuthorizationCodeGeneration:
    """Test authorization code generation."""

    @pytest.mark.asyncio
    async def test_create_authorization_code_success(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test successful authorization code generation."""
        code_verifier, code_challenge = pkce_pair

        auth_request = OAuthAuthorizationDTO(
            client_id="faultmaven-copilot",
            redirect_uri="chrome-extension://abc123/callback.html",
            state="random_state_123",
            code_challenge=code_challenge,
            code_challenge_method="S256",
            scope="openid profile email",
        )

        code = await oauth_service.create_authorization_code("user_123", auth_request)

        # Verify code is generated (32 bytes = 44 characters base64url)
        assert len(code) == 43  # 32 bytes base64url encoded without padding
        assert all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for c in code
        )

        # Verify code saved to repository
        mock_code_repository.save_code.assert_called_once()
        saved_code_data = mock_code_repository.save_code.call_args[0][0]

        assert saved_code_data.code == code
        assert saved_code_data.user_id == "user_123"
        assert saved_code_data.redirect_uri == "chrome-extension://abc123/callback.html"
        assert saved_code_data.code_challenge == code_challenge
        assert saved_code_data.used is False

    @pytest.mark.asyncio
    async def test_create_authorization_code_invalid_client(
        self, oauth_service, pkce_pair
    ):
        """Test authorization code generation with invalid client."""
        code_verifier, code_challenge = pkce_pair

        auth_request = OAuthAuthorizationDTO(
            client_id="invalid-client",  # Not in allowed clients
            redirect_uri="chrome-extension://abc123/callback.html",
            state="random_state_123",
            code_challenge=code_challenge,
            code_challenge_method="S256",
            scope="openid profile email",
        )

        with pytest.raises(InvalidRequestError, match="Unknown client_id"):
            await oauth_service.create_authorization_code("user_123", auth_request)

    @pytest.mark.asyncio
    async def test_create_authorization_code_unsupported_challenge_method(
        self, oauth_service, pkce_pair
    ):
        """Test authorization code generation with unsupported PKCE method."""
        code_verifier, code_challenge = pkce_pair

        auth_request = OAuthAuthorizationDTO(
            client_id="faultmaven-copilot",
            redirect_uri="chrome-extension://abc123/callback.html",
            state="random_state_123",
            code_challenge=code_challenge,
            code_challenge_method="plain",  # Not supported
            scope="openid profile email",
        )

        with pytest.raises(
            InvalidRequestError, match="Unsupported code_challenge_method"
        ):
            await oauth_service.create_authorization_code("user_123", auth_request)


class TestPKCEVerification:
    """Test PKCE code_challenge and code_verifier verification."""

    def test_pkce_verification_success(self, oauth_service, pkce_pair):
        """Test successful PKCE verification."""
        code_verifier, code_challenge = pkce_pair

        result = oauth_service._verify_pkce(code_verifier, code_challenge)
        assert result is True

    def test_pkce_verification_invalid_verifier(self, oauth_service, pkce_pair):
        """Test PKCE verification with invalid verifier."""
        _, code_challenge = pkce_pair
        invalid_verifier = "invalid_verifier_123"

        result = oauth_service._verify_pkce(invalid_verifier, code_challenge)
        assert result is False

    def test_pkce_verification_wrong_verifier(self, oauth_service, pkce_pair):
        """Test PKCE verification with wrong (but valid) verifier."""
        code_verifier, code_challenge = pkce_pair

        # Generate different verifier
        wrong_verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode("utf-8")
            .rstrip("=")
        )

        result = oauth_service._verify_pkce(wrong_verifier, code_challenge)
        assert result is False


class TestCodeExchange:
    """Test authorization code exchange for tokens."""

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_success(
        self,
        oauth_service,
        mock_code_repository,
        mock_user_repository,
        mock_token_generator,
        mock_user,
        pkce_pair,
    ):
        """Test successful code exchange for tokens."""
        code_verifier, code_challenge = pkce_pair
        authorization_code = "auth_code_123"

        # Mock stored code data
        code_data = OAuthCodeDTO(
            code=authorization_code,
            user_id="user_123",
            redirect_uri="chrome-extension://abc123/callback.html",
            code_challenge=code_challenge,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            used=False,
        )
        mock_code_repository.get_code.return_value = code_data

        # Create a real user-like object instead of mock
        user_obj = Mock()
        user_obj.user_id = "user_123"
        user_obj.username = "testuser"
        mock_user_repository.get.return_value = user_obj

        # Exchange code for tokens
        token_dto = await oauth_service.exchange_code_for_token(
            code=authorization_code,
            code_verifier=code_verifier,
            redirect_uri="chrome-extension://abc123/callback.html",
        )

        # Verify token response
        assert token_dto.access_token == "access_token_123"
        assert token_dto.refresh_token == "refresh_token_456"
        assert token_dto.user_id == "user_123"
        assert token_dto.username == "testuser"

        # Verify code marked as used
        mock_code_repository.mark_code_used.assert_called_once_with(authorization_code)

        # Verify tokens generated (with the user object returned by repository)
        mock_token_generator.generate_access_token.assert_called_once_with(user_obj)
        mock_token_generator.generate_refresh_token.assert_called_once_with(user_obj)

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_invalid_code(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test code exchange with invalid/expired code."""
        code_verifier, code_challenge = pkce_pair
        mock_code_repository.get_code.return_value = None

        with pytest.raises(
            InvalidGrantError, match="Invalid or expired authorization code"
        ):
            await oauth_service.exchange_code_for_token(
                code="invalid_code",
                code_verifier=code_verifier,
                redirect_uri="chrome-extension://abc123/callback.html",
            )

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_expired_code(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test code exchange with expired code."""
        code_verifier, code_challenge = pkce_pair

        expired_code_data = OAuthCodeDTO(
            code="expired_code",
            user_id="user_123",
            redirect_uri="chrome-extension://abc123/callback.html",
            code_challenge=code_challenge,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),  # Expired
            used=False,
        )
        mock_code_repository.get_code.return_value = expired_code_data

        with pytest.raises(InvalidGrantError, match="Authorization code expired"):
            await oauth_service.exchange_code_for_token(
                code="expired_code",
                code_verifier=code_verifier,
                redirect_uri="chrome-extension://abc123/callback.html",
            )

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_already_used(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test code exchange with already-used code."""
        code_verifier, code_challenge = pkce_pair

        used_code_data = OAuthCodeDTO(
            code="used_code",
            user_id="user_123",
            redirect_uri="chrome-extension://abc123/callback.html",
            code_challenge=code_challenge,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            used=True,  # Already used
        )
        mock_code_repository.get_code.return_value = used_code_data

        with pytest.raises(
            InvalidGrantError, match="Authorization code has already been used"
        ):
            await oauth_service.exchange_code_for_token(
                code="used_code",
                code_verifier=code_verifier,
                redirect_uri="chrome-extension://abc123/callback.html",
            )

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_pkce_verification_failed(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test code exchange with invalid PKCE verifier."""
        code_verifier, code_challenge = pkce_pair

        code_data = OAuthCodeDTO(
            code="auth_code_123",
            user_id="user_123",
            redirect_uri="chrome-extension://abc123/callback.html",
            code_challenge=code_challenge,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            used=False,
        )
        mock_code_repository.get_code.return_value = code_data

        # Use wrong verifier
        wrong_verifier = "wrong_verifier_123"

        with pytest.raises(InvalidGrantError, match="PKCE verification failed"):
            await oauth_service.exchange_code_for_token(
                code="auth_code_123",
                code_verifier=wrong_verifier,
                redirect_uri="chrome-extension://abc123/callback.html",
            )

    @pytest.mark.asyncio
    async def test_exchange_code_for_token_redirect_uri_mismatch(
        self, oauth_service, mock_code_repository, pkce_pair
    ):
        """Test code exchange with mismatched redirect URI."""
        code_verifier, code_challenge = pkce_pair

        code_data = OAuthCodeDTO(
            code="auth_code_123",
            user_id="user_123",
            redirect_uri="chrome-extension://abc123/callback.html",
            code_challenge=code_challenge,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            used=False,
        )
        mock_code_repository.get_code.return_value = code_data

        with pytest.raises(InvalidGrantError, match="Redirect URI mismatch"):
            await oauth_service.exchange_code_for_token(
                code="auth_code_123",
                code_verifier=code_verifier,
                redirect_uri="chrome-extension://different/callback.html",  # Different URI
            )


class TestRefreshToken:
    """Test refresh token flow."""

    @pytest.mark.asyncio
    async def test_refresh_access_token_success(
        self,
        oauth_service,
        mock_token_generator,
        mock_user_repository,
        mock_user,
    ):
        """Test successful access token refresh."""
        refresh_token = "refresh_token_456"

        # Mock token validation to return payload dict
        mock_token_generator.validate_refresh_token.return_value = {
            "sub": "user_123",
            "type": "refresh",
        }

        # Create real user-like object
        user_obj = Mock()
        user_obj.user_id = "user_123"
        user_obj.username = "testuser"
        mock_user_repository.get.return_value = user_obj

        token_dto = await oauth_service.refresh_access_token(
            refresh_token=refresh_token,
            client_id="faultmaven-copilot",
        )

        # Verify new tokens generated
        assert token_dto.access_token == "access_token_123"
        assert (
            token_dto.refresh_token == "refresh_token_456"
        )  # New rotated refresh token
        assert token_dto.user_id == "user_123"
        assert token_dto.username == "testuser"

        # Verify refresh token validated
        mock_token_generator.validate_refresh_token.assert_called_once_with(
            refresh_token
        )

    @pytest.mark.asyncio
    async def test_refresh_access_token_invalid_token(
        self, oauth_service, mock_token_generator
    ):
        """Test refresh with invalid token."""
        mock_token_generator.validate_refresh_token.side_effect = InvalidGrantError(
            "Invalid refresh token"
        )

        with pytest.raises(InvalidGrantError, match="Invalid refresh token"):
            await oauth_service.refresh_access_token(
                refresh_token="invalid_token",
                client_id="faultmaven-copilot",
            )


class TestTokenRevocation:
    """Test token revocation."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(self, oauth_service, mock_token_generator):
        """Test access token revocation."""
        access_token = "access_token_123"

        await oauth_service.revoke_token(access_token)

        mock_token_generator.revoke_access_token.assert_called_once_with(access_token)

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self, oauth_service, mock_token_generator):
        """Test refresh token revocation."""
        refresh_token = "refresh_token_456"

        await oauth_service.revoke_refresh_token(refresh_token)

        mock_token_generator.revoke_refresh_token.assert_called_once_with(refresh_token)
