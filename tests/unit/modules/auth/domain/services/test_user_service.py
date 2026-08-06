"""Unit tests for UserService (TASK-018).

Tests for:
- User registration with validation
- Password reset workflow
- Password change
- Profile updates
- User deactivation

Coverage target: 90%+
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import ConflictError, NotFoundError, ValidationException
from faultmaven.infrastructure.persistence.user_repository import InMemoryUserRepository
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
)
from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.utils.password import verify_password

RESET_TEST_SECRET = "unit-test-secret-key-please-ignore"


def _hs256_generator():
    """The real local-mode signer, holding its own secret.

    A real generator rather than a mock: the reset surface moved onto it in
    #959 precisely because a fabricated key state (an RSA PEM on an HMAC
    algorithm) is unreachable in production but trivially constructible in a
    fixture, and the mismatch it hides only shows up when something signs.
    """
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        HS256JWTTokenGenerator,
    )
    from tests.utils import InMemoryRevocationStore

    return HS256JWTTokenGenerator(
        secret_key=RESET_TEST_SECRET,
        revocation_store=InMemoryRevocationStore(),
        access_token_expire_minutes=60,
        refresh_token_expire_days=7,
        issuer="faultmaven",
        audience="faultmaven-api",
    )


@pytest.fixture
def mock_auth_service():
    """Create a mock AuthService."""
    auth_service = MagicMock(spec=AuthService)
    # No signing keys stubbed here: since #959 nothing outside
    # `IJWTTokenGenerator` signs, so the auth service is asked only about
    # revocation. A key on this double would be fixture state nothing reads.
    # `spec=` rather than `spec_set=`, which would have caught the stale
    # `_access_token_expire_minutes` stub #853 left here: both keys above are
    # INSTANCE attributes assigned in `AuthService.__init__`, so they are absent
    # from the class `spec_set` builds its allowlist from and setting either
    # raises. Tightening here would refuse two live stubs to catch one dead one.
    # Returns the revocation watermark (#769), not a token count.
    auth_service.revoke_user_tokens = AsyncMock(
        return_value=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    )
    # Nothing revoked by default. Stubbed explicitly because an unstubbed async
    # mock returns a truthy Mock, which every revocation check reads as REVOKED.
    auth_service.get_revocation_reason = AsyncMock(return_value=None)
    return auth_service


@pytest.fixture
def user_repo():
    """Create a fresh InMemoryUserRepository."""
    return InMemoryUserRepository()


@pytest.fixture
def user_service(user_repo, mock_auth_service):
    """Create UserService with mocked dependencies."""
    import fakeredis.aioredis as fakeredis_aio

    with patch(
        "faultmaven.modules.auth.domain.services.user_service.get_settings"
    ) as mock_settings:
        mock_settings.return_value.security.jwt_issuer = "faultmaven"
        mock_settings.return_value.security.jwt_audience = "faultmaven-api"
        mock_settings.return_value.security.jwt_algorithm = "HS256"
        service = UserService(
            user_repo=user_repo,
            auth_service=mock_auth_service,
            token_generator=_hs256_generator(),
        )
        # Redis is always available (FakeRedis for tests)
        service.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
        return service


@pytest.fixture
def registered_user(user_repo):
    """Create and save a registered user in the repository."""
    from faultmaven.utils.password import hash_password

    now = datetime.now(timezone.utc)
    user = RepositoryUser(
        user_id=str(uuid.uuid4()),
        username="existinguser",
        email="existing@example.com",
        display_name="Existing User",
        hashed_password=hash_password("ExistingP@ss123"),
        is_active=True,
        is_email_verified=False,
        created_at=now,
        updated_at=now,
        roles=["member"],
    )
    # Synchronously add to repository
    import asyncio

    asyncio.get_event_loop().run_until_complete(user_repo.save(user))
    return user


class TestRegisterUser:
    """Tests for UserService.register_user()."""

    @pytest.mark.asyncio
    async def test_register_creates_user(self, user_service, user_repo):
        """Registration should create a new user."""
        user = await user_service.register_user(
            email="newuser@example.com",
            password="TestP@ssw0rd!",
            full_name="New User",
        )
        assert user is not None
        assert user.email == "newuser@example.com"
        assert user.display_name == "New User"

    @pytest.mark.asyncio
    async def test_register_hashes_password(self, user_service, user_repo):
        """Registration should hash the password with bcrypt."""
        user = await user_service.register_user(
            email="newuser@example.com",
            password="TestP@ssw0rd!",
            full_name="New User",
        )
        assert user.hashed_password is not None
        assert user.hashed_password.startswith("$2")
        assert user.hashed_password != "TestP@ssw0rd!"

    @pytest.mark.asyncio
    async def test_register_sets_default_values(self, user_service, user_repo):
        """Registration should set correct default values."""
        user = await user_service.register_user(
            email="newuser@example.com",
            password="TestP@ssw0rd!",
            full_name="New User",
        )
        assert user.is_active is True
        assert user.is_email_verified is False
        assert "member" in user.roles

    @pytest.mark.asyncio
    async def test_register_validates_email_format(self, user_service):
        """Registration should reject invalid email format."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="invalid-email",
                password="TestP@ssw0rd!",
                full_name="New User",
            )
        assert "email" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_register_validates_password_length(self, user_service):
        """Registration should reject short passwords."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="Sh0rt!",
                full_name="New User",
            )
        assert "8 characters" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_validates_password_uppercase(self, user_service):
        """Registration should reject passwords without uppercase."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="testp@ssw0rd!",
                full_name="New User",
            )
        assert "uppercase" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_validates_password_lowercase(self, user_service):
        """Registration should reject passwords without lowercase."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="TESTP@SSW0RD!",
                full_name="New User",
            )
        assert "lowercase" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_validates_password_digit(self, user_service):
        """Registration should reject passwords without digit."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="TestP@ssword!",
                full_name="New User",
            )
        assert "digit" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_validates_password_special(self, user_service):
        """Registration should reject passwords without special char."""
        with pytest.raises(ValidationException) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="TestPassw0rd",
                full_name="New User",
            )
        assert "special character" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_rejects_duplicate_email(self, user_service, user_repo):
        """Registration should reject duplicate email."""
        await user_service.register_user(
            email="user@example.com",
            password="TestP@ssw0rd!",
            full_name="First User",
        )

        with pytest.raises(ConflictError) as excinfo:
            await user_service.register_user(
                email="user@example.com",
                password="OtherP@ssw0rd!",
                full_name="Second User",
            )
        assert "Email already registered" in str(excinfo.value)


