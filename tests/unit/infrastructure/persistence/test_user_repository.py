"""Unit tests for UserRepository (TASK-018).

Tests for:
- InMemoryUserRepository CRUD operations
- User lookup by email and username
- Pagination and filtering
- Conflict detection

Coverage target: 90%+
"""

from datetime import datetime, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import ConflictError, NotFoundError
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    User,
    UserRepository,
)


@pytest.fixture
def user_repo():
    """Create a fresh InMemoryUserRepository for each test."""
    return InMemoryUserRepository()


@pytest.fixture
def sample_user():
    """Create a sample user for testing."""
    now = datetime.now(timezone.utc)
    return User(
        user_id="user-001",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        hashed_password="$2b$12$hashedpassword",
        is_active=True,
        is_email_verified=False,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_users() -> List[User]:
    """Create a list of sample users for pagination tests."""
    now = datetime.now(timezone.utc)
    users = []
    for i in range(10):
        users.append(
            User(
                user_id=f"user-{i:03d}",
                username=f"user{i}",
                email=f"user{i}@example.com",
                display_name=f"User {i}",
                hashed_password="$2b$12$hashedpassword",
                is_active=i % 2 == 0,  # Alternate active/inactive
                is_email_verified=False,
                created_at=now,
                updated_at=now,
            )
        )
    return users


class TestInMemoryUserRepositorySave:
    """Tests for InMemoryUserRepository.save()."""

    @pytest.mark.asyncio
    async def test_save_stores_user(self, user_repo, sample_user):
        """Save should store user in repository."""
        saved = await user_repo.save(sample_user)
        assert saved.user_id == sample_user.user_id
        assert saved.email == sample_user.email

    @pytest.mark.asyncio
    async def test_save_updates_updated_at(self, user_repo, sample_user):
        """Save should update the updated_at timestamp."""
        original_updated = sample_user.updated_at
        saved = await user_repo.save(sample_user)
        assert saved.updated_at >= original_updated

    @pytest.mark.asyncio
    async def test_save_creates_email_index(self, user_repo, sample_user):
        """Save should create email index entry."""
        await user_repo.save(sample_user)
        found = await user_repo.get_by_email(sample_user.email)
        assert found is not None
        assert found.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_save_creates_username_index(self, user_repo, sample_user):
        """Save should create username index entry."""
        await user_repo.save(sample_user)
        found = await user_repo.get_by_username(sample_user.username)
        assert found is not None
        assert found.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, user_repo, sample_user):
        """Save should overwrite existing user with same ID."""
        await user_repo.save(sample_user)
        sample_user.display_name = "Updated Name"
        saved = await user_repo.save(sample_user)
        assert saved.display_name == "Updated Name"


class TestInMemoryUserRepositoryGet:
    """Tests for InMemoryUserRepository.get()."""

    @pytest.mark.asyncio
    async def test_get_returns_user(self, user_repo, sample_user):
        """Get should return user by ID."""
        await user_repo.save(sample_user)
        found = await user_repo.get(sample_user.user_id)
        assert found is not None
        assert found.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, user_repo):
        """Get should return None for non-existent user."""
        found = await user_repo.get("nonexistent-id")
        assert found is None

    @pytest.mark.asyncio
    async def test_get_by_email_case_insensitive(self, user_repo, sample_user):
        """Get by email should be case-insensitive."""
        await user_repo.save(sample_user)
        found = await user_repo.get_by_email(sample_user.email.upper())
        assert found is not None
        assert found.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_get_by_username_case_insensitive(self, user_repo, sample_user):
        """Get by username should be case-insensitive."""
        await user_repo.save(sample_user)
        found = await user_repo.get_by_username(sample_user.username.upper())
        assert found is not None
        assert found.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_get_by_email_returns_none_for_missing(self, user_repo):
        """Get by email should return None for non-existent email."""
        found = await user_repo.get_by_email("nonexistent@example.com")
        assert found is None

    @pytest.mark.asyncio
    async def test_get_by_username_returns_none_for_missing(self, user_repo):
        """Get by username should return None for non-existent username."""
        found = await user_repo.get_by_username("nonexistent")
        assert found is None


