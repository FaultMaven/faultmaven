"""User Repository for user account persistence.

This module provides the repository pattern for User domain model persistence.
It abstracts database operations and provides clean interfaces for the service layer.

Adapters:
- InMemoryUserRepository: Development/testing (RAM storage)
- PostgreSQLUserRepository: Production (PostgreSQL auth_db database)
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


class User(BaseModel):
    """
    Enterprise user model for repository pattern.

    Matches user-storage-design.md schema.
    All fields align with production PostgreSQL schema.
    """
    # ============================================================
    # Identity
    # ============================================================
    user_id: str = Field(..., description="Unique user identifier")
    username: str = Field(..., min_length=1, max_length=100, description="Unique username")
    email: EmailStr = Field(..., description="User email address")

    # ============================================================
    # Authentication
    # ============================================================
    hashed_password: Optional[str] = Field(None, description="Bcrypt password hash (NULL for SSO-only users)")
    is_active: bool = Field(True, description="Account active status")

    # ============================================================
    # Profile
    # ============================================================
    display_name: str = Field(..., min_length=1, max_length=200, description="Display name")
    avatar_url: Optional[str] = Field(None, max_length=500, description="Profile picture URL")
    timezone: str = Field("UTC", max_length=50, description="User timezone")
    locale: str = Field("en-US", max_length=10, description="User locale (i18n)")

    # ============================================================
    # Email Verification
    # ============================================================
    is_email_verified: bool = Field(False, description="Email verification status")
    email_verified_at: Optional[datetime] = Field(None, description="When email was verified")

    # ============================================================
    # SSO Integration
    # ============================================================
    sso_provider: Optional[str] = Field(None, max_length=50, description="SSO provider (google, okta, azure)")
    sso_provider_id: Optional[str] = Field(None, max_length=255, description="External user ID from SSO provider")

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(..., description="Account creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    last_login_at: Optional[datetime] = Field(None, description="Last successful login")
    last_password_change_at: Optional[datetime] = Field(None, description="Last password change")

    # ============================================================
    # Soft Delete
    # ============================================================
    deleted_at: Optional[datetime] = Field(None, description="Soft delete timestamp (NULL = active)")

    # ============================================================
    # Authorization (Simplified for InMemory/Redis)
    # ============================================================
    # NOTE: In production PostgreSQL with user-storage-design.md,
    # roles will be in separate tables (organization_members → roles).
    # For InMemory/Redis development, store as simple string list.
    roles: List[str] = Field(default_factory=list, description="User roles (development only)")


# ============================================================
# Repository Interface
# ============================================================

class UserRepository(ABC):
    """
    Abstract repository interface for User persistence.

    Implementations:
    - InMemoryUserRepository: Development and testing
    - PostgreSQLUserRepository: Production database
    """

    @abstractmethod
    async def save(self, user: User) -> User:
        """
        Save user to persistence layer.

        Args:
            user: User domain object

        Returns:
            Saved user (may have updated timestamps)
        """
        pass

    @abstractmethod
    async def get(self, user_id: str) -> Optional[User]:
        """
        Retrieve user by ID.

        Args:
            user_id: User identifier

        Returns:
            User if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_by_username(self, username: str) -> Optional[User]:
        """
        Retrieve user by username.

        Args:
            username: Username to search for

        Returns:
            User if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_by_email(self, email: str) -> Optional[User]:
        """
        Retrieve user by email.

        Args:
            email: Email address to search for

        Returns:
            User if found, None otherwise
        """
        pass

    @abstractmethod
    async def list(
        self,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List[User], int]:
        """
        List users with pagination.

        Args:
            limit: Maximum results
            offset: Pagination offset

        Returns:
            Tuple of (users, total_count)
        """
        pass

    @abstractmethod
    async def delete(self, user_id: str) -> bool:
        """
        Delete user by ID.

        Args:
            user_id: User identifier

        Returns:
            True if deleted, False if not found
        """
        pass


# ============================================================
# In-Memory Implementation (for Testing/Development)
# ============================================================

class InMemoryUserRepository(UserRepository):
    """
    In-memory user repository for testing and development.

    Data stored in dictionary, not persistent across restarts.
    """

    def __init__(self):
        """Initialize empty in-memory store."""
        self._users: Dict[str, User] = {}
        self._username_index: Dict[str, str] = {}  # username -> user_id
        self._email_index: Dict[str, str] = {}  # email -> user_id

    async def save(self, user: User) -> User:
        """Save user to memory."""
        # Auto-populate updated_at timestamp
        user.updated_at = datetime.now(timezone.utc)

        # Store user
        self._users[user.user_id] = user

        # Update indexes
        self._username_index[user.username.lower()] = user.user_id
        self._email_index[user.email.lower()] = user.user_id

        return user

    async def get(self, user_id: str) -> Optional[User]:
        """Get user from memory."""
        return self._users.get(user_id)

    async def get_by_username(self, username: str) -> Optional[User]:
        """Get user by username."""
        user_id = self._username_index.get(username.lower())
        if user_id:
            return self._users.get(user_id)
        return None

    async def get_by_email(self, email: str) -> Optional[User]:
        """Get user by email."""
        user_id = self._email_index.get(email.lower())
        if user_id:
            return self._users.get(user_id)
        return None

    async def list(
        self,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List[User], int]:
        """List users with pagination."""
        all_users = list(self._users.values())

        # Sort by created_at descending
        all_users.sort(key=lambda u: u.created_at, reverse=True)

        total_count = len(all_users)
        paginated = all_users[offset:offset + limit]

        return paginated, total_count

    async def delete(self, user_id: str) -> bool:
        """Delete user from memory."""
        user = self._users.get(user_id)
        if not user:
            return False

        # Remove from indexes
        self._username_index.pop(user.username.lower(), None)
        self._email_index.pop(user.email.lower(), None)

        # Remove user
        del self._users[user_id]
        return True


# ============================================================
# PostgreSQL Implementation (Production)
# ============================================================

class PostgreSQLUserRepository(UserRepository):
    """
    PostgreSQL user repository for production use.

    Uses SQLAlchemy for database operations.
    Targets auth_db database in PostgreSQL.
    """

    def __init__(self, db_session):
        """
        Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session
        """
        self.db = db_session

    async def save(self, user: User) -> User:
        """
        Save user to PostgreSQL.

        Uses INSERT ON CONFLICT UPDATE (upsert) for atomic save.
        Auto-populates updated_at timestamp.
        """
        from sqlalchemy import text

        # Auto-populate updated_at timestamp
        user.updated_at = datetime.now(timezone.utc)

        user_data = {
            'user_id': user.user_id,
            'username': user.username,
            'email': user.email,
            'display_name': user.display_name,
            'avatar_url': user.avatar_url,
            'timezone': user.timezone,
            'locale': user.locale,
            'hashed_password': user.hashed_password,
            'is_active': user.is_active,
            'is_email_verified': user.is_email_verified,
            'email_verified_at': user.email_verified_at,
            'sso_provider': user.sso_provider,
            'sso_provider_id': user.sso_provider_id,
            'created_at': user.created_at,
            'updated_at': user.updated_at,
            'last_login_at': user.last_login_at,
            'last_password_change_at': user.last_password_change_at,
            'deleted_at': user.deleted_at,
            'roles': ','.join(user.roles) if user.roles else ''
        }

        # Upsert query
        query = text("""
            INSERT INTO users (
                user_id, username, email, display_name, avatar_url, timezone, locale,
                hashed_password, is_active, is_email_verified, email_verified_at,
                sso_provider, sso_provider_id, created_at, updated_at, last_login_at,
                last_password_change_at, deleted_at, roles
            ) VALUES (
                :user_id, :username, :email, :display_name, :avatar_url, :timezone, :locale,
                :hashed_password, :is_active, :is_email_verified, :email_verified_at,
                :sso_provider, :sso_provider_id, :created_at, :updated_at, :last_login_at,
                :last_password_change_at, :deleted_at, :roles
            )
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                timezone = EXCLUDED.timezone,
                locale = EXCLUDED.locale,
                hashed_password = EXCLUDED.hashed_password,
                is_active = EXCLUDED.is_active,
                is_email_verified = EXCLUDED.is_email_verified,
                email_verified_at = EXCLUDED.email_verified_at,
                sso_provider = EXCLUDED.sso_provider,
                sso_provider_id = EXCLUDED.sso_provider_id,
                updated_at = EXCLUDED.updated_at,
                last_login_at = EXCLUDED.last_login_at,
                last_password_change_at = EXCLUDED.last_password_change_at,
                deleted_at = EXCLUDED.deleted_at,
                roles = EXCLUDED.roles
        """)

        await self.db.execute(query, user_data)
        await self.db.commit()

        return user

    async def get(self, user_id: str) -> Optional[User]:
        """Retrieve user from PostgreSQL."""
        from sqlalchemy import text

        query = text("SELECT * FROM users WHERE user_id = :user_id")
        result = await self.db.execute(query, {"user_id": user_id})
        row = result.first()

        if not row:
            return None

        return User(
            user_id=row.user_id,
            username=row.username,
            email=row.email,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
            timezone=row.timezone,
            locale=row.locale,
            hashed_password=row.hashed_password,
            is_active=row.is_active,
            is_email_verified=row.is_email_verified,
            email_verified_at=row.email_verified_at,
            sso_provider=row.sso_provider,
            sso_provider_id=row.sso_provider_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_login_at=row.last_login_at,
            last_password_change_at=row.last_password_change_at,
            deleted_at=row.deleted_at,
            roles=row.roles.split(',') if row.roles else []
        )

    async def get_by_username(self, username: str) -> Optional[User]:
        """Retrieve user by username."""
        from sqlalchemy import text

        query = text("SELECT * FROM users WHERE LOWER(username) = LOWER(:username)")
        result = await self.db.execute(query, {"username": username})
        row = result.first()

        if not row:
            return None

        return User(
            user_id=row.user_id,
            username=row.username,
            email=row.email,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
            timezone=row.timezone,
            locale=row.locale,
            hashed_password=row.hashed_password,
            is_active=row.is_active,
            is_email_verified=row.is_email_verified,
            email_verified_at=row.email_verified_at,
            sso_provider=row.sso_provider,
            sso_provider_id=row.sso_provider_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_login_at=row.last_login_at,
            last_password_change_at=row.last_password_change_at,
            deleted_at=row.deleted_at,
            roles=row.roles.split(',') if row.roles else []
        )

    async def get_by_email(self, email: str) -> Optional[User]:
        """Retrieve user by email."""
        from sqlalchemy import text

        query = text("SELECT * FROM users WHERE LOWER(email) = LOWER(:email)")
        result = await self.db.execute(query, {"email": email})
        row = result.first()

        if not row:
            return None

        return User(
            user_id=row.user_id,
            username=row.username,
            email=row.email,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
            timezone=row.timezone,
            locale=row.locale,
            hashed_password=row.hashed_password,
            is_active=row.is_active,
            is_email_verified=row.is_email_verified,
            email_verified_at=row.email_verified_at,
            sso_provider=row.sso_provider,
            sso_provider_id=row.sso_provider_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_login_at=row.last_login_at,
            last_password_change_at=row.last_password_change_at,
            deleted_at=row.deleted_at,
            roles=row.roles.split(',') if row.roles else []
        )

    async def list(
        self,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List[User], int]:
        """List users with pagination."""
        from sqlalchemy import text

        # Get total count
        count_query = text("SELECT COUNT(*) FROM users")
        count_result = await self.db.execute(count_query)
        total_count = count_result.scalar()

        # Get paginated results
        query = text("""
            SELECT * FROM users
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)
        result = await self.db.execute(query, {"limit": limit, "offset": offset})
        rows = result.fetchall()

        users = [
            User(
                user_id=row.user_id,
                username=row.username,
                email=row.email,
                display_name=row.display_name,
                avatar_url=row.avatar_url,
                timezone=row.timezone,
                locale=row.locale,
                hashed_password=row.hashed_password,
                is_active=row.is_active,
                is_email_verified=row.is_email_verified,
                email_verified_at=row.email_verified_at,
                sso_provider=row.sso_provider,
                sso_provider_id=row.sso_provider_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
                last_login_at=row.last_login_at,
                last_password_change_at=row.last_password_change_at,
                deleted_at=row.deleted_at,
                roles=row.roles.split(',') if row.roles else []
            )
            for row in rows
        ]

        return users, total_count

    async def delete(self, user_id: str) -> bool:
        """Delete user from PostgreSQL."""
        from sqlalchemy import text

        query = text("DELETE FROM users WHERE user_id = :user_id")
        result = await self.db.execute(query, {"user_id": user_id})
        await self.db.commit()

        return result.rowcount > 0