class TestRequestPasswordReset:
    """Tests for UserService.request_password_reset()."""

    @pytest.mark.asyncio
    async def test_reset_request_returns_token(self, user_service, user_repo):
        """Password reset request should return a token."""
        # Register user
        await user_service.register_user(
            email="reset@example.com",
            password="TestP@ssw0rd!",
            full_name="Reset User",
        )

        token = await user_service.request_password_reset(email="reset@example.com")

        # Verified through the generator that signed it, which is the only
        # thing holding the key (#959).
        claims = await user_service.token_generator.verify_password_reset_token(token)
        assert claims["type"] == "password_reset"

    @pytest.mark.asyncio
    async def test_reset_request_returns_token_for_nonexistent(self, user_service):
        """Reset request should return token even for non-existent email (enumeration prevention)."""
        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            PasswordResetMint,
        )

        with patch.object(
            user_service.token_generator,
            "generate_dummy_reset_token",
            new_callable=AsyncMock,
        ) as mock_dummy:
            # A decoy is a mint like any other: the caller files a single-use
            # key for it, so a store fault cannot separate it from a real one.
            mock_dummy.return_value = PasswordResetMint(
                token="dummy-token", jti="dummy-jti", subject="dummy-subject"
            )
            token = await user_service.request_password_reset(
                email="nonexistent@example.com"
            )
            assert token == "dummy-token"

    @pytest.mark.asyncio
    async def test_reset_request_generates_jwt_token(self, user_service, user_repo):
        """Reset request should generate a JWT token."""
        # Register user
        await user_service.register_user(
            email="resetjwt@example.com",
            password="TestP@ssw0rd!",
            full_name="Reset JWT User",
        )

        token = await user_service.request_password_reset(email="resetjwt@example.com")
        # JWT tokens have 3 parts separated by dots
        assert token is not None
        assert token.count(".") == 2

    @pytest.mark.asyncio
    async def test_reset_request_token_contains_user_id(self, user_service, user_repo):
        """Reset token should contain user_id in claims."""
        import jwt

        # Register user
        user = await user_service.register_user(
            email="resettok@example.com",
            password="TestP@ssw0rd!",
            full_name="Reset Token User",
        )

        token = await user_service.request_password_reset(email="resettok@example.com")

        # Decode without verification to check claims
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims.get("sub") == user.user_id

    @pytest.mark.asyncio
    async def test_reset_request_token_has_expiration(self, user_service, user_repo):
        """Reset token should have expiration time."""
        import jwt

        # Register user
        await user_service.register_user(
            email="resetexp@example.com",
            password="TestP@ssw0rd!",
            full_name="Reset Exp User",
        )

        token = await user_service.request_password_reset(email="resetexp@example.com")

        # Decode without verification to check claims
        claims = jwt.decode(token, options={"verify_signature": False})
        assert "exp" in claims

    @pytest.mark.asyncio
    async def test_reset_request_token_has_type(self, user_service, user_repo):
        """Reset token should have password_reset type."""
        import jwt

        # Register user
        await user_service.register_user(
            email="resettype@example.com",
            password="TestP@ssw0rd!",
            full_name="Reset Type User",
        )

        token = await user_service.request_password_reset(email="resettype@example.com")

        # Decode without verification to check claims
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims.get("type") == "password_reset"

    @pytest.mark.asyncio
    async def test_reset_request_case_insensitive_email(self, user_service, user_repo):
        """Reset request should work with different email case."""
        # Register with lowercase
        await user_service.register_user(
            email="casetest@example.com",
            password="TestP@ssw0rd!",
            full_name="Case Test User",
        )

        # Request with uppercase
        token = await user_service.request_password_reset(email="CASETEST@EXAMPLE.COM")
        assert token is not None
        assert token.count(".") == 2  # Valid JWT


