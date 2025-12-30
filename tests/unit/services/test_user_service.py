"""Unit tests for UserService (TASK-018).

Tests for:
- User registration with validation
- User authentication with JWT tokens
- Password reset workflow
- Password change
- Profile updates
- User deactivation

Coverage target: 90%+
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from faultmaven.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    User as RepositoryUser,
)
from faultmaven.services.auth_service import AuthenticationError, AuthService
from faultmaven.services.user_service import UserService


@pytest.fixture
def mock_auth_service():
    """Create a mock AuthService."""
    auth_service = MagicMock(spec=AuthService)
    auth_service._private_key = "test-key"
    auth_service._public_key = "test-key"
    auth_service._access_token_expire_minutes = 15
    auth_service.generate_token_pair.return_value = MagicMock(
        access_token="access-token",
        refresh_token="refresh-token",
    )
    auth_service.revoke_user_tokens = AsyncMock()
    return auth_service


@pytest.fixture
def user_repo():
    """Create a fresh InMemoryUserRepository."""
    return InMemoryUserRepository()


@pytest.fixture
def user_service(user_repo, mock_auth_service):
    """Create UserService with mocked dependencies."""
    with patch("faultmaven.services.user_service.get_settings") as mock_settings:
        mock_settings.return_value.security.jwt_issuer = "faultmaven"
        mock_settings.return_value.security.jwt_audience = "faultmaven-api"
        mock_settings.return_value.security.jwt_algorithm = "HS256"
        return UserService(
            user_repo=user_repo,
            auth_service=mock_auth_service,
        )


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


class TestAuthenticateUser:
    """Tests for UserService.authenticate_user()."""

    @pytest.mark.asyncio
    async def test_authenticate_returns_user_and_tokens(
        self, user_service, user_repo, mock_auth_service
    ):
        """Authentication should return user and JWT tokens."""
        # Register user first
        await user_service.register_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
            full_name="Auth User",
        )

        user, access_token, refresh_token = await user_service.authenticate_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
        )

        assert user is not None
        assert user.email == "auth@example.com"
        assert access_token == "access-token"
        assert refresh_token == "refresh-token"

    @pytest.mark.asyncio
    async def test_authenticate_updates_last_login(self, user_service, user_repo):
        """Authentication should update last_login_at timestamp."""
        # Register user
        await user_service.register_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
            full_name="Auth User",
        )

        # Authenticate
        user, _, _ = await user_service.authenticate_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
        )

        assert user.last_login_at is not None

    @pytest.mark.asyncio
    async def test_authenticate_rejects_wrong_password(self, user_service, user_repo):
        """Authentication should reject wrong password."""
        await user_service.register_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
            full_name="Auth User",
        )

        with pytest.raises(AuthenticationError) as excinfo:
            await user_service.authenticate_user(
                email="auth@example.com",
                password="WrongP@ssw0rd!",
            )
        assert "INVALID_CREDENTIALS" in excinfo.value.error_code

    @pytest.mark.asyncio
    async def test_authenticate_rejects_nonexistent_email(self, user_service):
        """Authentication should reject non-existent email."""
        with pytest.raises(AuthenticationError) as excinfo:
            await user_service.authenticate_user(
                email="nonexistent@example.com",
                password="TestP@ssw0rd!",
            )
        assert "INVALID_CREDENTIALS" in excinfo.value.error_code

    @pytest.mark.asyncio
    async def test_authenticate_rejects_inactive_user(self, user_service, user_repo):
        """Authentication should reject inactive users."""
        # Register and deactivate
        await user_service.register_user(
            email="inactive@example.com",
            password="TestP@ssw0rd!",
            full_name="Inactive User",
        )
        user = await user_repo.get_by_email("inactive@example.com")
        user.is_active = False
        await user_repo.save(user)

        with pytest.raises(AuthenticationError) as excinfo:
            await user_service.authenticate_user(
                email="inactive@example.com",
                password="TestP@ssw0rd!",
            )
        assert "ACCOUNT_INACTIVE" in excinfo.value.error_code

    @pytest.mark.asyncio
    async def test_authenticate_calls_generate_token_pair(
        self, user_service, user_repo, mock_auth_service
    ):
        """Authentication should call AuthService.generate_token_pair."""
        await user_service.register_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
            full_name="Auth User",
        )

        await user_service.authenticate_user(
            email="auth@example.com",
            password="TestP@ssw0rd!",
        )

        mock_auth_service.generate_token_pair.assert_called_once()


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

        with patch.object(user_service, "_encode_reset_token") as mock_encode:
            mock_encode.return_value = "reset-token"
            token = await user_service.request_password_reset(email="reset@example.com")
            assert token == "reset-token"

    @pytest.mark.asyncio
    async def test_reset_request_returns_token_for_nonexistent(self, user_service):
        """Reset request should return token even for non-existent email (enumeration prevention)."""
        with patch.object(user_service, "_generate_dummy_reset_token") as mock_dummy:
            mock_dummy.return_value = "dummy-token"
            token = await user_service.request_password_reset(
                email="nonexistent@example.com"
            )
            assert token == "dummy-token"


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

        # Mock token verification
        with patch.object(user_service, "_verify_reset_token") as mock_verify:
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

        with patch.object(user_service, "_verify_reset_token") as mock_verify:
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

        with patch.object(user_service, "_verify_reset_token") as mock_verify:
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
        with patch.object(user_service, "_verify_reset_token") as mock_verify:
            mock_verify.side_effect = Exception("Invalid token")

            with pytest.raises(AuthenticationError) as excinfo:
                await user_service.reset_password(
                    reset_token="invalid-token",
                    new_password="NewP@ssw0rd!",
                )
            assert "INVALID_RESET_TOKEN" in excinfo.value.error_code


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
    async def test_update_profile_email_change_unverifies(self, user_service, user_repo):
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
    async def test_update_profile_rejects_duplicate_email(self, user_service, user_repo):
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
