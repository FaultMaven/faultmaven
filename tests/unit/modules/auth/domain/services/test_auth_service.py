"""Unit tests for AuthService (TASK-017)

Tests JWT verification and revocation. AuthService mints nothing: its parallel
token-mint path was dead and was removed in #853, so tokens here are forged by
``tests.utils`` (see the note there) rather than produced by the service.

Test Categories:
1. Token Verification Tests - Signature, expiration, claims validation
2. Token Revocation Tests - revocation via the deployment-wide store (#767)

Coverage Target: 90%+
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from faultmaven.models.rbac import Permission, Role, get_permissions_for_roles
from faultmaven.modules.auth.domain.models.auth import (
    AuthenticatedUser,
    TokenClaims,
    TokenPair,
)
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)
from tests.utils import (
    InMemoryRevocationStore,
    forge_access_token,
    forge_refresh_token,
    sign_claims_for,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    settings = MagicMock()
    settings.auth.auth_mode = "local"  # Default to local mode for tests
    settings.security.jwt_algorithm = "HS256"  # Match local mode default
    settings.auth.jwt_access_token_expire_minutes = 15
    settings.auth.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = "faultmaven-api"
    settings.security.jwt_audience = "faultmaven-app"
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = None
    settings.security.jwt_public_key = None
    settings.security.jwt_private_key_path = None
    settings.security.jwt_public_key_path = None
    settings.security.jwt_secret_key = MagicMock()
    settings.security.jwt_secret_key.get_secret_value.return_value = (
        "test-secret-key-min-32-bytes!!!!!"
    )
    return settings


@pytest.fixture
def revocation_store():
    """In-memory revocation store for revocation testing."""
    return InMemoryRevocationStore()


@pytest.fixture
def auth_service(mock_settings):
    """Create AuthService with mocked settings."""
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=mock_settings,
    ):
        service = AuthService()
        return service


@pytest.fixture
def auth_service_with_store(mock_settings, revocation_store):
    """Create AuthService with mocked settings and a revocation store."""
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=mock_settings,
    ):
        service = AuthService(revocation_store=revocation_store)
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
# Token Verification Tests
# ============================================================


class TestTokenVerification:
    """Tests for JWT token verification."""

    def test_verify_valid_access_token(self, auth_service, sample_user_data):
        """verify_token decodes a valid access token."""
        token = forge_access_token(
            auth_service,
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
        token = forge_refresh_token(
            auth_service,
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
            "organization_id": "org-456",
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
        expired_token = sign_claims_for(auth_service, expired_claims)

        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(expired_token, token_type="access")

        assert exc_info.value.error_code == "TOKEN_EXPIRED"

    def test_verify_raises_on_invalid_signature(self, auth_service):
        """verify_token raises AuthenticationError on wrong signature."""
        # Create a token with wrong key
        fake_claims = {
            "sub": "user-123",
            "organization_id": "org-456",
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
        fake_token = jwt.encode(
            fake_claims, "wrong-secret-key-min-32-bytes!!!", algorithm="HS256"
        )

        with pytest.raises(AuthenticationError) as exc_info:
            auth_service.verify_token(fake_token, token_type="access")

        # Using wrong secret with HS256 raises InvalidTokenError (caught by generic handler), not DecodeError
        assert exc_info.value.error_code in ["INVALID_TOKEN", "DECODE_ERROR"]

    def test_verify_raises_on_wrong_token_type_refresh_as_access(
        self, auth_service, sample_user_data
    ):
        """verify_token raises AuthenticationError when refresh token used as access."""
        # Generate refresh token
        token = forge_refresh_token(
            auth_service,
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
        token = forge_refresh_token(
            auth_service,
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

        incomplete_token = sign_claims_for(auth_service, incomplete_claims)

        with pytest.raises(AuthenticationError):
            auth_service.verify_token(incomplete_token, token_type="access")


# ============================================================
# Token Verification with Revocation Tests
# ============================================================


class TestTokenVerificationWithRevocation:
    """Tests for token verification with revocation checking."""

    @pytest.mark.asyncio
    async def test_verify_with_revocation_allows_valid_token(
        self, auth_service_with_store, sample_user_data
    ):
        """Valid non-revoked token passes verification."""
        token = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = await auth_service_with_store.verify_token_with_revocation_check(
            token, token_type="access"
        )

        assert claims is not None
        assert claims["sub"] == sample_user_data["user_id"]

    @pytest.mark.asyncio
    async def test_verify_with_revocation_raises_on_revoked_token(
        self, auth_service_with_store, sample_user_data, revocation_store
    ):
        """Revoked token raises TokenRevocationError."""
        token = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )
        jti = auth_service_with_store.verify_token(token, token_type="access")["jti"]
        await revocation_store.add_revoked_token(jti, 600)

        with pytest.raises(TokenRevocationError):
            await auth_service_with_store.verify_token_with_revocation_check(
                token, token_type="access"
            )

    @pytest.mark.asyncio
    async def test_revocation_check_fails_open_on_store_error(
        self, auth_service_with_store, sample_user_data, revocation_store
    ):
        """Store outage on the request path fails OPEN (documented posture).

        Access tokens are short-lived; availability wins on the per-request
        check. Refresh validation in the generators fails closed instead.
        """
        token = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        async def boom(jti):
            raise ConnectionError("store down")

        revocation_store.is_revoked = boom

        claims = await auth_service_with_store.verify_token_with_revocation_check(
            token, token_type="access"
        )
        assert claims["sub"] == sample_user_data["user_id"]


# ============================================================
# Token Revocation Tests
# ============================================================


class TestTokenRevocation:
    """Tests for token revocation functionality."""

    @pytest.mark.asyncio
    async def test_revoke_token_adds_jti_to_revocation_store(
        self, auth_service_with_store, revocation_store
    ):
        """revoke_token records the jti in the revocation store."""
        jti = str(uuid.uuid4())
        exp = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())

        await auth_service_with_store.revoke_token(jti, exp)

        assert jti in revocation_store.revoked

    @pytest.mark.asyncio
    async def test_revoke_token_sets_correct_ttl(
        self, auth_service_with_store, revocation_store
    ):
        """revoke_token sets TTL matching token expiration."""
        jti = str(uuid.uuid4())
        now = int(datetime.now(timezone.utc).timestamp())
        exp = now + 600  # 10 minutes from now

        await auth_service_with_store.revoke_token(jti, exp)

        assert 595 <= revocation_store.revoked[jti] <= 605

    @pytest.mark.asyncio
    async def test_revoked_tokens_fail_verification(
        self, auth_service_with_store, sample_user_data
    ):
        """A token revoked via revoke_token fails the request-path check."""
        token = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims = auth_service_with_store.verify_token(token, token_type="access")

        # Revoke the token; the same store instance backs the check
        await auth_service_with_store.revoke_token(claims["jti"], claims["exp"])

        with pytest.raises(TokenRevocationError):
            await auth_service_with_store.verify_token_with_revocation_check(
                token, token_type="access"
            )

    @pytest.mark.asyncio
    async def test_multiple_tokens_can_be_revoked(
        self, auth_service_with_store, sample_user_data, revocation_store
    ):
        """Multiple tokens can be revoked independently."""
        token1 = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        token2 = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        claims1 = auth_service_with_store.verify_token(token1, token_type="access")
        claims2 = auth_service_with_store.verify_token(token2, token_type="access")

        await auth_service_with_store.revoke_token(claims1["jti"], claims1["exp"])
        await auth_service_with_store.revoke_token(claims2["jti"], claims2["exp"])

        assert len(revocation_store.revoked) == 2

    @pytest.mark.asyncio
    async def test_revoke_token_raises_on_store_failure(
        self, auth_service_with_store, revocation_store
    ):
        """Revocation writes fail CLOSED: a store error surfaces as ServiceError."""
        from faultmaven.exceptions import ServiceError

        async def boom(jti, ttl):
            raise ConnectionError("store down")

        revocation_store.add_revoked_token = boom

        with pytest.raises(ServiceError):
            exp = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())
            await auth_service_with_store.revoke_token(str(uuid.uuid4()), exp)

    @pytest.mark.asyncio
    async def test_revoke_token_raises_without_store(self, auth_service):
        """revoke_token must not silently succeed with no store configured."""
        from faultmaven.exceptions import ServiceError

        exp = int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp())
        with pytest.raises(ServiceError):
            await auth_service.revoke_token(str(uuid.uuid4()), exp)


# ============================================================
# Extract User Tests
# ============================================================


class TestExtractUser:
    """Tests for extracting AuthenticatedUser from token."""

    def test_extract_user_from_token(self, auth_service, sample_user_data):
        """extract_user_from_token returns AuthenticatedUser."""
        token = forge_access_token(
            auth_service,
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
        token = forge_access_token(
            auth_service,
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
        self, auth_service_with_store, sample_user_data
    ):
        """extract_user_from_token_with_revocation_check works correctly."""
        token = forge_access_token(
            auth_service_with_store,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=sample_user_data["roles"],
        )

        user = (
            await auth_service_with_store.extract_user_from_token_with_revocation_check(
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
        token = forge_access_token(
            auth_service,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email=sample_user_data["email"],
            roles=[],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["roles"] == []

    def test_special_characters_in_email(self, auth_service, sample_user_data):
        """Token handles special characters in email."""
        token = forge_access_token(
            auth_service,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email="test+tag@example.com",
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["email"] == "test+tag@example.com"

    def test_unicode_in_claims(self, auth_service, sample_user_data):
        """Token handles unicode characters."""
        token = forge_access_token(
            auth_service,
            user_id=sample_user_data["user_id"],
            organization_id=sample_user_data["organization_id"],
            email="тест@example.com",
            roles=sample_user_data["roles"],
        )

        claims = auth_service.verify_token(token, token_type="access")
        assert claims["email"] == "тест@example.com"

    def test_multiple_roles(self, auth_service, sample_user_data):
        """Token handles multiple roles."""
        token = forge_access_token(
            auth_service,
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
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
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
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()

            # Verify keys were loaded from files
            assert service._private_key == private_pem
            assert service._public_key == public_pem

    def test_load_keys_generates_dev_keys_when_not_configured(self, mock_settings):
        """Development RSA keys are generated when no keys are configured."""
        # No keys configured
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            with patch(
                "faultmaven.modules.auth.domain.services.auth_service.logger"
            ) as mock_logger:
                service = AuthService()

                # Verify warning was logged about dev keys
                warning_calls = [
                    str(call) for call in mock_logger.warning.call_args_list
                ]
                assert any(
                    "Generated development RSA keys" in call for call in warning_calls
                )

        # Verify dev keys were generated
        assert service._private_key is not None
        assert service._public_key is not None
        assert "-----BEGIN PRIVATE KEY-----" in service._private_key
        assert "-----BEGIN PUBLIC KEY-----" in service._public_key

    def test_load_keys_warns_on_missing_key_file(self, mock_settings, tmp_path):
        """Warning is logged when key file does not exist."""
        # Point to non-existent file
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = "/nonexistent/private.pem"
        mock_settings.security.jwt_public_key_path = "/nonexistent/public.pem"

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            with patch(
                "faultmaven.modules.auth.domain.services.auth_service.logger"
            ) as mock_logger:
                service = AuthService()

                # Verify warnings were logged for missing files
                warning_calls = [
                    str(call) for call in mock_logger.warning.call_args_list
                ]
                assert any(
                    "Private key file not found" in call for call in warning_calls
                )
                assert any(
                    "Public key file not found" in call for call in warning_calls
                )

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
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
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
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
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
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()

            # Generate a token
            token = forge_access_token(
                service,
                user_id="user-123",
                organization_id="org-456",
                email="test@example.com",
                roles=["admin"],
            )

            # Verify the token
            claims = service.verify_token(token, token_type="access")

            assert claims["sub"] == "user-123"
            assert claims["organization_id"] == "org-456"
            assert claims["email"] == "test@example.com"


# ============================================================
# Algorithm Selection Tests
# ============================================================


class TestAlgorithmSelection:
    """Tests for _algorithm property - AUTH_MODE-based algorithm selection."""

    def test_algorithm_uses_hs256_for_local_auth_mode(self, mock_settings):
        """Algorithm is HS256 when AUTH_MODE=local."""
        # Configure local auth mode
        mock_settings.auth.auth_mode = "local"
        mock_settings.security.jwt_secret_key = MagicMock()
        mock_settings.security.jwt_secret_key.get_secret_value.return_value = (
            "test-secret-key-min-32-bytes!!!!!"
        )
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()
            assert service._algorithm == "HS256"

    def test_algorithm_uses_rs256_for_oauth_auth_mode(self, mock_settings):
        """Algorithm is RS256 when AUTH_MODE=oauth."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test RSA keys
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

        # Configure OAuth auth mode with RSA keys
        mock_settings.auth.auth_mode = "oauth"
        mock_settings.security.jwt_private_key = MagicMock()
        mock_settings.security.jwt_private_key.get_secret_value.return_value = (
            private_pem
        )
        mock_settings.security.jwt_public_key = public_pem
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()
            assert service._algorithm == "RS256"

    def test_algorithm_respects_auth_mode_over_rsa_key_presence(self, mock_settings):
        """AUTH_MODE takes precedence over RSA key availability."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test RSA keys
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

        # Configure local auth mode BUT with RSA keys present
        # This simulates having RSA keys configured for future OAuth use
        mock_settings.auth.auth_mode = "local"
        mock_settings.security.jwt_secret_key = MagicMock()
        mock_settings.security.jwt_secret_key.get_secret_value.return_value = (
            "test-secret-key-min-32-bytes!!!!!"
        )
        mock_settings.security.jwt_private_key = MagicMock()
        mock_settings.security.jwt_private_key.get_secret_value.return_value = (
            private_pem
        )
        mock_settings.security.jwt_public_key = public_pem
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()
            # Despite RSA keys being present, should use HS256 because AUTH_MODE=local
            assert service._algorithm == "HS256"

    def test_local_mode_token_generation_and_validation(self, mock_settings):
        """Tokens generated and validated with HS256 in local mode."""
        # Configure local auth mode
        mock_settings.auth.auth_mode = "local"
        mock_settings.security.jwt_secret_key = MagicMock()
        mock_settings.security.jwt_secret_key.get_secret_value.return_value = (
            "test-secret-key-for-hs256-32b!!!"
        )
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = None
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()

            # Generate token
            token = forge_access_token(
                service,
                user_id="user-123",
                organization_id="org-456",
                email="test@example.com",
                roles=["admin"],
            )

            # Verify token can be decoded
            claims = service.verify_token(token, token_type="access")
            assert claims["sub"] == "user-123"

            # Verify token header uses HS256
            import jwt as pyjwt

            header = pyjwt.get_unverified_header(token)
            assert header["alg"] == "HS256"

    def test_oauth_mode_token_generation_and_validation(self, mock_settings):
        """Tokens generated and validated with RS256 in OAuth mode."""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate test RSA keys
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

        # Configure OAuth auth mode
        mock_settings.auth.auth_mode = "oauth"
        mock_settings.security.jwt_private_key = MagicMock()
        mock_settings.security.jwt_private_key.get_secret_value.return_value = (
            private_pem
        )
        mock_settings.security.jwt_public_key = public_pem
        mock_settings.security.jwt_private_key_path = None
        mock_settings.security.jwt_public_key_path = None

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService()

            # Generate token
            token = forge_access_token(
                service,
                user_id="user-123",
                organization_id="org-456",
                email="test@example.com",
                roles=["admin"],
            )

            # Verify token can be decoded
            claims = service.verify_token(token, token_type="access")
            assert claims["sub"] == "user-123"

            # Verify token header uses RS256
            import jwt as pyjwt

            header = pyjwt.get_unverified_header(token)
            assert header["alg"] == "RS256"


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
            "organization_id": "org-456",
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

        # Configure service with test keys and RS256 algorithm
        mock_settings.auth.auth_mode = "oauth"  # Use oauth mode for RS256
        mock_settings.security.jwt_private_key = None
        mock_settings.security.jwt_public_key = public_pem

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
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
            "organization_id": "org-456",
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

        # Configure service for RS256 algorithm
        mock_settings.auth.auth_mode = "oauth"  # Use oauth mode for RS256

        with patch(
            "faultmaven.modules.auth.domain.services.auth_service.get_settings",
            return_value=mock_settings,
        ):
            service = AuthService(private_key=private_pem, public_key=public_pem)

            with pytest.raises(AuthenticationError) as exc_info:
                service.verify_token(token_with_wrong_audience, token_type="access")

            assert exc_info.value.error_code == "INVALID_AUDIENCE"