class TestResetPassword:
    """Tests for UserService.reset_password()."""

    @pytest.mark.asyncio
    async def test_reset_password_updates_hash(self, user_service, user_repo):
        """Password reset should update the password hash."""
        # Register user
        user = await user_service.register_user(
            email="reset@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset User",
        )
        old_hash = user.hashed_password

        # Seed the reset token in Redis (normally done by request_password_reset)
        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )

        # Mock token verification
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            updated_user = await user_service.reset_password(
                reset_token="valid-token",
                new_password="NewP@ssw0rd!",
            )

            assert updated_user.hashed_password != old_hash

    @pytest.mark.asyncio
    async def test_reset_password_validates_strength(self, user_service, user_repo):
        """Password reset should validate new password strength."""
        user = await user_service.register_user(
            email="reset@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset User",
        )

        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            with pytest.raises(ValidationException):
                await user_service.reset_password(
                    reset_token="valid-token",
                    new_password="weak",
                )

    @pytest.mark.asyncio
    async def test_reset_password_revokes_tokens(
        self, user_service, user_repo, mock_auth_service
    ):
        """Password reset should revoke all user tokens."""
        user = await user_service.register_user(
            email="reset@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset User",
        )

        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            await user_service.reset_password(
                reset_token="valid-token",
                new_password="NewP@ssw0rd!",
            )

            mock_auth_service.revoke_user_tokens.assert_called_with(user.user_id)

    @pytest.mark.asyncio
    async def test_reset_password_rejects_invalid_token(self, user_service):
        """Password reset should reject invalid token."""
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.side_effect = Exception("Invalid token")

            with pytest.raises(AuthenticationError) as excinfo:
                await user_service.reset_password(
                    reset_token="invalid-token",
                    new_password="NewP@ssw0rd!",
                )
            assert "INVALID_RESET_TOKEN" in excinfo.value.error_code

    @pytest.mark.asyncio
    async def test_reset_password_user_can_login_after(self, user_service, user_repo):
        """After reset, user should be able to login with new password."""
        user = await user_service.register_user(
            email="resetlogin@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset Login User",
        )

        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            await user_service.reset_password(
                reset_token="valid-token",
                new_password="NewP@ssw0rd!",
            )

        # The stored credential is now the new password.
        stored = await user_repo.get_by_email("resetlogin@example.com")
        assert verify_password("NewP@ssw0rd!", stored.hashed_password)

    @pytest.mark.asyncio
    async def test_reset_password_old_password_fails(self, user_service, user_repo):
        """After reset, old password should fail authentication."""
        user = await user_service.register_user(
            email="resetoldfail@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset Old Fail User",
        )

        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            await user_service.reset_password(
                reset_token="valid-token",
                new_password="NewP@ssw0rd!",
            )

        # The old password no longer matches the stored credential.
        stored = await user_repo.get_by_email("resetoldfail@example.com")
        assert not verify_password("OldP@ssw0rd!", stored.hashed_password)

    @pytest.mark.asyncio
    async def test_reset_password_updates_updated_at(self, user_service, user_repo):
        """Password reset should update the updated_at timestamp."""
        # Register user
        user = await user_service.register_user(
            email="resetupdated@example.com",
            password="OldP@ssw0rd!",
            full_name="Reset Updated User",
        )
        original_updated = user.updated_at

        import asyncio

        await asyncio.sleep(0.01)

        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, user.user_id
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": user.user_id,
                "type": "password_reset",
                "jti": "test-jti",
            }

            updated_user = await user_service.reset_password(
                reset_token="valid-token",
                new_password="NewP@ssw0rd!",
            )

            assert updated_user.updated_at >= original_updated

    @pytest.mark.asyncio
    async def test_reset_password_nonexistent_user(self, user_service):
        """Password reset should fail for non-existent user."""
        await user_service.redis_client.setex(
            "password_reset:test-jti", 3600, "nonexistent-user-id"
        )
        with patch.object(
            user_service.token_generator,
            "verify_password_reset_token",
            new_callable=AsyncMock,
        ) as mock_verify:
            mock_verify.return_value = {
                "sub": "nonexistent-user-id",
                "type": "password_reset",
                "jti": "test-jti",
            }

            with pytest.raises((AuthenticationError, NotFoundError)):
                await user_service.reset_password(
                    reset_token="valid-token",
                    new_password="NewP@ssw0rd!",
                )


