"""Database-backed User Storage System

Purpose: Database implementation of user storage for local and cloud deployments

This module provides a database-backed user store that uses UserRepository
(SQLite for local, PostgreSQL for cloud) to persist user data across restarts.
It adapts between DevUser (authentication model) and User (repository model).
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from faultmaven.infrastructure.persistence.user_repository import User, UserRepository
from faultmaven.modules.auth.domain.models.auth import DevUser

logger = logging.getLogger(__name__)


class DatabaseUserStore:
    """Database-backed user storage system.

    Manages user accounts using UserRepository (SQLite/PostgreSQL).
    Adapts between DevUser (auth model) and User (repository model).

    Role persistence strategy (by deployment mode):
    - Local mode (AUTH_MODE=local): roles stored in the ``dev_roles`` TEXT
      column (JSON array) on the users table. This is the canonical role
      source for local deployments. RBAC join tables are NOT populated in
      local mode and must not be relied on.
    - Cloud mode (AUTH_MODE=oauth): roles derived from RBAC tables
      (organization_members → roles). The ``dev_roles`` column exists but
      is ignored in this path.

    Storage backends:
    - SQLite (local deployment via DATABASE_URL)
    - PostgreSQL (cloud deployment)
    """

    def __init__(self, user_repository: UserRepository):
        """Initialize database user store

        Args:
            user_repository: UserRepository instance (SQLite or PostgreSQL)
        """
        self.user_repository = user_repository

        # Validation patterns
        self.email_pattern = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
        self.username_pattern = re.compile(r"^([^@]+@[^@]+\.[^@]+|[a-zA-Z0-9._-]+)$")

    def _user_to_devuser(self, user: User) -> DevUser:
        """Convert User (repository model) to DevUser (auth model)"""
        return DevUser(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            is_dev_user=True,
            is_active=user.is_active,
            roles=user.roles if user.roles else ["user"],
        )

    def _devuser_to_user(self, dev_user: DevUser) -> User:
        """Convert DevUser (auth model) to User (repository model)"""
        return User(
            user_id=dev_user.user_id,
            username=dev_user.username,
            email=dev_user.email,
            display_name=dev_user.display_name,
            avatar_url=None,
            timezone="UTC",
            locale="en-US",
            hashed_password=None,
            is_active=dev_user.is_active,
            is_email_verified=False,
            email_verified_at=None,
            sso_provider=None,
            sso_provider_id=None,
            created_at=dev_user.created_at,
            updated_at=datetime.now(timezone.utc),
            last_login_at=None,
            last_password_change_at=None,
            deleted_at=None,
            roles=dev_user.roles if dev_user.roles else ["user"],
        )

    async def get_user(self, user_id: str) -> Optional[DevUser]:
        """Get user by ID

        Args:
            user_id: User identifier

        Returns:
            DevUser if found, None otherwise
        """
        try:
            if not user_id:
                return None

            user = await self.user_repository.get(user_id)
            if not user:
                return None

            return self._user_to_devuser(user)

        except Exception as e:
            logger.error(f"Failed to get user {user_id}: {e}")
            return None

    async def get_user_by_username(self, username: str) -> Optional[DevUser]:
        """Get user by username

        Args:
            username: Username to search for

        Returns:
            DevUser if found, None otherwise
        """
        try:
            if not username:
                return None

            user = await self.user_repository.get_by_username(username)
            if not user:
                return None

            return self._user_to_devuser(user)

        except Exception as e:
            logger.error(f"Failed to get user by username {username}: {e}")
            return None

    async def get_user_by_email(self, email: str) -> Optional[DevUser]:
        """Get user by email address

        Args:
            email: Email address to search for

        Returns:
            DevUser if found, None otherwise
        """
        try:
            if not email:
                return None

            user = await self.user_repository.get_by_email(email)
            if not user:
                return None

            return self._user_to_devuser(user)

        except Exception as e:
            logger.error(f"Failed to get user by email {email}: {e}")
            return None

    async def create_user(
        self, username: str, email: str = None, display_name: str = None
    ) -> DevUser:
        """Create new development user

        Args:
            username: Unique username
            email: User email address (optional, auto-generated if not provided)
            display_name: Human-readable display name (optional, auto-generated if not provided)

        Returns:
            Created DevUser

        Raises:
            ValueError: If username/email already exists or validation fails
            Exception: If user creation fails
        """
        try:
            # Validate inputs
            username = username.strip()
            if not self._validate_username(username):
                raise ValueError(f"Invalid username format: {username}")

            # Check if user already exists
            existing = await self.get_user_by_username(username)
            if existing:
                raise ValueError(f"User with username '{username}' already exists")

            # Auto-generate email if not provided
            if not email:
                email = f"{username}@faultmaven.example"
            else:
                email = email.strip().lower()
                if not self._validate_email(email):
                    raise ValueError(f"Invalid email format: {email}")

                # Check email uniqueness
                existing_by_email = await self.get_user_by_email(email)
                if existing_by_email:
                    raise ValueError(f"User with email '{email}' already exists")

            # Auto-generate display name if not provided
            if not display_name:
                display_name = self._generate_display_name(username)

            # Generate UUID for user_id
            import uuid

            user_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)

            # Create User model for repository
            user = User(
                user_id=user_id,
                username=username,
                email=email,
                display_name=display_name,
                avatar_url=None,
                timezone="UTC",
                locale="en-US",
                hashed_password=None,
                is_active=True,
                is_email_verified=False,
                email_verified_at=None,
                sso_provider=None,
                sso_provider_id=None,
                created_at=now,
                updated_at=now,
                last_login_at=None,
                last_password_change_at=None,
                deleted_at=None,
                roles=["user"],
            )

            # Save via repository (upsert behavior - handles both insert and update)
            # This will create the user or update if it already exists
            saved_user = await self.user_repository.save(user)

            # Convert to DevUser for return
            return self._user_to_devuser(saved_user)

        except Exception as e:
            logger.error(f"Failed to create user {username}: {e}")
            raise

    async def update_user(self, user: DevUser) -> DevUser:
        """Update existing user

        Args:
            user: DevUser with updated fields

        Returns:
            Updated DevUser

        Raises:
            ValueError: If user not found or validation fails
        """
        try:
            # Get existing user from repository
            existing_user = await self.user_repository.get(user.user_id)
            if not existing_user:
                raise ValueError(f"User {user.user_id} not found")

            # Convert DevUser to User and update
            updated_user = self._devuser_to_user(user)
            updated_user.updated_at = datetime.now(timezone.utc)

            saved_user = await self.user_repository.update(updated_user)
            return self._user_to_devuser(saved_user)

        except Exception as e:
            logger.error(f"Failed to update user {user.user_id}: {e}")
            raise

    async def delete_user(self, user_id: str) -> bool:
        """Delete user by ID

        Args:
            user_id: User identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            return await self.user_repository.delete(user_id)
        except Exception as e:
            logger.error(f"Failed to delete user {user_id}: {e}")
            return False

    async def list_users(self, limit: int = 100, offset: int = 0) -> List[DevUser]:
        """List users with pagination

        Args:
            limit: Maximum number of users to return
            offset: Pagination offset

        Returns:
            List of DevUser objects
        """
        try:
            users, _ = await self.user_repository.list(limit=limit, offset=offset)
            return [self._user_to_devuser(user) for user in users]
        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            return []

    async def count_users(self) -> int:
        """Get total number of users

        Returns:
            Total user count
        """
        try:
            _, total = await self.user_repository.list(limit=1, offset=0)
            return total
        except Exception as e:
            logger.error(f"Failed to count users: {e}")
            return 0

    def _validate_username(self, username: str) -> bool:
        """Validate username format"""
        if not username or len(username) < 1:
            return False
        return bool(self.username_pattern.match(username))

    def _validate_email(self, email: str) -> bool:
        """Validate email format"""
        if not email:
            return False
        return bool(self.email_pattern.match(email))

    def _generate_display_name(self, username: str) -> str:
        """Generate display name from username"""
        # Capitalize first letter and replace underscores/dots with spaces
        display = username.replace("_", " ").replace(".", " ").title()
        return display if display else username

    # OAuth service compatibility adapter
    async def get(self, user_id: str) -> Optional[DevUser]:
        """Get user by ID (OAuth service compatibility)

        This method provides compatibility with the OAuth service which expects
        a .get() method instead of .get_user().

        Args:
            user_id: User ID

        Returns:
            DevUser if found, None otherwise
        """
        return await self.get_user(user_id)
