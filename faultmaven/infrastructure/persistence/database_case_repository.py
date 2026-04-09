"""Database Case Repository - SQLAlchemy ORM Implementation.

This module implements the CaseRepository interface using SQLAlchemy ORM.
It provides persistent case storage with proper transaction handling.

Features:
- Full CRUD operations for cases
- Automatic domain <-> ORM model conversion
- Transaction management with rollback on errors
- Support for both SQLite (dev) and PostgreSQL (prod)
- Efficient pagination and filtering
- Full-text search support

Usage:
    from faultmaven.infrastructure.persistence.database_case_repository import DatabaseCaseRepository
    from faultmaven.infrastructure.persistence.database import get_db_session

    async with get_db_session() as session:
        repo = DatabaseCaseRepository(session)
        case = await repo.get("case_abc123def456")
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from faultmaven.infrastructure.persistence.case_repository import (
    CaseRepository,
    RepositoryException,
)
from faultmaven.infrastructure.persistence.models import (
    CaseActionModel,
    CaseMessageModel,
    CaseModel,
    CaseTagModel,
    EvidenceModel,
    HypothesisModel,
    SolutionModel,
    UploadedFileModel,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseStatus,
    DocumentationData,
    EscalationState,
    Evidence,
    Hypothesis,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    JournalEntry,
    PathSelection,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
)

logger = logging.getLogger(__name__)


class DatabaseCaseRepository(CaseRepository):
    """
    SQLAlchemy ORM-based case repository.

    Provides persistent storage for cases with full transaction support.
    Uses the hybrid schema: normalized tables for high-cardinality data,
    JSON columns for flexible embedded data.
    """

    def __init__(self, db_session: AsyncSession):
        """
        Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    # ========================================================================
    # Core CRUD Operations
    # ========================================================================

    async def save(self, case: Case) -> Case:
        """
        Save or update a case in the database.

        Uses merge/upsert pattern for idempotent saves.
        Handles all related entities in a single transaction.

        Args:
            case: Case domain object to save

        Returns:
            Saved case with updated timestamps

        Raises:
            RepositoryException: If save fails
        """
        try:
            # Update timestamp
            case.updated_at = datetime.now(timezone.utc)

            # Convert domain model to ORM model
            case_model = self._case_to_model(case)

            # Merge (upsert) the case
            merged = await self.db.merge(case_model)
            await self.db.flush()

            # Handle related entities
            await self._sync_messages(case.case_id, case.messages)
            await self._sync_case_actions(case.case_id, case.action_history)

            await self.db.commit()

            logger.debug(f"Saved case {case.case_id}")
            return case

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to save case {case.case_id}: {e}")
            raise RepositoryException(f"Failed to save case {case.case_id}: {e}") from e

    async def get(self, case_id: str) -> Optional[Case]:
        """
        Retrieve a case by ID.

        Loads case with all related entities efficiently using JOINs.

        Args:
            case_id: Case identifier

        Returns:
            Case if found, None otherwise

        Raises:
            RepositoryException: If retrieval fails
        """
        try:
            # Query with eager loading of relationships
            stmt = (
                select(CaseModel)
                .options(
                    selectinload(CaseModel.evidence),
                    selectinload(CaseModel.hypotheses),
                    selectinload(CaseModel.solutions),
                    selectinload(CaseModel.messages),
                    selectinload(CaseModel.uploaded_files),
                    selectinload(CaseModel.case_actions),
                    selectinload(CaseModel.tags),
                )
                .where(CaseModel.case_id == case_id)
            )

            result = await self.db.execute(stmt)
            case_model = result.scalar_one_or_none()

            if case_model is None:
                return None

            # Convert ORM model to domain model
            return self._model_to_case(case_model)

        except Exception as e:
            logger.error(f"Failed to get case {case_id}: {e}")
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters and pagination.

        Args:
            user_id: Filter by user
            organization_id: Filter by organization
            status: Filter by status
            limit: Maximum results (default 50)
            offset: Pagination offset

        Returns:
            Tuple of (cases, total_count)

        Raises:
            RepositoryException: If query fails
        """
        try:
            # Build filter conditions
            conditions = []
            if user_id:
                conditions.append(CaseModel.user_id == user_id)
            if organization_id:
                conditions.append(CaseModel.organization_id == organization_id)
            if status:
                conditions.append(CaseModel.status == status.value)

            # Count query
            count_stmt = select(func.count()).select_from(CaseModel)
            if conditions:
                count_stmt = count_stmt.where(and_(*conditions))

            count_result = await self.db.execute(count_stmt)
            total_count = count_result.scalar()

            # Data query with eager loading
            data_stmt = select(CaseModel).options(
                selectinload(CaseModel.evidence),
                selectinload(CaseModel.hypotheses),
                selectinload(CaseModel.solutions),
                selectinload(CaseModel.messages),
                selectinload(CaseModel.uploaded_files),
                selectinload(CaseModel.case_actions),
            )
            if conditions:
                data_stmt = data_stmt.where(and_(*conditions))

            data_stmt = (
                data_stmt.order_by(CaseModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(data_stmt)
            case_models = result.scalars().all()

            cases = [self._model_to_case(m) for m in case_models]

            return cases, total_count

        except Exception as e:
            logger.error(f"Failed to list cases: {e}")
            raise RepositoryException(f"Failed to list cases: {e}") from e

    async def delete(self, case_id: str) -> bool:
        """
        Delete a case by ID.

        Cascade deletes all related entities.

        Args:
            case_id: Case identifier

        Returns:
            True if deleted, False if not found

        Raises:
            RepositoryException: If deletion fails
        """
        try:
            stmt = delete(CaseModel).where(CaseModel.case_id == case_id)
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted case {case_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete case {case_id}: {e}")
            raise RepositoryException(f"Failed to delete case {case_id}: {e}") from e

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """
        Search cases by text query.

        Searches in title and description fields.

        Args:
            query: Search query
            user_id: Filter by user
            organization_id: Filter by organization
            limit: Maximum results

        Returns:
            Tuple of (cases, total_count)

        Raises:
            RepositoryException: If search fails
        """
        try:
            search_pattern = f"%{query}%"

            # Build filter conditions
            search_condition = or_(
                CaseModel.title.ilike(search_pattern),
                CaseModel.inquiry.ilike(search_pattern),
            )

            conditions = [search_condition]
            if user_id:
                conditions.append(CaseModel.user_id == user_id)
            if organization_id:
                conditions.append(CaseModel.organization_id == organization_id)

            # Count query
            count_stmt = (
                select(func.count()).select_from(CaseModel).where(and_(*conditions))
            )
            count_result = await self.db.execute(count_stmt)
            total_count = count_result.scalar()

            # Data query
            data_stmt = (
                select(CaseModel)
                .options(
                    selectinload(CaseModel.evidence),
                    selectinload(CaseModel.hypotheses),
                    selectinload(CaseModel.solutions),
                    selectinload(CaseModel.messages),
                    selectinload(CaseModel.uploaded_files),
                    selectinload(CaseModel.case_actions),
                )
                .where(and_(*conditions))
                .order_by(CaseModel.updated_at.desc())
                .limit(limit)
            )

            result = await self.db.execute(data_stmt)
            case_models = result.scalars().all()

            cases = [self._model_to_case(m) for m in case_models]

            return cases, total_count

        except Exception as e:
            logger.error(f"Failed to search cases: {e}")
            raise RepositoryException(f"Failed to search cases: {e}") from e

    # ========================================================================
    # Message Operations
    # ========================================================================

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """
        Add a message to a case.

        Args:
            case_id: Case identifier
            message_dict: Message data dictionary

        Returns:
            True if message was added successfully

        Raises:
            RepositoryException: If add fails
        """
        try:
            # Check if case exists
            case_exists = await self.db.execute(
                select(CaseModel.case_id).where(CaseModel.case_id == case_id)
            )
            if case_exists.scalar_one_or_none() is None:
                return False

            # Create message model
            # Accept both 'timestamp' (legacy) and 'created_at' (design spec)
            message_id = message_dict.get("message_id", f"msg_{uuid4().hex[:12]}")
            created_at = (
                message_dict.get("created_at")
                or message_dict.get("timestamp")
                or datetime.now(timezone.utc)
            )
            message_model = CaseMessageModel(
                message_id=message_id,
                case_id=case_id,
                turn_number=message_dict.get("turn_number", 0),
                role=message_dict.get("role", "user"),
                content=message_dict.get("content", ""),
                created_at=created_at,
                token_count=message_dict.get("token_count"),
                message_metadata=json.dumps(message_dict.get("metadata", {})),
            )

            self.db.add(message_model)

            # Update case activity timestamp
            await self.db.execute(
                update(CaseModel)
                .where(CaseModel.case_id == case_id)
                .values(updated_at=datetime.now(timezone.utc))
            )

            await self.db.commit()
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to add message to case {case_id}: {e}")
            raise RepositoryException(
                f"Failed to add message to case {case_id}: {e}"
            ) from e

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """
        Get messages for a case with pagination.

        Args:
            case_id: Case identifier
            limit: Maximum messages to return
            offset: Pagination offset

        Returns:
            List of message dictionaries

        Raises:
            RepositoryException: If retrieval fails
        """
        try:
            stmt = (
                select(CaseMessageModel)
                .where(CaseMessageModel.case_id == case_id)
                .order_by(CaseMessageModel.created_at.asc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            message_models = result.scalars().all()

            messages = []
            for m in message_models:
                messages.append(
                    {
                        "message_id": m.message_id,
                        "case_id": m.case_id,
                        "turn_number": m.turn_number,
                        "role": m.role,
                        "content": m.content,
                        "created_at": (
                            m.created_at.isoformat() if m.created_at else None
                        ),
                        "token_count": m.token_count,
                        "metadata": (
                            json.loads(m.message_metadata) if m.message_metadata else {}
                        ),
                    }
                )

            return messages

        except Exception as e:
            logger.error(f"Failed to get messages for case {case_id}: {e}")
            raise RepositoryException(
                f"Failed to get messages for case {case_id}: {e}"
            ) from e

    # ========================================================================
    # Activity & Analytics
    # ========================================================================

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """
        Update case last_activity_at timestamp.

        Args:
            case_id: Case identifier

        Returns:
            True if updated successfully

        Raises:
            RepositoryException: If update fails
        """
        try:
            stmt = (
                update(CaseModel)
                .where(CaseModel.case_id == case_id)
                .values(updated_at=datetime.now(timezone.utc))
            )

            result = await self.db.execute(stmt)
            await self.db.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update activity timestamp for case {case_id}: {e}")
            raise RepositoryException(
                f"Failed to update activity timestamp for case {case_id}: {e}"
            ) from e

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """
        Compute analytics for a case.

        Args:
            case_id: Case identifier

        Returns:
            Dictionary with analytics data

        Raises:
            RepositoryException: If computation fails
        """
        try:
            case = await self.get(case_id)
            if case is None:
                return {}

            analytics = {
                "case_id": case.case_id,
                "status": case.status.value,
                "created_at": case.created_at.isoformat() if case.created_at else None,
                "updated_at": case.updated_at.isoformat() if case.updated_at else None,
                "last_activity_at": (
                    case.last_activity_at.isoformat() if case.last_activity_at else None
                ),
                "message_count": case.message_count,
                "current_turn": case.current_turn,
                "turns_without_progress": case.turns_without_progress,
                "evidence_count": len(case.evidence),
                "hypothesis_count": len(case.hypotheses),
                "solution_count": len(case.solutions),
                "investigation_strategy": case.investigation_strategy.value,
                "has_working_conclusion": case.working_conclusion is not None,
                "has_root_cause": case.root_cause_conclusion is not None,
                "is_escalated": case.escalation_state is not None,
            }

            if case.resolved_at:
                analytics["resolved_at"] = case.resolved_at.isoformat()
                duration = (case.resolved_at - case.created_at).total_seconds()
                analytics["resolution_time_seconds"] = duration

            return analytics

        except Exception as e:
            logger.error(f"Failed to compute analytics for case {case_id}: {e}")
            raise RepositoryException(
                f"Failed to compute analytics for case {case_id}: {e}"
            ) from e

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """
        Clean up expired/old closed cases.

        Args:
            max_age_days: Maximum age in days for closed cases
            batch_size: Maximum cases to process in one batch

        Returns:
            Number of cases deleted

        Raises:
            RepositoryException: If cleanup fails
        """
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)

            # Find expired cases - use closed_at from metadata
            # Need to extract closed_at from JSONB metadata column
            stmt = (
                select(CaseModel.case_id, CaseModel.case_metadata)
                .where(CaseModel.status == "closed")
                .limit(batch_size * 2)  # Fetch more to filter by closed_at in Python
            )

            result = await self.db.execute(stmt)
            rows = result.fetchall()

            # Filter cases by closed_at timestamp from metadata
            expired_case_ids = []
            for row in rows:
                case_id = row[0]
                metadata = self._parse_json(row[1], {})
                closed_at_str = metadata.get("closed_at")
                if closed_at_str:
                    closed_at = self._parse_datetime(closed_at_str)
                    if closed_at and closed_at < cutoff_date:
                        expired_case_ids.append(case_id)
                        if len(expired_case_ids) >= batch_size:
                            break

            if not expired_case_ids:
                return 0

            # Delete expired cases
            delete_stmt = delete(CaseModel).where(
                CaseModel.case_id.in_(expired_case_ids)
            )
            result = await self.db.execute(delete_stmt)
            await self.db.commit()

            deleted_count = result.rowcount
            logger.info(f"Cleaned up {deleted_count} expired cases")

            return deleted_count

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to cleanup expired cases: {e}")
            raise RepositoryException(f"Failed to cleanup expired cases: {e}") from e

    # ========================================================================
    # Session-Aware Methods
    # ========================================================================

    async def get_cases_by_session(self, session_id: str) -> List[Case]:
        """
        Get all cases associated with a session.

        Args:
            session_id: Session identifier

        Returns:
            List of cases linked to the session

        Raises:
            RepositoryException: If query fails
        """
        try:
            stmt = (
                select(CaseModel)
                .options(
                    selectinload(CaseModel.evidence),
                    selectinload(CaseModel.hypotheses),
                    selectinload(CaseModel.solutions),
                    selectinload(CaseModel.messages),
                    selectinload(CaseModel.uploaded_files),
                    selectinload(CaseModel.case_actions),
                )
                .where(CaseModel.session_id == session_id)
                .order_by(CaseModel.updated_at.desc())
            )

            result = await self.db.execute(stmt)
            case_models = result.scalars().all()

            return [self._model_to_case(m) for m in case_models]

        except Exception as e:
            logger.error(f"Failed to get cases for session {session_id}: {e}")
            raise RepositoryException(
                f"Failed to get cases for session {session_id}: {e}"
            ) from e

    async def get_orphaned_cases(
        self, user_id: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> tuple[List[Case], int]:
        """
        Get cases with no session (session_id is NULL).

        These are cases that were either created without a session
        or whose session has been deleted.

        Args:
            user_id: Optional filter by user
            limit: Maximum results
            offset: Pagination offset

        Returns:
            Tuple of (cases, total_count)

        Raises:
            RepositoryException: If query fails
        """
        try:
            # Build conditions
            conditions = [CaseModel.session_id.is_(None)]
            if user_id:
                conditions.append(CaseModel.user_id == user_id)

            # Count query
            count_stmt = (
                select(func.count()).select_from(CaseModel).where(and_(*conditions))
            )
            count_result = await self.db.execute(count_stmt)
            total_count = count_result.scalar()

            # Data query
            data_stmt = (
                select(CaseModel)
                .options(
                    selectinload(CaseModel.evidence),
                    selectinload(CaseModel.hypotheses),
                    selectinload(CaseModel.solutions),
                    selectinload(CaseModel.messages),
                    selectinload(CaseModel.uploaded_files),
                    selectinload(CaseModel.case_actions),
                )
                .where(and_(*conditions))
                .order_by(CaseModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(data_stmt)
            case_models = result.scalars().all()

            return [self._model_to_case(m) for m in case_models], total_count

        except Exception as e:
            logger.error(f"Failed to get orphaned cases: {e}")
            raise RepositoryException(f"Failed to get orphaned cases: {e}") from e

    async def link_case_to_session(
        self, case_id: str, session_id: Optional[str]
    ) -> bool:
        """
        Link or unlink a case to/from a session.

        Args:
            case_id: Case identifier
            session_id: Session identifier, or None to unlink

        Returns:
            True if updated, False if case not found

        Raises:
            RepositoryException: If update fails
        """
        try:
            stmt = (
                update(CaseModel)
                .where(CaseModel.case_id == case_id)
                .values(session_id=session_id, updated_at=datetime.now(timezone.utc))
            )

            result = await self.db.execute(stmt)
            await self.db.commit()

            updated = result.rowcount > 0
            if updated:
                action = "linked to" if session_id else "unlinked from"
                logger.debug(f"Case {case_id} {action} session {session_id}")
            return updated

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to link case {case_id} to session: {e}")
            raise RepositoryException(
                f"Failed to link case {case_id} to session: {e}"
            ) from e

    async def save_with_session(
        self, case: Case, session_id: Optional[str] = None
    ) -> Case:
        """
        Save a case with optional session linkage.

        This is a convenience method that combines save() with session linking.

        Args:
            case: Case domain object to save
            session_id: Optional session ID to link the case to

        Returns:
            Saved case

        Raises:
            RepositoryException: If save fails
        """
        try:
            # Update timestamp
            case.updated_at = datetime.now(timezone.utc)

            # Convert domain model to ORM model
            case_model = self._case_to_model(case)

            # Set session_id on the model
            case_model.session_id = session_id

            # Merge (upsert) the case
            merged = await self.db.merge(case_model)
            await self.db.flush()

            # Handle related entities
            await self._sync_messages(case.case_id, case.messages)
            await self._sync_case_actions(case.case_id, case.action_history)

            await self.db.commit()

            logger.debug(f"Saved case {case.case_id} with session {session_id}")
            return case

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to save case {case.case_id} with session: {e}")
            raise RepositoryException(
                f"Failed to save case {case.case_id} with session: {e}"
            ) from e

    # ========================================================================
    # Helper: Sync Related Entities
    # ========================================================================

    async def _sync_messages(
        self, case_id: str, messages: List[Dict[str, Any]]
    ) -> None:
        """Sync messages for a case (append-only)."""
        if not messages:
            return

        # Get existing message IDs
        stmt = select(CaseMessageModel.message_id).where(
            CaseMessageModel.case_id == case_id
        )
        result = await self.db.execute(stmt)
        existing_ids = {row[0] for row in result.fetchall()}

        # Add new messages
        for msg in messages:
            msg_id = msg.get("message_id")
            if msg_id and msg_id not in existing_ids:
                message_model = CaseMessageModel(
                    message_id=msg_id,
                    case_id=case_id,
                    turn_number=msg.get("turn_number", 0),
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    created_at=self._parse_datetime(msg.get("created_at")),
                    token_count=msg.get("token_count"),
                    message_metadata=json.dumps(msg.get("metadata", {})),
                )
                self.db.add(message_model)

    async def _sync_case_actions(self, case_id: str, actions: List[CaseAction]) -> None:
        """Sync case actions for a case (append-only)."""
        if not actions:
            return

        # Get existing action count
        stmt = (
            select(func.count())
            .select_from(CaseActionModel)
            .where(CaseActionModel.case_id == case_id)
        )
        result = await self.db.execute(stmt)
        existing_count = result.scalar()

        # Add new actions
        for i, t in enumerate(actions):
            if i >= existing_count:
                action_model = CaseActionModel(
                    case_id=case_id,
                    from_status=t.from_status.value,
                    to_status=t.to_status.value,
                    reason=t.reason,
                    transitioned_at=t.triggered_at,
                    transition_metadata=json.dumps({"triggered_by": t.triggered_by}),
                )
                self.db.add(action_model)

    # ========================================================================
    # Model Conversion: Domain -> ORM
    # ========================================================================

    def _case_to_model(self, case: Case) -> CaseModel:
        """Convert Case domain model to CaseModel ORM model."""
        # Serialize complex fields to JSON (use mode='json' to serialize datetime objects)
        inquiry_json = (
            json.dumps(case.inquiry.model_dump(mode="json")) if case.inquiry else "{}"
        )
        progress_json = (
            json.dumps(case.progress.model_dump(mode="json")) if case.progress else "{}"
        )
        documentation_json = (
            json.dumps(case.documentation.model_dump(mode="json"))
            if case.documentation
            else "{}"
        )

        # Optional JSONB fields
        problem_verification_json = (
            json.dumps(case.problem_verification.model_dump(mode="json"))
            if case.problem_verification
            else None
        )
        working_conclusion_json = (
            json.dumps(case.working_conclusion.model_dump(mode="json"))
            if case.working_conclusion
            else None
        )
        root_cause_conclusion_json = (
            json.dumps(case.root_cause_conclusion.model_dump(mode="json"))
            if case.root_cause_conclusion
            else None
        )
        path_selection_json = (
            json.dumps(case.path_selection.model_dump(mode="json"))
            if case.path_selection
            else None
        )
        escalation_state_json = (
            json.dumps(case.escalation_state.model_dump(mode="json"))
            if case.escalation_state
            else None
        )

        # Build metadata (use mode='json' to serialize datetime objects)
        metadata = {
            "description": case.description,
            "investigation_strategy": case.investigation_strategy.value,
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "message_count": case.message_count,
            "closure_reason": case.closure_reason,
            "turn_history": [t.model_dump(mode="json") for t in case.turn_history],
            "uploaded_files": [f.model_dump(mode="json") for f in case.uploaded_files],
            "evidence": [e.model_dump(mode="json") for e in case.evidence],
            "hypotheses": {
                k: v.model_dump(mode="json") for k, v in case.hypotheses.items()
            },
            "solutions": [s.model_dump(mode="json") for s in case.solutions],
            "investigation_journal": [
                j.model_dump(mode="json") for j in case.investigation_journal
            ],
            "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
            "closed_at": case.closed_at.isoformat() if case.closed_at else None,
            "last_activity_at": (
                case.last_activity_at.isoformat() if case.last_activity_at else None
            ),
        }

        return CaseModel(
            case_id=case.case_id,
            user_id=case.user_id,
            title=case.title,
            status=case.status.value,
            created_at=case.created_at,
            updated_at=case.updated_at,
            inquiry=inquiry_json,
            problem_verification=problem_verification_json,
            working_conclusion=working_conclusion_json,
            root_cause_conclusion=root_cause_conclusion_json,
            path_selection=path_selection_json,
            escalation_state=escalation_state_json,
            documentation=documentation_json,
            progress=progress_json,
            case_metadata=json.dumps(metadata),
            organization_id=case.organization_id,
        )

    # ========================================================================
    # Model Conversion: ORM -> Domain
    # ========================================================================

    def _model_to_case(self, model: CaseModel) -> Case:
        """Convert CaseModel ORM model to Case domain model."""
        # Parse JSONB fields
        inquiry = self._parse_inquiry(model.inquiry)
        progress = self._parse_progress(model.progress)
        documentation = self._parse_documentation(model.documentation)
        metadata = self._parse_json(model.case_metadata, {})

        # Optional JSONB fields
        problem_verification = self._parse_problem_verification(
            model.problem_verification
        )
        working_conclusion = self._parse_working_conclusion(model.working_conclusion)
        root_cause_conclusion = self._parse_root_cause_conclusion(
            model.root_cause_conclusion
        )
        path_selection = self._parse_path_selection(model.path_selection)
        escalation_state = self._parse_escalation_state(model.escalation_state)

        # Extract fields from metadata
        description = metadata.get("description", "")

        # Backward compatibility: Fix description for old INVESTIGATING cases
        # Old cases may be in INVESTIGATING status with empty description
        if (
            model.status == CaseStatus.INVESTIGATING.value
            and (not description or not description.strip())
            and inquiry.proposed_problem_statement
        ):
            description = inquiry.proposed_problem_statement
            # Log only in debug mode to avoid production log spam
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Auto-healed missing description for case {model.case_id} "
                    f"from proposed_problem_statement"
                )

        investigation_strategy = InvestigationStrategy(
            metadata.get("investigation_strategy", "post_mortem")
        )
        current_turn = metadata.get("current_turn", 0)
        turns_without_progress = metadata.get("turns_without_progress", 0)
        message_count = metadata.get("message_count", 0)
        closure_reason = metadata.get("closure_reason")

        # Parse complex lists from metadata
        turn_history = self._parse_turn_history(metadata.get("turn_history", []))
        uploaded_files = self._parse_uploaded_files(metadata.get("uploaded_files", []))
        evidence = self._parse_evidence(metadata.get("evidence", []))
        hypotheses = self._parse_hypotheses(metadata.get("hypotheses", {}))
        solutions = self._parse_solutions(metadata.get("solutions", []))
        investigation_journal = self._parse_investigation_journal(
            metadata.get("investigation_journal", [])
        )

        # Parse messages from relationship
        messages = []
        if model.messages:
            for msg in model.messages:
                messages.append(
                    {
                        "message_id": msg.message_id,
                        "turn_number": msg.turn_number,
                        "role": msg.role,
                        "content": msg.content,
                        "created_at": (
                            msg.created_at.isoformat() if msg.created_at else None
                        ),
                        "token_count": msg.token_count,
                        "metadata": self._parse_json(msg.message_metadata, {}),
                    }
                )

        # Parse case actions from relationship
        action_history = []
        if model.case_actions:
            for t in model.case_actions:
                try:
                    action = CaseAction(
                        from_status=(
                            CaseStatus(t.from_status)
                            if t.from_status
                            else CaseStatus.INQUIRY
                        ),
                        to_status=CaseStatus(t.to_status),
                        triggered_at=t.transitioned_at,
                        triggered_by=self._parse_json(t.transition_metadata, {}).get(
                            "triggered_by", "system"
                        ),
                        reason=t.reason or "",
                    )
                    action_history.append(action)
                except Exception:
                    pass  # Skip invalid actions

        # Parse timestamps from metadata
        resolved_at = self._parse_datetime(metadata.get("resolved_at"))
        closed_at = self._parse_datetime(metadata.get("closed_at"))
        last_activity_at = self._parse_datetime(
            metadata.get("last_activity_at")
        ) or self._ensure_tz_aware(model.updated_at)

        return Case(
            case_id=model.case_id,
            user_id=model.user_id,
            organization_id=model.organization_id or "",
            title=model.title,
            description=description,
            status=CaseStatus(model.status),
            action_history=action_history,
            closure_reason=closure_reason,
            progress=progress,
            current_turn=current_turn,
            turns_without_progress=turns_without_progress,
            turn_history=turn_history,
            messages=messages,
            message_count=message_count,
            path_selection=path_selection,
            investigation_strategy=investigation_strategy,
            inquiry=inquiry,
            problem_verification=problem_verification,
            uploaded_files=uploaded_files,
            evidence=evidence,
            hypotheses=hypotheses,
            solutions=solutions,
            investigation_journal=investigation_journal,
            working_conclusion=working_conclusion,
            root_cause_conclusion=root_cause_conclusion,
            escalation_state=escalation_state,
            documentation=documentation,
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
            last_activity_at=last_activity_at,
            resolved_at=resolved_at,
            closed_at=closed_at,
        )

    # ========================================================================
    # JSON Parsing Helpers
    # ========================================================================

    def _parse_json(self, value: Optional[str], default: Any = None) -> Any:
        """Safely parse JSON string."""
        if value is None:
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        """Parse datetime from string or datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._ensure_tz_aware(value)
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return self._ensure_tz_aware(dt)
        except (ValueError, AttributeError):
            return None

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _parse_inquiry(self, value: Optional[str]) -> InquiryData:
        """Parse InquiryData from JSON."""
        data = self._parse_json(value, {})
        try:
            return InquiryData(**data)
        except Exception:
            return InquiryData()

    def _parse_progress(self, value: Optional[str]) -> InvestigationProgress:
        """Parse InvestigationProgress from JSON."""
        data = self._parse_json(value, {})
        try:
            return InvestigationProgress(**data)
        except Exception:
            return InvestigationProgress()

    def _parse_documentation(self, value: Optional[str]) -> DocumentationData:
        """Parse DocumentationData from JSON."""
        data = self._parse_json(value, {})
        try:
            return DocumentationData(**data)
        except Exception:
            return DocumentationData()

    def _parse_problem_verification(
        self, value: Optional[str]
    ) -> Optional[ProblemVerification]:
        """Parse ProblemVerification from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return ProblemVerification(**data)
        except Exception:
            return None

    def _parse_working_conclusion(
        self, value: Optional[str]
    ) -> Optional[WorkingConclusion]:
        """Parse WorkingConclusion from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return WorkingConclusion(**data)
        except Exception:
            return None

    def _parse_root_cause_conclusion(
        self, value: Optional[str]
    ) -> Optional[RootCauseConclusion]:
        """Parse RootCauseConclusion from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return RootCauseConclusion(**data)
        except Exception:
            return None

    def _parse_path_selection(self, value: Optional[str]) -> Optional[PathSelection]:
        """Parse PathSelection from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return PathSelection(**data)
        except Exception:
            return None

    def _parse_escalation_state(
        self, value: Optional[str]
    ) -> Optional[EscalationState]:
        """Parse EscalationState from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return EscalationState(**data)
        except Exception:
            return None

    def _parse_turn_history(self, value: List[dict]) -> List[TurnProgress]:
        """Parse TurnProgress list from JSON."""
        result = []
        for item in value:
            try:
                result.append(TurnProgress(**item))
            except Exception:
                pass
        return result

    def _parse_uploaded_files(self, value: List[dict]) -> List[UploadedFile]:
        """Parse UploadedFile list from JSON."""
        result = []
        for item in value:
            try:
                result.append(UploadedFile(**item))
            except Exception:
                pass
        return result

    def _parse_evidence(self, value: List[dict]) -> List[Evidence]:
        """Parse Evidence list from JSON."""
        result = []
        for item in value:
            try:
                result.append(Evidence(**item))
            except Exception:
                pass
        return result

    def _parse_hypotheses(self, value: Dict[str, dict]) -> Dict[str, Hypothesis]:
        """Parse Hypothesis dict from JSON."""
        result = {}
        for key, item in value.items():
            try:
                result[key] = Hypothesis(**item)
            except Exception:
                pass
        return result

    def _parse_solutions(self, value: List[dict]) -> List[Solution]:
        """Parse Solution list from JSON."""
        result = []
        for item in value:
            try:
                result.append(Solution(**item))
            except Exception:
                pass
        return result

    def _parse_investigation_journal(self, value: List[dict]) -> List[JournalEntry]:
        """Parse JournalEntry list from JSON."""
        result = []
        for item in value:
            try:
                result.append(JournalEntry(**item))
            except Exception:
                pass
        return result

    async def create_agent_execution(self, execution: Any) -> Any:
        """Create new agent execution record in database.

        Args:
            execution: AgentExecution domain object to save

        Returns:
            Saved AgentExecution with empty tool_calls list

        Raises:
            RepositoryException: If save fails
        """
        try:
            from faultmaven.infrastructure.persistence.models import AgentExecutionModel
            from faultmaven.modules.case.domain.owned_models.agent_execution import (
                AgentExecution,
            )

            # Convert domain model to ORM model
            token_usage_json = (
                json.dumps(execution.token_usage) if execution.token_usage else None
            )
            metadata_json = (
                json.dumps(execution.metadata) if execution.metadata else None
            )

            execution_model = AgentExecutionModel(
                execution_id=execution.execution_id,
                case_id=execution.case_id,
                organization_id=getattr(
                    execution, "organization_id", "00000000-0000-0000-0000-000000000001"
                ),
                agent_type=(
                    execution.agent_type.value
                    if hasattr(execution.agent_type, "value")
                    else str(execution.agent_type)
                ),
                agent_model=execution.agent_model,
                status=(
                    execution.status.value
                    if hasattr(execution.status, "value")
                    else str(execution.status)
                ),
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                execution_duration_ms=execution.execution_duration_ms,
                prompt=execution.prompt,
                response=execution.response,
                error_message=execution.error_message,
                token_usage=token_usage_json,
                created_at=execution.created_at,
                updated_at=execution.updated_at,
            )

            # Save to database
            self.db.add(execution_model)
            await self.db.flush()
            await self.db.commit()

            # Return domain object with empty tool_calls list
            result = AgentExecution(
                execution_id=execution.execution_id,
                case_id=execution.case_id,
                agent_type=execution.agent_type,
                agent_model=execution.agent_model,
                status=execution.status,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                execution_duration_ms=execution.execution_duration_ms,
                prompt=execution.prompt,
                response=execution.response,
                error_message=execution.error_message,
                token_usage=execution.token_usage,
                tool_calls=[],  # Initially empty, tools added separately
                metadata=execution.metadata,
                created_at=execution.created_at,
                updated_at=execution.updated_at,
            )

            logger.debug(f"Created agent execution {execution.execution_id}")
            return result

        except Exception as e:
            await self.db.rollback()
            logger.error(
                f"Failed to create agent execution {execution.execution_id}: {e}"
            )
            raise RepositoryException(
                f"Failed to create agent execution {execution.execution_id}: {e}"
            ) from e

    # ========================================================================
    # Clear (Testing Utility)
    # ========================================================================

    def clear(self) -> None:
        """Clear all cases (testing utility) - not async for compatibility."""
        pass  # Not implemented for database; use drop_database for testing