class TestChangePassword:
    """Tests for UserService.change_password()."""

    @pytest.mark.asyncio
    async def test_change_password_updates_hash(self, user_service, user_repo):
        """Password change should update the password hash."""
        user = await user_service.register_user(
            email="change@example.com",
            password="OldP@ssw0rd!",
            full_name="Change User",
        )
        old_hash = user.hashed_password

        updated_user = await user_service.change_password(
            user_id=user.user_id,
            current_password="OldP@ssw0rd!",
            new_password="NewP@ssw0rd!",
        )

        assert updated_user.hashed_password != old_hash

    @pytest.mark.asyncio
    async def test_change_password_verifies_current(self, user_service, user_repo):
        """Password change should verify current password."""
        user = await user_service.register_user(
            email="change@example.com",
            password="OldP@ssw0rd!",
            full_name="Change User",
        )

        with pytest.raises(AuthenticationError) as excinfo:
            await user_service.change_password(
                user_id=user.user_id,
                current_password="WrongP@ssw0rd!",
                new_password="NewP@ssw0rd!",
            )
        assert "INVALID_PASSWORD" in excinfo.value.error_code

    @pytest.mark.asyncio
    async def test_change_password_validates_new(self, user_service, user_repo):
        """Password change should validate new password strength."""
        user = await user_service.register_user(
            email="change@example.com",
            password="OldP@ssw0rd!",
            full_name="Change User",
        )

        with pytest.raises(ValidationException):
            await user_service.change_password(
                user_id=user.user_id,
                current_password="OldP@ssw0rd!",
                new_password="weak",
            )

    @pytest.mark.asyncio
    async def test_change_password_revokes_tokens(
        self, user_service, user_repo, mock_auth_service
    ):
        """Password change should revoke all user tokens."""
        user = await user_service.register_user(
            email="change@example.com",
            password="OldP@ssw0rd!",
            full_name="Change User",
        )

        await user_service.change_password(
            user_id=user.user_id,
            current_password="OldP@ssw0rd!",
            new_password="NewP@ssw0rd!",
        )

        mock_auth_service.revoke_user_tokens.assert_called_with(user.user_id)

    @pytest.mark.asyncio
    async def test_change_password_nonexistent_user(self, user_service):
        """Password change should raise for non-existent user."""
        with pytest.raises(NotFoundError):
            await user_service.change_password(
                user_id="nonexistent-id",
                current_password="OldP@ssw0rd!",
                new_password="NewP@ssw0rd!",
            )


