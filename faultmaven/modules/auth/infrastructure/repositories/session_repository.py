"""Session Repository - Database and In-Memory Implementations.

This module provides session repository implementations for managing
user sessions and their association with cases.

Features:
- Full CRUD operations for sessions
- Session expiry management
- Cleanup of expired sessions
- Support for both in-memory and database backends

Usage:
    from faultmaven.modules.auth.infrastructure.repositories.session_repository import (
        DatabaseSessionRepository,
        InMemorySessionRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseSessionRepository(session)
        user_session = await repo.create_session(session_data)

    # In-memory usage (for testing)
    repo = InMemorySessionRepository()
    user_session = await repo.create_session(session_data)
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import SessionModel
from faultmaven.modules.auth.domain.models.session import Session

logger = logging.getLogger(__name__)


class SessionRepositoryException(Exception):
    """Exception raised when session repository operations fail."""

    pass


class SessionRepository(ABC):
    """Abstract base class for session repositories."""

    @abstractmethod
    async def create_session(self, session: Session) -> Session:
        """Create a new session."""
        pass

    @abstractmethod
    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve session by ID."""
        pass

    @abstractmethod
    async def get_sessions_by_user(self, user_id: str) -> List[Session]:
        """Retrieve all sessions for a user."""
        pass

    @abstractmethod
    async def update_last_accessed(self, session_id: str) -> bool:
        """Update session last_accessed timestamp."""
        pass

    @abstractmethod
    async def update_session_metadata(self, session_id: str, metadata: Dict) -> bool:
        """Update session metadata."""
        pass

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        """Delete session (cases will be orphaned with session_id=NULL)."""
        pass

    @abstractmethod
    async def cleanup_expired_sessions(self) -> int:
        """Delete expired sessions."""
        pass


