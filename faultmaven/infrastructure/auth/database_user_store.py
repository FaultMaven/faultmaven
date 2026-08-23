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

from faultmaven.exceptions import (
    ConflictError,
    NotFoundError,
    UserLookupFailed,
    ValidationException,
)
from faultmaven.infrastructure.persistence.user_repository import User, UserRepository
from faultmaven.modules.auth.domain.models.auth import DevUser

logger = logging.getLogger(__name__)


class DatabaseUserStore:
    """Database-backed user storage system.

    Manages user accounts using UserRepository (SQLite/PostgreSQL).
    Adapts between DevUser (auth model) and User (repository model).

    Role persistence strategy (settled in #706):
    - ``dev_roles`` (TEXT, JSON array) is the **canonical** source of the JWT
      ``roles`` claim in BOTH ``AUTH_MODE=local`` and ``AUTH_MODE=oauth``, and
      stays so across the multi-tenant flip. ``organization_members.role_id``
      records affiliation — written by the SSO login path, read for membership
      verification — and is deliberately NOT the claim source.
    - Deriving the claim from the RBAC join tables is left unwired rather than
      half-wired: a partial derivation is exactly what makes role
      administration silently stop taking effect. So ``assign_role`` /
      ``remove_role`` writing here is correct in both modes, ungated.
    - Those methods manage only the **org-scoped** role axis and preserve
      ``platform_admin`` and the base ``user`` marker. See
      ``user_service._split_role_axes``.

    Storage backends:
    - SQLite (local deployment via DATABASE_URL)
    - PostgreSQL (cloud deployment)
    """

    def __init__(self, user_repository: UserRepository):
        """Initialize database user store

        Args:
            user_repository: UserRepository instance. In production this is a
                SessionlessUserRepository, which opens a fresh session per
                operation — the store holds no session and has nothing to
                release at shutdown (#703).
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
            account_kind=user.account_kind,
        )

    async def get_user(self, user_id: str) -> Optional[DevUser]:
        """Get user by ID.

        Args:
            user_id: User identifier

        Returns:
            DevUser if the lookup completed and matched, None if it completed
            and matched nothing. Never None because the lookup *failed* — see
            :class:`~faultmaven.exceptions.UserLookupFailed` (#1043).

        Raises:
            UserLookupFailed: The lookup could not be completed. The caller must
                not read this as "no such user".
        """
        # An empty id matches no user by construction: there is nothing to ask
        # the database, so "absent" here is a completed answer, not a guess.
        if not user_id:
            return None

        try:
            user = await self.user_repository.get(user_id)
        except Exception as e:
            logger.error(f"Failed to get user {user_id}: {e}")
            raise UserLookupFailed(
                f"Could not look up user id {user_id!r}: {e}. This is NOT "
                "'no such user' — the lookup itself failed, so the account's "
                "existence is unknown.",
                lookup="user_id",
                identifier=user_id,
            ) from e

        return self._user_to_devuser(user) if user else None

    async def get_user_by_username(self, username: str) -> Optional[DevUser]:
        """Get user by username.

        Args:
            username: Username to search for

        Returns:
            DevUser if the lookup completed and matched, None if it completed
            and matched nothing. See :meth:`get_user` (#1043).

        Raises:
            UserLookupFailed: The lookup could not be completed.
        """
        if not username:
            return None

        try:
            user = await self.user_repository.get_by_username(username)
        except Exception as e:
            logger.error(f"Failed to get user by username {username}: {e}")
            raise UserLookupFailed(
                f"Could not look up username {username!r}: {e}. This is NOT "
                "'no such user' — the lookup itself failed, so the account's "
                "existence is unknown.",
                lookup="username",
                identifier=username,
            ) from e

        return self._user_to_devuser(user) if user else None

    async def get_user_by_email(self, email: str) -> Optional[DevUser]:
        """Get user by email address.

        Args:
            email: Email address to search for

        Returns:
            DevUser if the lookup completed and matched, None if it completed
            and matched nothing. See :meth:`get_user` (#1043).

        Raises:
            UserLookupFailed: The lookup could not be completed.
        """
        if not email:
            return None

        try:
            user = await self.user_repository.get_by_email(email)
        except Exception as e:
            logger.error(f"Failed to get user by email {email}: {e}")
            raise UserLookupFailed(
                f"Could not look up email {email!r}: {e}. This is NOT "
                "'no such user' — the lookup itself failed, so the account's "
                "existence is unknown.",
                lookup="email",
                identifier=email,
            ) from e

        return self._user_to_devuser(user) if user else None

    async def create_user(
        self,
        username: str,
        email: str = None,
        display_name: str = None,
        account_kind: str = "individual",
    ) -> DevUser:
        """Create new development user

        Args:
            username: Unique username
            email: User email address (optional, auto-generated if not provided)
            display_name: Human-readable display name (optional, auto-generated if not provided)
            account_kind: ADR-012 account kind — 'individual' (human) or 'slack'
                (service account). Set at creation so a service account is never
                briefly persisted as an individual.

        Returns:
            Created DevUser

        Raises:
            ValidationException: Invalid username or email format.
            ConflictError: Username or email already exists.
            Exception: If user creation fails for unforeseen reasons.
        """
        try:
            # Validate inputs
            username = username.strip()
            if not self._validate_username(username):
                raise ValidationException(f"Invalid username format: {username}")

            # Check if user already exists
            existing = await self.get_user_by_username(username)
            if existing:
                raise ConflictError(
                    f"User with username '{username}' already exists",
                    resource_type="user",
                    resource_id=username,
                    conflict_reason="duplicate_username",
                )

            # Auto-generate email if not provided
            if not email:
                email = f"{username}@faultmaven.example"
            else:
                email = email.strip().lower()
                if not self._validate_email(email):
                    raise ValidationException(f"Invalid email format: {email}")

                # Check email uniqueness
                existing_by_email = await self.get_user_by_email(email)
                if existing_by_email:
                    raise ConflictError(
                        f"User with email '{email}' already exists",
                        resource_type="user",
                        resource_id=email,
                        conflict_reason="duplicate_email",
                    )

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
                account_kind=account_kind,
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
        """Update the fields a DevUser carries, leaving the rest of the record.

        DevUser is a partial view of a stored user, and ``UserRepository.update``
        writes every column. Rebuilding a User from a DevUser would therefore
        write NULL over everything DevUser has no concept of — password hash,
        SSO linkage, verification and login timestamps — silently locking the
        account out of both auth modes. So the DevUser's fields are applied onto
        the stored record instead.

        Args:
            user: DevUser with updated fields

        Returns:
            Updated DevUser

        Raises:
            NotFoundError: If user not found.
        """
        try:
            # Get existing user from repository
            existing_user = await self.user_repository.get(user.user_id)
            if not existing_user:
                raise NotFoundError(
                    resource_type="user",
                    resource_id=user.user_id,
                    message=f"User {user.user_id} not found",
                )

            existing_user.username = user.username
            existing_user.email = user.email
            existing_user.display_name = user.display_name
            existing_user.is_active = user.is_active
            existing_user.roles = user.roles if user.roles else ["user"]
            existing_user.account_kind = user.account_kind
            existing_user.updated_at = datetime.now(timezone.utc)

            saved_user = await self.user_repository.update(existing_user)
            return self._user_to_devuser(saved_user)

        except Exception as e:
            logger.error(f"Failed to update user {user.user_id}: {e}")
            raise

    async def record_login(self, user_id: str) -> None:
        """Stamp ``last_login_at`` on the stored user row.

        A separate method rather than a field on ``update_user``: DevUser has
        no last-login concept, and ``update_user`` deliberately leaves login
        timestamps alone. This is the local-mode counterpart of the stamp the
        SSO login path writes (``sso_login_service``), and the row it writes is
        the one ``GET /auth/me`` (#1123) and the admin listing report from.

        Delegates to ``touch_last_login`` — a targeted single-column UPDATE,
        never a read-modify-write, so a login racing an admin's deactivation
        or a password reset cannot write a stale snapshot back over it.
        ``updated_at`` is left alone: a login does not modify the account.

        NOT called by ``create_user``: account creation (CLI, bootstrap admin)
        is not a login. Login handlers call this on successful authentication.

        Raises:
            NotFoundError: If user not found.
        """
        stamped = await self.user_repository.touch_last_login(
            user_id, datetime.now(timezone.utc)
        )
        if not stamped:
            raise NotFoundError(
                resource_type="user",
                resource_id=user_id,
                message=f"User {user_id} not found",
            )

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