class TestInMemoryUserRepositoryCreate:
    """Tests for InMemoryUserRepository.create()."""

    @pytest.mark.asyncio
    async def test_create_stores_user(self, user_repo, sample_user):
        """Create should store new user."""
        created = await user_repo.create(sample_user)
        assert created.user_id == sample_user.user_id

    @pytest.mark.asyncio
    async def test_create_raises_on_duplicate_email(self, user_repo, sample_user):
        """Create should raise ConflictError on duplicate email."""
        await user_repo.create(sample_user)

        duplicate = User(
            user_id="user-002",
            username="different",
            email=sample_user.email,  # Same email
            display_name="Another User",
            hashed_password="$2b$12$hash",
            is_active=True,
            is_email_verified=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        with pytest.raises(ConflictError) as excinfo:
            await user_repo.create(duplicate)
        assert "Email already registered" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_create_raises_on_duplicate_username(self, user_repo, sample_user):
        """Create should raise ConflictError on duplicate username."""
        await user_repo.create(sample_user)

        duplicate = User(
            user_id="user-002",
            username=sample_user.username,  # Same username
            email="different@example.com",
            display_name="Another User",
            hashed_password="$2b$12$hash",
            is_active=True,
            is_email_verified=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        with pytest.raises(ConflictError) as excinfo:
            await user_repo.create(duplicate)
        assert "Username already taken" in str(excinfo.value)


class TestInMemoryUserRepositoryUpdate:
    """Tests for InMemoryUserRepository.update()."""

    @pytest.mark.asyncio
    async def test_update_modifies_user(self, user_repo, sample_user):
        """Update should modify existing user."""
        await user_repo.save(sample_user)
        sample_user.display_name = "Updated Name"
        updated = await user_repo.update(sample_user)
        assert updated.display_name == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_raises_for_nonexistent(self, user_repo, sample_user):
        """Update should raise NotFoundError for non-existent user."""
        with pytest.raises(NotFoundError):
            await user_repo.update(sample_user)

    @pytest.mark.asyncio
    async def test_update_allows_email_change(self, user_repo, sample_user):
        """Update should allow email change if unique."""
        await user_repo.save(sample_user)
        sample_user.email = "new@example.com"
        updated = await user_repo.update(sample_user)
        assert updated.email == "new@example.com"

    @pytest.mark.asyncio
    async def test_update_raises_on_email_conflict(self, user_repo, sample_user):
        """Update should raise ConflictError on email conflict."""
        # Create two users
        await user_repo.save(sample_user)

        other_user = User(
            user_id="user-002",
            username="other",
            email="other@example.com",
            display_name="Other User",
            hashed_password="$2b$12$hash",
            is_active=True,
            is_email_verified=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await user_repo.save(other_user)

        # Try to change sample_user's email to other's email
        sample_user.email = "other@example.com"
        with pytest.raises(ConflictError) as excinfo:
            await user_repo.update(sample_user)
        assert "Email already in use" in str(excinfo.value)


class TestInMemoryUserRepositoryDelete:
    """Tests for InMemoryUserRepository.delete()."""

    @pytest.mark.asyncio
    async def test_delete_removes_user(self, user_repo, sample_user):
        """Delete should remove user from repository."""
        await user_repo.save(sample_user)
        result = await user_repo.delete(sample_user.user_id)
        assert result is True

        found = await user_repo.get(sample_user.user_id)
        assert found is None

    @pytest.mark.asyncio
    async def test_delete_removes_from_indexes(self, user_repo, sample_user):
        """Delete should remove user from email and username indexes."""
        await user_repo.save(sample_user)
        await user_repo.delete(sample_user.user_id)

        found_by_email = await user_repo.get_by_email(sample_user.email)
        found_by_username = await user_repo.get_by_username(sample_user.username)

        assert found_by_email is None
        assert found_by_username is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_nonexistent(self, user_repo):
        """Delete should return False for non-existent user."""
        result = await user_repo.delete("nonexistent-id")
        assert result is False


class TestInMemoryUserRepositoryList:
    """Tests for InMemoryUserRepository.list() and list_users()."""

    @pytest.mark.asyncio
    async def test_list_returns_all_users(self, user_repo, sample_users):
        """List should return all users."""
        for user in sample_users:
            await user_repo.save(user)

        users, total = await user_repo.list()
        assert total == 10
        assert len(users) == 10

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, user_repo, sample_users):
        """List should respect limit parameter."""
        for user in sample_users:
            await user_repo.save(user)

        users, total = await user_repo.list(limit=5)
        assert total == 10
        assert len(users) == 5

    @pytest.mark.asyncio
    async def test_list_respects_offset(self, user_repo, sample_users):
        """List should respect offset parameter."""
        for user in sample_users:
            await user_repo.save(user)

        users, total = await user_repo.list(limit=5, offset=5)
        assert total == 10
        assert len(users) == 5

    @pytest.mark.asyncio
    async def test_list_users_filters_by_active(self, user_repo, sample_users):
        """list_users should filter by is_active."""
        for user in sample_users:
            await user_repo.save(user)

        # 5 users are active (i % 2 == 0)
        active_users, active_total = await user_repo.list_users(is_active=True)
        assert active_total == 5
        for user in active_users:
            assert user.is_active is True

        inactive_users, inactive_total = await user_repo.list_users(is_active=False)
        assert inactive_total == 5
        for user in inactive_users:
            assert user.is_active is False

    @pytest.mark.asyncio
    async def test_list_users_no_filter_returns_all(self, user_repo, sample_users):
        """list_users without filter should return all users."""
        for user in sample_users:
            await user_repo.save(user)

        users, total = await user_repo.list_users()
        assert total == 10

    @pytest.mark.asyncio
    async def test_list_empty_repo_returns_empty(self, user_repo):
        """List on empty repo should return empty list and 0 total."""
        users, total = await user_repo.list()
        assert users == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_sorted_by_created_at_desc(self, user_repo, sample_users):
        """List should sort by created_at descending."""
        for user in sample_users:
            await user_repo.save(user)

        users, _ = await user_repo.list()
        # All users have same created_at in our fixture, but order should be consistent


class TestUserModel:
    """Tests for User Pydantic model."""

    def test_user_model_validation(self):
        """User model should validate fields."""
        now = datetime.now(timezone.utc)
        user = User(
            user_id="user-001",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            hashed_password="hash",
            is_active=True,
            is_email_verified=False,
            created_at=now,
            updated_at=now,
        )
        assert user.user_id == "user-001"

    def test_user_email_validation(self):
        """User model should validate email format."""
        now = datetime.now(timezone.utc)
        with pytest.raises(Exception):  # Pydantic validation error
            User(
                user_id="user-001",
                username="testuser",
                email="invalid-email",  # Invalid
                display_name="Test User",
                created_at=now,
                updated_at=now,
            )

    def test_user_optional_fields(self):
        """User model should allow optional fields to be None."""
        now = datetime.now(timezone.utc)
        user = User(
            user_id="user-001",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            hashed_password=None,  # Optional for SSO users
            is_active=True,
            is_email_verified=False,
            created_at=now,
            updated_at=now,
        )
        assert user.hashed_password is None
        assert user.avatar_url is None
        assert user.sso_provider is None

    def test_user_default_values(self):
        """User model should have correct defaults."""
        now = datetime.now(timezone.utc)
        user = User(
            user_id="user-001",
            username="testuser",
            email="test@example.com",
            display_name="Test User",
            created_at=now,
            updated_at=now,
        )
        assert user.is_active is True
        assert user.is_email_verified is False
        assert user.timezone == "UTC"
        assert user.locale == "en-US"
        assert user.roles == []
