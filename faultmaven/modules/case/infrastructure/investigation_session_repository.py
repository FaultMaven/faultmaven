"""Investigation Session Repository - Database and In-Memory Implementations.

This module provides investigation session repository implementations for tracking
investigation sessions within cases.

Features:
- Full CRUD operations for investigation sessions
- Query by case with optional status filtering
- Get active session for a case
- Pagination support
- Support for both in-memory and database backends

Usage:
    from faultmaven.modules.case.infrastructure.investigation_session_repository import (
        DatabaseInvestigationSessionRepository,
        InMemoryInvestigationSessionRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseInvestigationSessionRepository(session)
        session = await repo.create(session)

    # In-memory usage (for testing)
    repo = InMemoryInvestigationSessionRepository()
    session = await repo.create(session)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from faultmaven.modules.case.domain.investigation_session import (
    InvestigationSession,
    SessionStatus,
)
from faultmaven.infrastructure.persistence.models import (
    InvestigationSessionModel,
)

logger = logging.getLogger(__name__)


class InvestigationSessionRepositoryException(Exception):
    """Exception raised when investigation session repository operations fail."""

    pass


class InvestigationSessionRepository(ABC):
    """Abstract base class for investigation session repositories."""

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    @abstractmethod
    async def create(self, session: InvestigationSession) -> InvestigationSession:
        """Create a new investigation session.

        Args:
            session: Investigation session to create

        Returns:
            Created investigation session with timestamps

        Raises:
            ValueError: If session_id already exists or case_id not found
            InvestigationSessionRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_by_id(self, session_id: str) -> Optional[InvestigationSession]:
        """Get session by ID.

        Args:
            session_id: Session identifier

        Returns:
            Investigation session if found, None otherwise
        """
        pass

    @abstractmethod
    async def update(self, session: InvestigationSession) -> InvestigationSession:
        """Update existing session.

        Args:
            session: Session with updated fields

        Returns:
            Updated session

        Raises:
            ValueError: If session not found
            InvestigationSessionRepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Delete session by ID.

        Args:
            session_id: Session identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    # =========================================================================
    # Query Operations
    # =========================================================================

    @abstractmethod
    async def list_by_case_id(
        self,
        case_id: str,
        status: Optional[SessionStatus] = None,
    ) -> List[InvestigationSession]:
        """List all sessions for a case, optionally filtered by status.

        Args:
            case_id: Case identifier
            status: Optional filter by session status

        Returns:
            List of sessions ordered by started_at DESC
        """
        pass

    @abstractmethod
    async def get_active_session(self, case_id: str) -> Optional[InvestigationSession]:
        """Get the currently active session for a case (if any).

        Args:
            case_id: Case identifier

        Returns:
            Active session if exists, None otherwise
        """
        pass

    @abstractmethod
    async def list_by_user_id(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[InvestigationSession]:
        """List sessions by user (paginated).

        Args:
            user_id: User identifier
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            List of sessions ordered by started_at DESC
        """
        pass

    @abstractmethod
    async def count_by_case_id(self, case_id: str) -> int:
        """Count sessions for a case.

        Args:
            case_id: Case identifier

        Returns:
            Number of sessions for the case
        """
        pass


class DatabaseInvestigationSessionRepository(InvestigationSessionRepository):
    """Database-backed investigation session repository using SQLAlchemy ORM."""

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create(self, session: InvestigationSession) -> InvestigationSession:
        """Create new investigation session record in the database."""
        try:
            # Convert domain model to ORM model
            session_model = InvestigationSessionModel(
                session_id=session.session_id,
                case_id=session.case_id,
                user_id=session.user_id,
                organization_id=session.organization_id,
                status=session.status.value,
                started_at=session.started_at,
                ended_at=session.ended_at,
                last_activity_at=session.last_activity_at,
                total_duration_ms=session.total_duration_ms,
                session_goal=session.session_goal,
                findings_summary=session.findings_summary,
                total_token_usage=session.total_token_usage,
                total_agent_executions=session.total_agent_executions,
                token_budget_limit=session.token_budget_limit,
                session_metadata=(
                    json.dumps(session.metadata) if session.metadata else "{}"
                ),
                created_at=session.created_at,
                updated_at=session.updated_at,
            )

            self.db.add(session_model)
            await self.db.commit()
            await self.db.refresh(session_model)

            logger.debug(f"Created investigation session {session.session_id}")
            return self._to_domain(session_model)

        except IntegrityError as e:
            await self.db.rollback()
            error_str = str(e).lower()
            if "foreign key" in error_str or "fk_" in error_str:
                logger.error(f"Case {session.case_id} not found")
                raise ValueError(f"Case {session.case_id} not found") from e
            logger.error(f"Investigation session {session.session_id} already exists")
            raise ValueError(
                f"Investigation session {session.session_id} already exists"
            ) from e

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create investigation session: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to create investigation session: {e}"
            ) from e

    async def get_by_id(self, session_id: str) -> Optional[InvestigationSession]:
        """Get session by ID."""
        try:
            stmt = select(InvestigationSessionModel).where(
                InvestigationSessionModel.session_id == session_id
            )
            result = await self.db.execute(stmt)
            session_model = result.scalar_one_or_none()

            if session_model is None:
                return None

            return self._to_domain(session_model)

        except Exception as e:
            logger.error(f"Failed to get investigation session {session_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to get investigation session {session_id}: {e}"
            ) from e

    async def update(self, session: InvestigationSession) -> InvestigationSession:
        """Update session status and fields."""
        try:
            session.updated_at = datetime.now(timezone.utc)

            stmt = (
                update(InvestigationSessionModel)
                .where(InvestigationSessionModel.session_id == session.session_id)
                .values(
                    status=session.status.value,
                    ended_at=session.ended_at,
                    last_activity_at=session.last_activity_at,
                    total_duration_ms=session.total_duration_ms,
                    session_goal=session.session_goal,
                    findings_summary=session.findings_summary,
                    total_token_usage=session.total_token_usage,
                    total_agent_executions=session.total_agent_executions,
                    token_budget_limit=session.token_budget_limit,
                    session_metadata=(
                        json.dumps(session.metadata) if session.metadata else "{}"
                    ),
                    updated_at=session.updated_at,
                )
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(stmt)

            if result.rowcount == 0:
                raise ValueError(
                    f"Investigation session {session.session_id} not found"
                )

            await self.db.commit()
            logger.debug(f"Updated investigation session {session.session_id}")
            return session

        except ValueError:
            raise

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update investigation session: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to update investigation session: {e}"
            ) from e

    async def delete(self, session_id: str) -> bool:
        """Delete session by ID."""
        try:
            stmt = delete(InvestigationSessionModel).where(
                InvestigationSessionModel.session_id == session_id
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted investigation session {session_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete investigation session {session_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to delete investigation session {session_id}: {e}"
            ) from e

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def list_by_case_id(
        self,
        case_id: str,
        status: Optional[SessionStatus] = None,
    ) -> List[InvestigationSession]:
        """List all sessions for a case, optionally filtered by status."""
        try:
            # Build query conditions
            conditions = [InvestigationSessionModel.case_id == case_id]
            if status:
                conditions.append(InvestigationSessionModel.status == status.value)

            where_clause = and_(*conditions)

            stmt = (
                select(InvestigationSessionModel)
                .where(where_clause)
                .order_by(InvestigationSessionModel.started_at.desc())
            )

            result = await self.db.execute(stmt)
            session_models = result.scalars().all()

            return [self._to_domain(model) for model in session_models]

        except Exception as e:
            logger.error(f"Failed to list sessions for case {case_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to list sessions for case {case_id}: {e}"
            ) from e

    async def get_active_session(self, case_id: str) -> Optional[InvestigationSession]:
        """Get the currently active session for a case (if any)."""
        try:
            stmt = (
                select(InvestigationSessionModel)
                .where(
                    and_(
                        InvestigationSessionModel.case_id == case_id,
                        InvestigationSessionModel.status == SessionStatus.ACTIVE.value,
                    )
                )
                .order_by(InvestigationSessionModel.started_at.desc())
                .limit(1)
            )

            result = await self.db.execute(stmt)
            session_model = result.scalar_one_or_none()

            if session_model is None:
                return None

            return self._to_domain(session_model)

        except Exception as e:
            logger.error(f"Failed to get active session for case {case_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to get active session for case {case_id}: {e}"
            ) from e

    async def list_by_user_id(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[InvestigationSession]:
        """List sessions by user (paginated)."""
        try:
            stmt = (
                select(InvestigationSessionModel)
                .where(InvestigationSessionModel.user_id == user_id)
                .order_by(InvestigationSessionModel.started_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            session_models = result.scalars().all()

            return [self._to_domain(model) for model in session_models]

        except Exception as e:
            logger.error(f"Failed to list sessions for user {user_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to list sessions for user {user_id}: {e}"
            ) from e

    async def count_by_case_id(self, case_id: str) -> int:
        """Count sessions for a case."""
        try:
            stmt = (
                select(func.count())
                .select_from(InvestigationSessionModel)
                .where(InvestigationSessionModel.case_id == case_id)
            )
            result = await self.db.execute(stmt)
            return result.scalar() or 0

        except Exception as e:
            logger.error(f"Failed to count sessions for case {case_id}: {e}")
            raise InvestigationSessionRepositoryException(
                f"Failed to count sessions for case {case_id}: {e}"
            ) from e

    # =========================================================================
    # Model Conversion Helpers
    # =========================================================================

    def _to_domain(self, model: InvestigationSessionModel) -> InvestigationSession:
        """Convert SQLAlchemy model to domain model."""
        metadata = None
        if model.session_metadata:
            try:
                metadata = json.loads(model.session_metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        return InvestigationSession(
            session_id=model.session_id,
            case_id=model.case_id,
            user_id=model.user_id,
            organization_id=model.organization_id,
            status=SessionStatus(model.status),
            started_at=self._ensure_tz_aware(model.started_at),
            ended_at=self._ensure_tz_aware(model.ended_at),
            last_activity_at=self._ensure_tz_aware(model.last_activity_at),
            total_duration_ms=model.total_duration_ms,
            session_goal=model.session_goal,
            findings_summary=model.findings_summary,
            total_token_usage=model.total_token_usage or 0,
            total_agent_executions=model.total_agent_executions or 0,
            token_budget_limit=model.token_budget_limit,
            metadata=metadata,
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
        )

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class InMemoryInvestigationSessionRepository(InvestigationSessionRepository):
    """In-memory investigation session repository for testing."""

    def __init__(self):
        """Initialize empty storage."""
        self._sessions: Dict[str, InvestigationSession] = {}

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    async def create(self, session: InvestigationSession) -> InvestigationSession:
        """Create new investigation session record in memory."""
        if session.session_id in self._sessions:
            raise ValueError(
                f"Investigation session {session.session_id} already exists"
            )

        # Store a deep copy to prevent mutation
        self._sessions[session.session_id] = deepcopy(session)
        return deepcopy(session)

    async def get_by_id(self, session_id: str) -> Optional[InvestigationSession]:
        """Get session by ID."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return deepcopy(session)

    async def update(self, session: InvestigationSession) -> InvestigationSession:
        """Update session status and fields."""
        if session.session_id not in self._sessions:
            raise ValueError(f"Investigation session {session.session_id} not found")

        session.updated_at = datetime.now(timezone.utc)
        self._sessions[session.session_id] = deepcopy(session)
        return deepcopy(session)

    async def delete(self, session_id: str) -> bool:
        """Delete session by ID."""
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        return True

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def list_by_case_id(
        self,
        case_id: str,
        status: Optional[SessionStatus] = None,
    ) -> List[InvestigationSession]:
        """List all sessions for a case, optionally filtered by status."""
        sessions = [s for s in self._sessions.values() if s.case_id == case_id]

        if status:
            sessions = [s for s in sessions if s.status == status]

        # Sort by started_at descending
        sessions.sort(key=lambda s: s.started_at, reverse=True)

        return [deepcopy(s) for s in sessions]

    async def get_active_session(self, case_id: str) -> Optional[InvestigationSession]:
        """Get the currently active session for a case (if any)."""
        active_sessions = [
            s
            for s in self._sessions.values()
            if s.case_id == case_id and s.status == SessionStatus.ACTIVE
        ]

        if not active_sessions:
            return None

        # Return most recent active session
        latest = max(active_sessions, key=lambda s: s.started_at)
        return deepcopy(latest)

    async def list_by_user_id(
        self,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[InvestigationSession]:
        """List sessions by user (paginated)."""
        sessions = [s for s in self._sessions.values() if s.user_id == user_id]

        # Sort by started_at descending
        sessions.sort(key=lambda s: s.started_at, reverse=True)

        # Apply pagination
        paginated = sessions[offset : offset + limit]

        return [deepcopy(s) for s in paginated]

    async def count_by_case_id(self, case_id: str) -> int:
        """Count sessions for a case."""
        return sum(1 for s in self._sessions.values() if s.case_id == case_id)

    # =========================================================================
    # Testing Helpers
    # =========================================================================

    def clear(self) -> None:
        """Clear all data (for testing)."""
        self._sessions.clear()

    def delete_sessions_for_case(self, case_id: str) -> int:
        """Delete all sessions for a case (simulates CASCADE from case delete).

        Returns:
            Number of sessions deleted
        """
        to_delete = [s_id for s_id, s in self._sessions.items() if s.case_id == case_id]

        for s_id in to_delete:
            del self._sessions[s_id]

        return len(to_delete)