class TestUpdateUserProfile:
    """Tests for UserService.update_user_profile()."""

    @pytest.mark.asyncio
    async def test_update_profile_changes_name(self, user_service, user_repo):
        """Profile update should change full name."""
        user = await user_service.register_user(
            email="profile@example.com",
            password="TestP@ssw0rd!",
            full_name="Original Name",
        )

        updated = await user_service.update_user_profile(
            user_id=user.user_id,
            full_name="New Name",
        )

        assert updated.display_name == "New Name"

    @pytest.mark.asyncio
    async def test_update_profile_changes_email(self, user_service, user_repo):
        """Profile update should change email."""
        user = await user_service.register_user(
            email="original@example.com",
            password="TestP@ssw0rd!",
            full_name="Test User",
        )

        updated = await user_service.update_user_profile(
            user_id=user.user_id,
            email="new@example.com",
        )

        assert updated.email == "new@example.com"

    @pytest.mark.asyncio
    async def test_update_profile_email_change_unverifies(
        self, user_service, user_repo
    ):
        """Email change should set is_verified to False."""
        user = await user_service.register_user(
            email="original@example.com",
            password="TestP@ssw0rd!",
            full_name="Test User",
        )
        # Manually verify email
        user.is_email_verified = True
        await user_repo.save(user)

        updated = await user_service.update_user_profile(
            user_id=user.user_id,
            email="new@example.com",
        )

        assert updated.is_email_verified is False

    @pytest.mark.asyncio
    async def test_update_profile_rejects_duplicate_email(
        self, user_service, user_repo
    ):
        """Profile update should reject duplicate email."""
        user1 = await user_service.register_user(
            email="user1@example.com",
            password="TestP@ssw0rd!",
            full_name="User 1",
        )
        user2 = await user_service.register_user(
            email="user2@example.com",
            password="TestP@ssw0rd!",
            full_name="User 2",
        )

        with pytest.raises(ConflictError):
            await user_service.update_user_profile(
                user_id=user1.user_id,
                email="user2@example.com",  # Already used by user2
            )

    @pytest.mark.asyncio
    async def test_update_profile_validates_email(self, user_service, user_repo):
        """Profile update should validate email format."""
        user = await user_service.register_user(
            email="valid@example.com",
            password="TestP@ssw0rd!",
            full_name="Test User",
        )

        with pytest.raises(ValidationException):
            await user_service.update_user_profile(
                user_id=user.user_id,
                email="invalid-email",
            )

    @pytest.mark.asyncio
    async def test_update_profile_nonexistent_user(self, user_service):
        """Profile update should raise for non-existent user."""
        with pytest.raises(NotFoundError):
            await user_service.update_user_profile(
                user_id="nonexistent-id",
                full_name="New Name",
            )


class TestDeactivateUser:
    """Tests for UserService.deactivate_user()."""

    @pytest.mark.asyncio
    async def test_deactivate_sets_inactive(self, user_service, user_repo):
        """Deactivation should set is_active to False."""
        user = await user_service.register_user(
            email="deactivate@example.com",
            password="TestP@ssw0rd!",
            full_name="Deactivate User",
        )

        deactivated = await user_service.deactivate_user(user.user_id)
        assert deactivated.is_active is False

    @pytest.mark.asyncio
    async def test_deactivate_sets_deleted_at(self, user_service, user_repo):
        """Deactivation should set deleted_at timestamp."""
        user = await user_service.register_user(
            email="deactivate@example.com",
            password="TestP@ssw0rd!",
            full_name="Deactivate User",
        )

        deactivated = await user_service.deactivate_user(user.user_id)
        assert deactivated.deleted_at is not None

    @pytest.mark.asyncio
    async def test_deactivate_revokes_tokens(
        self, user_service, user_repo, mock_auth_service
    ):
        """Deactivation should revoke all user tokens."""
        user = await user_service.register_user(
            email="deactivate@example.com",
            password="TestP@ssw0rd!",
            full_name="Deactivate User",
        )

        await user_service.deactivate_user(user.user_id)
        mock_auth_service.revoke_user_tokens.assert_called_with(user.user_id)

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent_user(self, user_service):
        """Deactivation should raise for non-existent user."""
        with pytest.raises(NotFoundError):
            await user_service.deactivate_user("nonexistent-id")


