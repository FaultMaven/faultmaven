"""In-Memory User Storage System

Purpose: In-memory implementation of user storage for local development

This module provides a RAM-based user store that stores users in Python
dictionaries. Data is lost on application restart, making it suitable for
local development and testing where Redis is not available.

Key Features:
- Unique username validation
- User account creation and updates
- Email validation and uniqueness
- Auto-generated user IDs
- Development user management

Storage:
- All data stored in Python dictionaries (RAM)
- Data lost on application restart
- Thread-safe using asyncio.Lock
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from faultmaven.models.auth import DevUser

logger = logging.getLogger(__name__)


class InMemoryUserStore:
    """In-memory user storage system

    Manages user accounts using Python dictionaries.
    Suitable for local development and testing.

    Storage Schema (in-memory):
    - _users: Dict[user_id, DevUser dict]
    - _username_index: Dict[username_lower, user_id]
    - _email_index: Dict[email_lower, user_id]
    - _user_list: List[user_id]
    """

    def __init__(self):
        """Initialize in-memory user store"""
        # In-memory storage
        self._users: Dict[str, dict] = {}  # user_id -> DevUser dict
        self._username_index: Dict[str, str] = {}  # username_lower -> user_id
        self._email_index: Dict[str, str] = {}  # email_lower -> user_id
        self._user_list: List[str] = []  # List of user_ids
        self._lock = asyncio.Lock()  # Thread-safe operations

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

            async with self._lock:
                user_dict = self._users.get(user_id)
                if not user_dict:
                    return None

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

            async with self._lock:
                user_id = self._username_index.get(username.lower())
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

            async with self._lock:
                user_id = self._email_index.get(email.lower())
                if not user_id:
                    return None

                return await self.get_user(user_id)

        except Exception as e:
            logger.error(f"Failed to get user by email {email}: {e}")
            return None

    async def create_user(
        self, username: str, email: str = None, display_name: str = None
    ) -> DevUser:
        """Create new development user

        Args:
            username: Unique username
            email: User email address (optional)
            display_name: Human-readable display name (optional)

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
                raise ValueError(
                    "Invalid username format (3-50 chars, email address or alphanumeric with ., _, -)"
                )

            if email:
                email = email.strip().lower()
                if not self._validate_email(email):
                    raise ValueError("Invalid email format")

            # Check username uniqueness
            if await self.get_user_by_username(username):
                raise ValueError(f"Username '{username}' already exists")

            # Check email uniqueness
            if email and await self.get_user_by_email(email):
                raise ValueError(f"Email '{email}' already exists")

            async with self._lock:
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
                        email = f"{username.lower()}@dev.faultmaven.local"

                user = DevUser(
                    user_id=user_id,
                    username=username,
                    email=email,
                    display_name=display_name,
                    created_at=now,
                    is_dev_user=True,
                    is_active=True,
                )

                # Store in memory
                self._users[user_id] = user.to_dict()
                self._username_index[username.lower()] = user_id
                self._email_index[email.lower()] = user_id
                self._user_list.append(user_id)

                logger.info(f"Created user {user_id} with username '{username}'")
                return user

        except ValueError:
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
            ValueError: If user not found or validation fails
            Exception: If update fails
        """
        try:
            # Verify user exists
            existing_user = await self.get_user(user.user_id)
            if not existing_user:
                raise ValueError(f"User {user.user_id} not found")

            # Validate email if changed
            if user.email != existing_user.email:
                if not self._validate_email(user.email):
                    raise ValueError("Invalid email format")

                # Check email uniqueness
                if await self.get_user_by_email(user.email):
                    raise ValueError(f"Email '{user.email}' already exists")

            async with self._lock:
                # Update storage
                self._users[user.user_id] = user.to_dict()

                # Update email index if changed
                if user.email != existing_user.email:
                    # Remove old email mapping
                    if existing_user.email.lower() in self._email_index:
                        del self._email_index[existing_user.email.lower()]

                    # Add new email mapping
                    self._email_index[user.email.lower()] = user.user_id

                logger.info(f"Updated user {user.user_id}")
                return user

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Failed to update user {user.user_id}: {e}")
            raise Exception(f"User update failed: {str(e)}")

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

            async with self._lock:
                # Remove from storage
                if user_id in self._users:
                    del self._users[user_id]
                if user.username.lower() in self._username_index:
                    del self._username_index[user.username.lower()]
                if user.email.lower() in self._email_index:
                    del self._email_index[user.email.lower()]
                if user_id in self._user_list:
                    self._user_list.remove(user_id)

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
            async with self._lock:
                # Apply pagination
                paginated_ids = self._user_list[offset : offset + limit]

                users = []
                for user_id in paginated_ids:
                    user_dict = self._users.get(user_id)
                    if user_dict:
                        users.append(DevUser.from_dict(user_dict))

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
            async with self._lock:
                return len(self._user_list)
        except Exception as e:
            logger.error(f"Failed to count users: {e}")
            return 0

    def _validate_username(self, username: str) -> bool:
        """Validate username format"""
        if not username or len(username) < 3 or len(username) > 50:
            return False
        return bool(self.username_pattern.match(username))

    def _validate_email(self, email: str) -> bool:
        """Validate email format"""
        if not email:
            return False
        return bool(self.email_pattern.match(email))

    def _generate_display_name(self, username: str) -> str:
        """Generate display name from username"""
        # Capitalize first letter and replace common separators
        display = username.replace("_", " ").replace("-", " ").replace(".", " ")
        return " ".join(word.capitalize() for word in display.split())