class DatabaseSessionRepository(SessionRepository):
    """Database-backed session repository using SQLAlchemy ORM."""

    def __init__(self, db_session: AsyncSession):
        """
        Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    async def create_session(self, session: Session) -> Session:
        """
        Create a new session in the database.

        Args:
            session: Session domain object to create

        Returns:
            Created session with database-generated timestamps

        Raises:
            SessionRepositoryException: If creation fails
        """
        try:
            session_model = SessionModel(
                session_id=session.session_id,
                user_id=session.user_id,
                created_at=session.created_at,
                last_accessed=session.last_accessed,
                expires_at=session.expires_at,
                session_metadata=(
                    json.dumps(session.metadata) if session.metadata else "{}"
                ),
            )
            self.db.add(session_model)
            await self.db.commit()
            await self.db.refresh(session_model)

            logger.debug(f"Created session {session.session_id}")
            return self._to_domain(session_model)

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create session: {e}")
            raise SessionRepositoryException(f"Failed to create session: {e}") from e

    async def get_session(self, session_id: str) -> Optional[Session]:
        """
        Retrieve session by ID.

        Args:
            session_id: Session identifier

        Returns:
            Session if found, None otherwise

        Raises:
            SessionRepositoryException: If retrieval fails
        """
        try:
            stmt = select(SessionModel).where(SessionModel.session_id == session_id)
            result = await self.db.execute(stmt)
            session_model = result.scalar_one_or_none()

            if session_model is None:
                return None

            return self._to_domain(session_model)

        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}")
            raise SessionRepositoryException(
                f"Failed to get session {session_id}: {e}"
            ) from e

    async def get_sessions_by_user(self, user_id: str) -> List[Session]:
        """
        Retrieve all sessions for a user.

        Args:
            user_id: User identifier

        Returns:
            List of sessions for the user

        Raises:
            SessionRepositoryException: If retrieval fails
        """
        try:
            stmt = (
                select(SessionModel)
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.last_accessed.desc())
            )
            result = await self.db.execute(stmt)
            session_models = result.scalars().all()

            return [self._to_domain(model) for model in session_models]

        except Exception as e:
            logger.error(f"Failed to get sessions for user {user_id}: {e}")
            raise SessionRepositoryException(
                f"Failed to get sessions for user {user_id}: {e}"
            ) from e

    async def update_last_accessed(self, session_id: str) -> bool:
        """
        Update session last_accessed timestamp.

        Args:
            session_id: Session identifier

        Returns:
            True if updated, False if session not found

        Raises:
            SessionRepositoryException: If update fails
        """
        try:
            stmt = (
                update(SessionModel)
                .where(SessionModel.session_id == session_id)
                .values(last_accessed=datetime.now(timezone.utc))
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            updated = result.rowcount > 0
            if updated:
                logger.debug(f"Updated last_accessed for session {session_id}")
            return updated

        except Exception as e:
            await self.db.rollback()
            logger.error(
                f"Failed to update last_accessed for session {session_id}: {e}"
            )
            raise SessionRepositoryException(
                f"Failed to update last_accessed for session {session_id}: {e}"
            ) from e

    async def update_session_metadata(self, session_id: str, metadata: Dict) -> bool:
        """
        Update session metadata.

        Args:
            session_id: Session identifier
            metadata: New metadata dictionary

        Returns:
            True if updated, False if session not found

        Raises:
            SessionRepositoryException: If update fails
        """
        try:
            stmt = (
                update(SessionModel)
                .where(SessionModel.session_id == session_id)
                .values(
                    session_metadata=json.dumps(metadata),
                    last_accessed=datetime.now(timezone.utc),
                )
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            updated = result.rowcount > 0
            if updated:
                logger.debug(f"Updated metadata for session {session_id}")
            return updated

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update metadata for session {session_id}: {e}")
            raise SessionRepositoryException(
                f"Failed to update metadata for session {session_id}: {e}"
            ) from e

    async def delete_session(self, session_id: str) -> bool:
        """
        Delete session.

        Cases linked to this session will have their session_id set to NULL
        due to the ON DELETE SET NULL constraint.

        Args:
            session_id: Session identifier

        Returns:
            True if deleted, False if session not found

        Raises:
            SessionRepositoryException: If deletion fails
        """
        try:
            stmt = delete(SessionModel).where(SessionModel.session_id == session_id)
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted session {session_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete session {session_id}: {e}")
            raise SessionRepositoryException(
                f"Failed to delete session {session_id}: {e}"
            ) from e

    async def cleanup_expired_sessions(self) -> int:
        """
        Delete expired sessions.

        Sessions with expires_at in the past are considered expired.
        Sessions without expires_at never expire.

        Returns:
            Number of sessions deleted

        Raises:
            SessionRepositoryException: If cleanup fails
        """
        try:
            now = datetime.now(timezone.utc)
            stmt = delete(SessionModel).where(
                and_(SessionModel.expires_at.isnot(None), SessionModel.expires_at < now)
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted_count = result.rowcount
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired sessions")
            return deleted_count

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to cleanup expired sessions: {e}")
            raise SessionRepositoryException(
                f"Failed to cleanup expired sessions: {e}"
            ) from e

    def _to_domain(self, model: SessionModel) -> Session:
        """Convert SQLAlchemy model to domain model."""
        metadata = None
        if model.session_metadata:
            try:
                metadata = json.loads(model.session_metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        return Session(
            session_id=model.session_id,
            user_id=model.user_id,
            created_at=self._ensure_tz_aware(model.created_at),
            last_accessed=self._ensure_tz_aware(model.last_accessed),
            expires_at=(
                self._ensure_tz_aware(model.expires_at) if model.expires_at else None
            ),
            metadata=metadata,
        )

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class InMemorySessionRepository(SessionRepository):
    """In-memory session repository for testing."""

    def __init__(self):
        """Initialize empty session storage."""
        self._sessions: Dict[str, Session] = {}

    async def create_session(self, session: Session) -> Session:
        """Create a new session in memory."""
        self._sessions[session.session_id] = session
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve session by ID."""
        return self._sessions.get(session_id)

    async def get_sessions_by_user(self, user_id: str) -> List[Session]:
        """Retrieve all sessions for a user."""
        return [s for s in self._sessions.values() if s.user_id == user_id]

    async def update_last_accessed(self, session_id: str) -> bool:
        """Update session last_accessed timestamp."""
        session = self._sessions.get(session_id)
        if session:
            session.touch()
            return True
        return False

    async def update_session_metadata(self, session_id: str, metadata: Dict) -> bool:
        """Update session metadata."""
        session = self._sessions.get(session_id)
        if session:
            session.metadata = metadata
            session.touch()
            return True
        return False

    async def delete_session(self, session_id: str) -> bool:
        """Delete session from memory."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    async def cleanup_expired_sessions(self) -> int:
        """Delete expired sessions from memory."""
        now = datetime.now(timezone.utc)
        expired_ids = [
            sid
            for sid, s in self._sessions.items()
            if s.expires_at and s.expires_at < now
        ]
        for sid in expired_ids:
            del self._sessions[sid]
        return len(expired_ids)

    def clear(self) -> None:
        """Clear all sessions (for testing)."""
        self._sessions.clear()