class TestActivateUser:
    """Tests for UserService.activate_user()."""

    @pytest.mark.asyncio
    async def test_activate_sets_active(self, user_service, user_repo):
        """Activation should set is_active to True."""
        user = await user_service.register_user(
            email="activate@example.com",
            password="TestP@ssw0rd!",
            full_name="Activate User",
        )
        await user_service.deactivate_user(user.user_id)

        activated = await user_service.activate_user(user.user_id)
        assert activated.is_active is True

    @pytest.mark.asyncio
    async def test_activate_clears_deleted_at(self, user_service, user_repo):
        """Activation should clear deleted_at timestamp."""
        user = await user_service.register_user(
            email="activate@example.com",
            password="TestP@ssw0rd!",
            full_name="Activate User",
        )
        await user_service.deactivate_user(user.user_id)

        activated = await user_service.activate_user(user.user_id)
        assert activated.deleted_at is None


class TestGetUser:
    """Tests for UserService.get_user() and get_user_by_email()."""

    @pytest.mark.asyncio
    async def test_get_user_returns_user(self, user_service, user_repo):
        """get_user should return user by ID."""
        user = await user_service.register_user(
            email="getuser@example.com",
            password="TestP@ssw0rd!",
            full_name="Get User",
        )

        found = await user_service.get_user(user.user_id)
        assert found is not None
        assert found.user_id == user.user_id

    @pytest.mark.asyncio
    async def test_get_user_returns_none(self, user_service):
        """get_user should return None for non-existent ID."""
        found = await user_service.get_user("nonexistent-id")
        assert found is None

    @pytest.mark.asyncio
    async def test_get_user_by_email_returns_user(self, user_service, user_repo):
        """get_user_by_email should return user by email."""
        user = await user_service.register_user(
            email="byemail@example.com",
            password="TestP@ssw0rd!",
            full_name="By Email User",
        )

        found = await user_service.get_user_by_email("byemail@example.com")
        assert found is not None
        assert found.email == "byemail@example.com"

    @pytest.mark.asyncio
    async def test_get_user_by_email_returns_none(self, user_service):
        """get_user_by_email should return None for non-existent email."""
        found = await user_service.get_user_by_email("nonexistent@example.com")
        assert found is None


class TestListUsers:
    """Tests for UserService.list_users()."""

    @pytest.mark.asyncio
    async def test_list_users_returns_all(self, user_service, user_repo):
        """list_users should return all users."""
        for i in range(5):
            await user_service.register_user(
                email=f"user{i}@example.com",
                password="TestP@ssw0rd!",
                full_name=f"User {i}",
            )

        users, total = await user_service.list_users()
        assert total == 5
        assert len(users) == 5

    @pytest.mark.asyncio
    async def test_list_users_respects_pagination(self, user_service, user_repo):
        """list_users should respect limit and offset."""
        for i in range(10):
            await user_service.register_user(
                email=f"user{i}@example.com",
                password="TestP@ssw0rd!",
                full_name=f"User {i}",
            )

        users, total = await user_service.list_users(limit=3, offset=2)
        assert total == 10
        assert len(users) == 3

    @pytest.mark.asyncio
    async def test_list_users_filters_by_active(self, user_service, user_repo):
        """list_users should filter by is_active."""
        for i in range(4):
            user = await user_service.register_user(
                email=f"user{i}@example.com",
                password="TestP@ssw0rd!",
                full_name=f"User {i}",
            )
            if i % 2 == 0:
                await user_service.deactivate_user(user.user_id)

        active_users, active_total = await user_service.list_users(is_active=True)
        assert active_total == 2

        inactive_users, inactive_total = await user_service.list_users(is_active=False)
        assert inactive_total == 2
