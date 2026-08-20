"""User Storage System

Purpose: Handle user account storage and retrieval operations

This module provides user management for the FaultMaven authentication system.
It handles user creation, retrieval, updates, and username uniqueness using
Redis as the backend store.

Key Features:
- Unique username validation
- User account creation and updates
- Email validation and uniqueness
- Auto-generated user IDs
- Development user management

Storage Schema:
- user:{user_id} -> {user_json}
- user:username:{username} -> {user_id}
- user:email:{email} -> {user_id}
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from faultmaven.exceptions import ConflictError, NotFoundError, ValidationException
from faultmaven.modules.auth.domain.models.auth import DevUser

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)


class RedisUserStore:
    """Redis-based user storage system

    Manages user accounts using Redis as the backend store.
    Suitable for production and multi-process deployments.

    Redis Storage Schema:
    - user:{user_id} -> {user_data}
    - user:username:{username} -> {user_id}
    - user:email:{email} -> {user_id}
    - user:list -> [{user_id}, ...]
    """

    def __init__(self, redis_client: Redis):
        """Initialize user store

        Args:
            redis_client: Redis connection for user storage
        """
        self.redis = redis_client

        # Redis key patterns
        self.user_key_pattern = "auth:user:{}"
        self.username_key_pattern = "auth:username:{}"
        self.email_key_pattern = "auth:email:{}"
        self.user_list_key = "auth:user_list"

        # Validation patterns
        self.email_pattern = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
        # Allow both email addresses and traditional usernames
        self.username_pattern = re.compile(r"^([^@]+@[^@]+\.[^@]+|[a-zA-Z0-9._-]+)$")

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

            user_key = self.user_key_pattern.format(user_id)
            user_data = await self._redis_get(user_key)

            if not user_data:
                return None

            user_dict = json.loads(user_data)
            return DevUser.from_dict(user_dict)

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

            username_key = self.username_key_pattern.format(username.lower())
            user_id = await self._redis_get(username_key)

            if not user_id:
                return None

            return await self.get_user(user_id)

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

            email_key = self.email_key_pattern.format(email.lower())
            user_id = await self._redis_get(email_key)

            if not user_id:
                return None

            return await self.get_user(user_id)

        except Exception as e:
            logger.error(f"Failed to get user by email {email}: {e}")
            return None

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
            email: User email address (optional)
            display_name: Human-readable display name (optional)
            account_kind: ADR-012 account kind — 'individual' or 'slack'. Kept
                in step with DatabaseUserStore: the container picks between the
                two at runtime, so a caller cannot know which one it holds.

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
                raise ValidationException(
                    "Invalid username format (3-50 chars, email address or alphanumeric with ., _, -)"
                )

            if email:
                email = email.strip().lower()
                if not self._validate_email(email):
                    raise ValidationException("Invalid email format")

            # Check username uniqueness
            if await self.get_user_by_username(username):
                raise ConflictError(
                    f"Username '{username}' already exists",
                    resource_type="user",
                    resource_id=username,
                    conflict_reason="duplicate_username",
                )

            # Check email uniqueness
            if email and await self.get_user_by_email(email):
                raise ConflictError(
                    f"Email '{email}' already exists",
                    resource_type="user",
                    resource_id=email,
                    conflict_reason="duplicate_email",
                )

            # Generate user data
            user_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)

            # Auto-generate display name if not provided
            if not display_name:
                display_name = self._generate_display_name(username)

            # Auto-generate email if not provided
            if not email:
                # If username is already an email, use it directly
                if self._validate_email(username):
                    email = username.lower()
                else:
                    email = f"{username.lower()}@faultmaven.example"

            user = DevUser(
                user_id=user_id,
                username=username,
                email=email,
                display_name=display_name,
                created_at=now,
                is_dev_user=True,
                is_active=True,
                # Explicit, so this path and DatabaseUserStore visibly agree.
                roles=["user"],
                account_kind=account_kind,
            )

            # Store in Redis
            user_key = self.user_key_pattern.format(user_id)
            username_key = self.username_key_pattern.format(username.lower())
            email_key = self.email_key_pattern.format(email.lower())

            await self._redis_set(user_key, json.dumps(user.to_dict()))
            await self._redis_set(username_key, user_id)
            await self._redis_set(email_key, user_id)

            # Add to user list
            await self._redis_sadd(self.user_list_key, user_id)

            logger.info(f"Created user {user_id} with username '{username}'")
            return user

        except (ValidationException, ConflictError):
            raise
        except Exception as e:
            logger.error(f"Failed to create user '{username}': {e}")
            raise Exception(f"User creation failed: {str(e)}")

    async def update_user(self, user: DevUser) -> DevUser:
        """Update existing user

        Args:
            user: DevUser with updated information

        Returns:
            Updated DevUser

        Raises:
            NotFoundError: If user not found.
            ValidationException: Invalid email format.
            ConflictError: Email already used by another user.
            Exception: If update fails for unforeseen reasons.
        """
        try:
            # Verify user exists
            existing_user = await self.get_user(user.user_id)
            if not existing_user:
                raise NotFoundError(
                    resource_type="user",
                    resource_id=user.user_id,
                    message=f"User {user.user_id} not found",
                )

            # Validate email if changed
            if user.email != existing_user.email:
                if not self._validate_email(user.email):
                    raise ValidationException("Invalid email format")

                # Check email uniqueness
                if await self.get_user_by_email(user.email):
                    raise ConflictError(
                        f"Email '{user.email}' already exists",
                        resource_type="user",
                        resource_id=user.email,
                        conflict_reason="duplicate_email",
                    )

            # Update storage
            user_key = self.user_key_pattern.format(user.user_id)
            await self._redis_set(user_key, json.dumps(user.to_dict()))

            # Update email index if changed
            if user.email != existing_user.email:
                # Remove old email mapping
                old_email_key = self.email_key_pattern.format(
                    existing_user.email.lower()
                )
                await self._redis_delete(old_email_key)

                # Add new email mapping
                new_email_key = self.email_key_pattern.format(user.email.lower())
                await self._redis_set(new_email_key, user.user_id)

            logger.info(f"Updated user {user.user_id}")
            return user

        except (NotFoundError, ValidationException, ConflictError):
            # Typed service exceptions propagate; the route layer relies on
            # the global handler to map them to 404/422/409.
            raise
        except Exception as e:
            logger.error(f"Failed to update user {user.user_id}: {e}")
            raise Exception(f"User update failed: {str(e)}")

    async def record_login(self, user_id: str) -> None:
        """Accept a login stamp without persisting one.

        Kept in step with ``DatabaseUserStore``: the container picks between
        the two at runtime, so a login handler cannot know which one it holds.
        DevUser (this store's stored shape) has no last-login field, and no
        reader sources ``last_login`` from Redis — ``GET /auth/me`` reads the
        database user row and degrades to null when there is none. A stamp
        here would be write-only state, so this is deliberately a no-op.
        """
        logger.debug(
            f"record_login({user_id}): RedisUserStore persists no last-login; no-op"
        )

    async def delete_user(self, user_id: str) -> bool:
        """Delete user account

        Args:
            user_id: User identifier

        Returns:
            True if user was deleted successfully
        """
        try:
            user = await self.get_user(user_id)
            if not user:
                return False

            # Remove from Redis
            user_key = self.user_key_pattern.format(user_id)
            username_key = self.username_key_pattern.format(user.username.lower())
            email_key = self.email_key_pattern.format(user.email.lower())

            await self._redis_delete(user_key)
            await self._redis_delete(username_key)
            await self._redis_delete(email_key)

            # Remove from user list
            await self._redis_srem(self.user_list_key, user_id)

            logger.info(f"Deleted user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete user {user_id}: {e}")
            return False

    async def list_users(self, limit: int = 100, offset: int = 0) -> List[DevUser]:
        """List all users with pagination

        Args:
            limit: Maximum number of users to return
            offset: Number of users to skip

        Returns:
            List of DevUser objects
        """
        try:
            user_ids = await self._redis_smembers(self.user_list_key)

            # Apply pagination
            paginated_ids = user_ids[offset : offset + limit]

            users = []
            for user_id in paginated_ids:
                user = await self.get_user(user_id)
                if user:
                    users.append(user)

            return users

        except Exception as e:
            logger.error(f"Failed to list users: {e}")
            return []

    async def count_users(self) -> int:
        """Get total number of users

        Returns:
            Total user count
        """
        try:
            return await self._redis_scard(self.user_list_key)
        except Exception as e:
            logger.error(f"Failed to count users: {e}")
            return 0

    def _validate_username(self, username: str) -> bool:
        """Validate username format (allows email addresses and traditional usernames)"""
        return bool(
            username
            and self.username_pattern.match(username)
            and len(username) >= 3
            and len(username) <= 50
        )

    def _validate_email(self, email: str) -> bool:
        """Validate email format"""
        return bool(email and self.email_pattern.match(email))

    def _generate_display_name(self, username: str) -> str:
        """Generate display name from username"""
        # If username is an email, use the local part before @
        if self._validate_email(username):
            local_part = username.split("@")[0]
            display_name = (
                local_part.replace(".", " ").replace("_", " ").replace("-", " ")
            )
            return " ".join(word.capitalize() for word in display_name.split())
        else:
            # Convert username to title case and replace separators
            display_name = (
                username.replace(".", " ").replace("_", " ").replace("-", " ")
            )
            return " ".join(word.capitalize() for word in display_name.split())

    # Redis async wrapper methods (using async Redis client)
    async def _redis_set(self, key: str, value: str) -> None:
        """Set Redis key"""
        try:
            return await self.redis.set(key, value)
        except Exception as e:
            logger.error(f"Redis SET failed for key {key}: {e}")
            raise

    async def _redis_get(self, key: str) -> Optional[str]:
        """Get Redis key value"""
        try:
            result = await self.redis.get(key)
            return result if result else None
        except Exception as e:
            logger.error(f"Redis GET failed for key {key}: {e}")
            return None

    async def _redis_delete(self, key: str) -> None:
        """Delete Redis key"""
        try:
            return await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Redis DELETE failed for key {key}: {e}")

    async def _redis_sadd(self, key: str, value: str) -> None:
        """Add to Redis set"""
        try:
            return await self.redis.sadd(key, value)
        except Exception as e:
            logger.error(f"Redis SADD failed for key {key}: {e}")

    async def _redis_srem(self, key: str, value: str) -> None:
        """Remove from Redis set"""
        try:
            return await self.redis.srem(key, value)
        except Exception as e:
            logger.error(f"Redis SREM failed for key {key}: {e}")

    async def _redis_smembers(self, key: str) -> List[str]:
        """Get Redis set members"""
        try:
            members = await self.redis.smembers(key)
            return [str(member) for member in members]
        except Exception as e:
            logger.error(f"Redis SMEMBERS failed for key {key}: {e}")
            return []

    async def _redis_scard(self, key: str) -> int:
        """Get Redis set cardinality"""
        try:
            return await self.redis.scard(key)
        except Exception as e:
            logger.error(f"Redis SCARD failed for key {key}: {e}")
            return 0
