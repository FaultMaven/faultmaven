"""User Repository for user account persistence.

This module provides the repository pattern for User domain model persistence.
It abstracts database operations and provides clean interfaces for the service layer.

Adapters:
- InMemoryUserRepository: Development/testing (RAM storage)
- PostgreSQLUserRepository: Production (SQLAlchemy ORM)
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
    username: str = Field(
        ..., min_length=1, max_length=100, description="Unique username"
    )
    email: EmailStr = Field(..., description="User email address")
    enterprise_id: Optional[str] = Field(
        None,
        description=(
            "Enterprise (top-tier tenant) this user belongs to. Required to scope "
            "cross-tenant lookups — e.g. resolving an existing user by email within "
            "the caller's enterprise when adding them to an organization."
        ),
    )

    # ============================================================
    # Authentication
    # ============================================================
    hashed_password: Optional[str] = Field(
        None, description="Bcrypt password hash (NULL for SSO-only users)"
    )
    is_active: bool = Field(True, description="Account active status")

    # ============================================================
    # Profile
    # ============================================================
    display_name: str = Field(
        ..., min_length=1, max_length=200, description="Display name"
    )
    avatar_url: Optional[str] = Field(
        None, max_length=500, description="Profile picture URL"
    )
    timezone: str = Field("UTC", max_length=50, description="User timezone")
    locale: str = Field("en-US", max_length=10, description="User locale (i18n)")
    account_kind: str = Field(
        "individual",
        max_length=20,
        description="Account kind (ADR-012): 'individual' or 'slack'",
    )

    # ============================================================
    # Email Verification
    # ============================================================
    is_email_verified: bool = Field(False, description="Email verification status")
    email_verified_at: Optional[datetime] = Field(
        None, description="When email was verified"
    )

    # ============================================================
    # SSO Integration
    # ============================================================
    sso_provider: Optional[str] = Field(
        None, max_length=50, description="SSO provider (google, okta, azure)"
    )
    sso_provider_id: Optional[str] = Field(
        None, max_length=255, description="External user ID from SSO provider"
    )

    # ============================================================
    # Timestamps
    # ============================================================
    created_at: datetime = Field(..., description="Account creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    last_login_at: Optional[datetime] = Field(None, description="Last successful login")
    last_password_change_at: Optional[datetime] = Field(
        None, description="Last password change"
    )

    # ============================================================
    # Soft Delete
    # ============================================================
    deleted_at: Optional[datetime] = Field(
        None, description="Soft delete timestamp (NULL = active)"
    )

    # ============================================================
    # Authorization (Simplified for InMemory/Redis)
    # ============================================================
    roles: List[str] = Field(
        default_factory=list, description="User roles (development only)"
    )


# ============================================================
# Repository Interface
# ============================================================


class UserRepository(ABC):
    """Abstract repository interface for User persistence."""

    @abstractmethod
    async def save(self, user: User) -> User:
        """Save user to persistence layer (upsert)."""
        pass

    @abstractmethod
    async def get(self, user_id: str) -> Optional[User]:
        """Retrieve user by ID."""
        pass

    @abstractmethod
    async def get_by_username(self, username: str) -> Optional[User]:
        """Retrieve user by username."""
        pass

    @abstractmethod
    async def get_by_email(self, email: str) -> Optional[User]:
        """Retrieve user by email."""
        pass

    @abstractmethod
    async def get_by_sso(self, provider: str, provider_id: str) -> Optional[User]:
        """Retrieve user by external SSO subject (provider + provider_id)."""
        pass

    @abstractmethod
    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List[User], int]:
        """List users with pagination."""
        pass

    @abstractmethod
    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
    ) -> tuple[List[User], int]:
        """List users with pagination and optional active filter."""
        pass

    @abstractmethod
    async def update(self, user: User) -> User:
        """Update existing user."""
        pass

    @abstractmethod
    async def create(self, user: User) -> User:
        """Create a new user."""
        pass

    @abstractmethod
    async def delete(self, user_id: str) -> bool:
        """Delete user by ID."""
        pass


# ============================================================
# In-Memory Implementation (for Testing/Development)
# ============================================================


class InMemoryUserRepository(UserRepository):
    """In-memory user repository for testing and development."""

    def __init__(self):
        """Initialize empty in-memory store."""
        self._users: Dict[str, User] = {}
        self._username_index: Dict[str, str] = {}
        self._email_index: Dict[str, str] = {}

    async def save(self, user: User) -> User:
        """Save user to memory."""
        user.updated_at = datetime.now(timezone.utc)
        self._users[user.user_id] = user.model_copy(deep=True)
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

    async def get_by_sso(self, provider: str, provider_id: str) -> Optional[User]:
        """Get user by external SSO subject."""
        # Never match on empty subject: a password user has NULL sso fields, and
        # (None, None) must not resolve to it.
        if not provider or not provider_id:
            return None
        for user in self._users.values():
            if user.sso_provider == provider and user.sso_provider_id == provider_id:
                return user
        return None

    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List[User], int]:
        """List users with pagination."""
        all_users = list(self._users.values())
        all_users.sort(key=lambda u: u.created_at, reverse=True)
        total_count = len(all_users)
        paginated = all_users[offset : offset + limit]
        return paginated, total_count

    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
    ) -> tuple[List[User], int]:
        """List users with pagination and optional active filter."""
        all_users = list(self._users.values())
        if is_active is not None:
            all_users = [u for u in all_users if u.is_active == is_active]
        all_users.sort(key=lambda u: u.created_at, reverse=True)
        total_count = len(all_users)
        paginated = all_users[offset : offset + limit]
        return paginated, total_count

    async def create(self, user: User) -> User:
        """Create a new user."""
        from faultmaven.exceptions import ConflictError

        if user.email.lower() in self._email_index:
            raise ConflictError("Email already registered")
        if user.username.lower() in self._username_index:
            raise ConflictError("Username already taken")
        return await self.save(user)

    async def update(self, user: User) -> User:
        """Update existing user."""
        existing = self._users.get(user.user_id)
        if not existing:
            from faultmaven.exceptions import NotFoundError

            raise NotFoundError("User", user.user_id)

        original_email = existing.email
        original_username = existing.username

        if original_email.lower() != user.email.lower():
            existing_with_email = self._email_index.get(user.email.lower())
            if existing_with_email and existing_with_email != user.user_id:
                from faultmaven.exceptions import ConflictError

                raise ConflictError("Email already in use")
            self._email_index.pop(original_email.lower(), None)

        if original_username.lower() != user.username.lower():
            existing_with_username = self._username_index.get(user.username.lower())
            if existing_with_username and existing_with_username != user.user_id:
                from faultmaven.exceptions import ConflictError

                raise ConflictError("Username already taken")
            self._username_index.pop(original_username.lower(), None)

        return await self.save(user)

    async def delete(self, user_id: str) -> bool:
        """Delete user from memory."""
        user = self._users.get(user_id)
        if not user:
            return False
        self._username_index.pop(user.username.lower(), None)
        self._email_index.pop(user.email.lower(), None)
        del self._users[user_id]
        return True


# ============================================================
# SQLAlchemy ORM Implementation (Production)
# ============================================================


class PostgreSQLUserRepository(UserRepository):
    """SQLAlchemy ORM user repository for production use.

    Works with both SQLite and PostgreSQL via SQLAlchemy abstraction.
    """

    def __init__(self, db_session):
        """Initialize repository with database session."""
        self.db = db_session

    def _model_to_domain(self, model) -> User:
        """Convert UserModel ORM instance to User domain object.

        Roles come from the `dev_roles` TEXT column (JSON array) in BOTH auth
        modes today — no login/token path currently derives them from the RBAC
        tables (organization_members → roles). Deriving the cloud role claim
        from RBAC is designed but not yet wired; when it lands, callers that
        need effective cloud roles should use the org-repository query path
        rather than this field. Tracked in #706.
        """
        import json

        raw = getattr(model, "dev_roles", None)
        try:
            roles = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            roles = []

        return User(
            user_id=model.user_id,
            username=model.username,
            email=model.email,
            enterprise_id=getattr(model, "enterprise_id", None),
            display_name=model.display_name,
            avatar_url=model.avatar_url,
            timezone=model.timezone,
            locale=model.locale,
            hashed_password=model.hashed_password,
            is_active=model.is_active,
            is_email_verified=model.is_email_verified,
            email_verified_at=model.email_verified_at,
            sso_provider=model.sso_provider,
            sso_provider_id=model.sso_provider_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
            last_login_at=model.last_login_at,
            last_password_change_at=model.last_password_change_at,
            deleted_at=model.deleted_at,
            roles=roles,
            account_kind=getattr(model, "account_kind", "individual"),
        )

    def _domain_to_dict(self, user: User) -> dict:
        """Convert User domain object to dict for ORM model assignment.

        enterprise_id is taken from the user object; it falls back to
        DEFAULT_ENTERPRISE_ID when unset (standalone / single-tenant, where
        every user belongs to the one default enterprise) since the column is
        NOT NULL.
        """
        from faultmaven.providers.tenancy.single_tenant import DEFAULT_ENTERPRISE_ID

        enterprise_id = user.enterprise_id or DEFAULT_ENTERPRISE_ID
        return {
            "user_id": user.user_id,
            "enterprise_id": enterprise_id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "timezone": user.timezone,
            "locale": user.locale,
            "hashed_password": user.hashed_password,
            "is_active": user.is_active,
            "is_email_verified": user.is_email_verified,
            "email_verified_at": user.email_verified_at,
            "sso_provider": user.sso_provider,
            "sso_provider_id": user.sso_provider_id,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "last_login_at": user.last_login_at,
            "last_password_change_at": user.last_password_change_at,
            "deleted_at": user.deleted_at,
            "account_kind": getattr(user, "account_kind", "individual"),
            # dev_roles: JSON-serialised role list. This is the canonical role
            # source in BOTH auth modes today — no login/token path derives the
            # role claim from the RBAC tables yet (#706). Deriving cloud roles
            # from organization_members → roles is designed but not yet wired.
            "dev_roles": __import__("json").dumps(user.roles) if user.roles else None,
        }

    async def save(self, user: User) -> User:
        """Save user via ORM merge (upsert)."""
        from faultmaven.infrastructure.persistence.db_compat import dialect_insert
        from faultmaven.infrastructure.persistence.models import UserModel

        user.updated_at = datetime.now(timezone.utc)
        values = self._domain_to_dict(user)

        # Dialect-aware upsert (portable across SQLite and PostgreSQL)
        stmt = dialect_insert(self.db, UserModel).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                k: v for k, v in values.items() if k not in ("user_id", "created_at")
            },
        )
        await self.db.execute(stmt)
        await self.db.commit()
        return user

    async def get(self, user_id: str) -> Optional[User]:
        """Retrieve user by ID."""
        from sqlalchemy import select

        from faultmaven.infrastructure.persistence.models import UserModel

        stmt = select(UserModel).where(UserModel.user_id == user_id)
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return self._model_to_domain(model) if model else None

    async def get_by_username(self, username: str) -> Optional[User]:
        """Retrieve user by username (case-insensitive)."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import UserModel

        stmt = select(UserModel).where(
            func.lower(UserModel.username) == func.lower(username)
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return self._model_to_domain(model) if model else None

    async def get_by_email(self, email: str) -> Optional[User]:
        """Retrieve user by email (case-insensitive)."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import UserModel

        stmt = select(UserModel).where(func.lower(UserModel.email) == func.lower(email))
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return self._model_to_domain(model) if model else None

    async def get_by_sso(self, provider: str, provider_id: str) -> Optional[User]:
        """Retrieve user by external SSO subject (provider + provider_id)."""
        # Never match on empty subject: `sso_provider == None` renders as
        # `IS NULL` and would match every password (non-SSO) user.
        if not provider or not provider_id:
            return None

        from sqlalchemy import select

        from faultmaven.infrastructure.persistence.models import UserModel

        stmt = select(UserModel).where(
            UserModel.sso_provider == provider,
            UserModel.sso_provider_id == provider_id,
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return self._model_to_domain(model) if model else None

    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List[User], int]:
        """List users with pagination."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import UserModel

        count_stmt = select(func.count()).select_from(UserModel)
        count_result = await self.db.execute(count_stmt)
        total_count = count_result.scalar()

        stmt = (
            select(UserModel)
            .order_by(UserModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()

        return [self._model_to_domain(m) for m in models], total_count

    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
    ) -> tuple[List[User], int]:
        """List users with pagination and optional active filter."""
        from sqlalchemy import func, select

        from faultmaven.infrastructure.persistence.models import UserModel

        base_filter = []
        if is_active is not None:
            base_filter.append(UserModel.is_active == is_active)

        count_stmt = select(func.count()).select_from(UserModel).where(*base_filter)
        count_result = await self.db.execute(count_stmt)
        total_count = count_result.scalar()

        stmt = (
            select(UserModel)
            .where(*base_filter)
            .order_by(UserModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()

        return [self._model_to_domain(m) for m in models], total_count

    async def create(self, user: User) -> User:
        """Create a new user with uniqueness checks."""
        from faultmaven.exceptions import ConflictError

        if await self.get_by_email(user.email):
            raise ConflictError("Email already registered")
        if await self.get_by_username(user.username):
            raise ConflictError("Username already taken")
        return await self.save(user)

    async def update(self, user: User) -> User:
        """Update existing user with uniqueness checks."""
        from sqlalchemy import func, select

        from faultmaven.exceptions import ConflictError, NotFoundError
        from faultmaven.infrastructure.persistence.models import UserModel

        existing = await self.get(user.user_id)
        if not existing:
            raise NotFoundError("User", user.user_id)

        if existing.email.lower() != user.email.lower():
            stmt = select(UserModel.user_id).where(
                func.lower(UserModel.email) == func.lower(user.email),
                UserModel.user_id != user.user_id,
            )
            result = await self.db.execute(stmt)
            if result.first():
                raise ConflictError("Email already in use")

        if existing.username.lower() != user.username.lower():
            stmt = select(UserModel.user_id).where(
                func.lower(UserModel.username) == func.lower(user.username),
                UserModel.user_id != user.user_id,
            )
            result = await self.db.execute(stmt)
            if result.first():
                raise ConflictError("Username already taken")

        return await self.save(user)

    async def delete(self, user_id: str) -> bool:
        """Delete user."""
        from sqlalchemy import delete

        from faultmaven.infrastructure.persistence.models import UserModel

        stmt = delete(UserModel).where(UserModel.user_id == user_id)
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0


# ============================================================
# Sessionless Wrapper (Production DI)
# ============================================================


class SessionlessUserRepository(UserRepository):
    """Sessionless user repository that opens a session per operation.

    Holds NO session instance. Each method acquires a fresh session via
    ``get_db_session()`` — the context manager commits on success, rolls
    back on error, and always closes — then delegates to a per-call
    ``PostgreSQLUserRepository`` bound to that session.

    This follows Architectural Design Principle 5 (Composition Root — no
    shared session state), mirroring ``SessionlessCaseRepository``. It is
    the fix for the idle-in-transaction leak (#703): the previous wiring
    handed a single process-lifetime ``AsyncSession`` to a singleton store,
    and the read methods never committed/closed, so PostgreSQL left the
    backing connection ``idle in transaction`` indefinitely. Per-operation
    sessions end every transaction promptly, avoid sharing one AsyncSession
    across concurrent requests, and re-apply the RLS tenant scope per op.
    """

    async def save(self, user: User) -> User:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).save(user)

    async def get(self, user_id: str) -> Optional[User]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).get(user_id)

    async def get_by_username(self, username: str) -> Optional[User]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).get_by_username(username)

    async def get_by_email(self, email: str) -> Optional[User]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).get_by_email(email)

    async def get_by_sso(self, provider: str, provider_id: str) -> Optional[User]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).get_by_sso(
                provider, provider_id
            )

    async def list(self, limit: int = 50, offset: int = 0) -> tuple[List[User], int]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).list(
                limit=limit, offset=offset
            )

    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        is_active: Optional[bool] = None,
    ) -> tuple[List[User], int]:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).list_users(
                limit=limit, offset=offset, is_active=is_active
            )

    async def update(self, user: User) -> User:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).update(user)

    async def create(self, user: User) -> User:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).create(user)

    async def delete(self, user_id: str) -> bool:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            return await PostgreSQLUserRepository(session).delete(user_id)
