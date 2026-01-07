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
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import select, func, delete, update, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from faultmaven.modules.case.infrastructure.case_repository import (
    CaseRepository,
    RepositoryException,
)
from faultmaven.infrastructure.persistence.models import (
    CaseModel,
    EvidenceModel,
    HypothesisModel,
    SolutionModel,
    CaseMessageModel,
    UploadedFileModel,
    CaseStatusTransitionModel,
    CaseTagModel,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationProgress,
    TurnProgress,
    UploadedFile,
    Evidence,
    Hypothesis,
    Solution,
    ConsultingData,
    ProblemVerification,
    WorkingConclusion,
    RootCauseConclusion,
    DegradedMode,
    EscalationState,
    DocumentationData,
    PathSelection,
    CaseStatusTransition,
    InvestigationStrategy,
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
            await self._sync_status_transitions(case.case_id, case.status_history)

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
                    selectinload(CaseModel.status_transitions),
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
        offset: int = 0
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
                conditions.append(CaseModel.org_id == organization_id)
            if status:
                conditions.append(CaseModel.status == status.value)

            # Count query
            count_stmt = select(func.count()).select_from(CaseModel)
            if conditions:
                count_stmt = count_stmt.where(and_(*conditions))

            count_result = await self.db.execute(count_stmt)
            total_count = count_result.scalar()

            # Data query with eager loading
            data_stmt = (
                select(CaseModel)
                .options(
                    selectinload(CaseModel.evidence),
                    selectinload(CaseModel.hypotheses),
                    selectinload(CaseModel.solutions),
                    selectinload(CaseModel.messages),
                    selectinload(CaseModel.uploaded_files),
                    selectinload(CaseModel.status_transitions),
                )
            )
            if conditions:
                data_stmt = data_stmt.where(and_(*conditions))

            data_stmt = (
                data_stmt
                .order_by(CaseModel.updated_at.desc())
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
        limit: int = 20
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
                CaseModel.consulting.ilike(search_pattern),
            )

            conditions = [search_condition]
            if user_id:
                conditions.append(CaseModel.user_id == user_id)
            if organization_id:
                conditions.append(CaseModel.org_id == organization_id)

            # Count query
            count_stmt = (
                select(func.count())
                .select_from(CaseModel)
                .where(and_(*conditions))
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
                    selectinload(CaseModel.status_transitions),
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
            message_id = message_dict.get("message_id", f"msg_{uuid4().hex[:12]}")
            message_model = CaseMessageModel(
                message_id=message_id,
                case_id=case_id,
                role=message_dict.get("role", "user"),
                content=message_dict.get("content", ""),
                timestamp=message_dict.get("timestamp", datetime.now(timezone.utc)),
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
            raise RepositoryException(f"Failed to add message to case {case_id}: {e}") from e

    async def get_messages(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
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
                .order_by(CaseMessageModel.timestamp.asc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            message_models = result.scalars().all()

            messages = []
            for m in message_models:
                messages.append({
                    "message_id": m.message_id,
                    "case_id": m.case_id,
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                    "metadata": json.loads(m.message_metadata) if m.message_metadata else {},
                })

            return messages

        except Exception as e:
            logger.error(f"Failed to get messages for case {case_id}: {e}")
            raise RepositoryException(f"Failed to get messages for case {case_id}: {e}") from e

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
                "last_activity_at": case.last_activity_at.isoformat() if case.last_activity_at else None,
                "message_count": case.message_count,
                "current_turn": case.current_turn,
                "turns_without_progress": case.turns_without_progress,
                "evidence_count": len(case.evidence),
                "hypothesis_count": len(case.hypotheses),
                "solution_count": len(case.solutions),
                "investigation_strategy": case.investigation_strategy.value,
                "has_working_conclusion": case.working_conclusion is not None,
                "has_root_cause": case.root_cause_conclusion is not None,
                "is_degraded": case.degraded_mode is not None,
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

    async def cleanup_expired(self, max_age_days: int = 90, batch_size: int = 100) -> int:
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

            # Find expired cases
            stmt = (
                select(CaseModel.case_id)
                .where(
                    and_(
                        CaseModel.status == "closed",
                        CaseModel.updated_at < cutoff_date,
                    )
                )
                .limit(batch_size)
            )

            result = await self.db.execute(stmt)
            case_ids = [row[0] for row in result.fetchall()]

            if not case_ids:
                return 0

            # Delete expired cases
            delete_stmt = delete(CaseModel).where(CaseModel.case_id.in_(case_ids))
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
                    selectinload(CaseModel.status_transitions),
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
        self,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
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
                select(func.count())
                .select_from(CaseModel)
                .where(and_(*conditions))
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
                    selectinload(CaseModel.status_transitions),
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
        self,
        case_id: str,
        session_id: Optional[str]
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
                .values(
                    session_id=session_id,
                    updated_at=datetime.now(timezone.utc)
                )
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
        self,
        case: Case,
        session_id: Optional[str] = None
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
            await self._sync_status_transitions(case.case_id, case.status_history)

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

    async def _sync_messages(self, case_id: str, messages: List[Dict[str, Any]]) -> None:
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
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    timestamp=self._parse_datetime(msg.get("created_at")),
                    message_metadata=json.dumps(msg.get("metadata", {})),
                )
                self.db.add(message_model)

    async def _sync_status_transitions(
        self, case_id: str, transitions: List[CaseStatusTransition]
    ) -> None:
        """Sync status transitions for a case (append-only)."""
        if not transitions:
            return

        # Get existing transition count
        stmt = select(func.count()).select_from(CaseStatusTransitionModel).where(
            CaseStatusTransitionModel.case_id == case_id
        )
        result = await self.db.execute(stmt)
        existing_count = result.scalar()

        # Add new transitions
        for i, t in enumerate(transitions):
            if i >= existing_count:
                transition_model = CaseStatusTransitionModel(
                    case_id=case_id,
                    from_status=t.from_status.value,
                    to_status=t.to_status.value,
                    reason=t.reason,
                    transitioned_at=t.triggered_at,
                    transition_metadata=json.dumps({"triggered_by": t.triggered_by}),
                )
                self.db.add(transition_model)

    # ========================================================================
    # Model Conversion: Domain -> ORM
    # ========================================================================

    def _case_to_model(self, case: Case) -> CaseModel:
        """Convert Case domain model to CaseModel ORM model."""
        # Serialize complex fields to JSON
        consulting_json = json.dumps(case.consulting.model_dump()) if case.consulting else "{}"
        progress_json = json.dumps(case.progress.model_dump()) if case.progress else "{}"
        documentation_json = json.dumps(case.documentation.model_dump()) if case.documentation else "{}"

        # Optional JSONB fields
        problem_verification_json = (
            json.dumps(case.problem_verification.model_dump())
            if case.problem_verification else None
        )
        working_conclusion_json = (
            json.dumps(case.working_conclusion.model_dump())
            if case.working_conclusion else None
        )
        root_cause_conclusion_json = (
            json.dumps(case.root_cause_conclusion.model_dump())
            if case.root_cause_conclusion else None
        )
        path_selection_json = (
            json.dumps(case.path_selection.model_dump())
            if case.path_selection else None
        )
        degraded_mode_json = (
            json.dumps(case.degraded_mode.model_dump())
            if case.degraded_mode else None
        )
        escalation_state_json = (
            json.dumps(case.escalation_state.model_dump())
            if case.escalation_state else None
        )

        # Build metadata
        metadata = {
            "description": case.description,
            "investigation_strategy": case.investigation_strategy.value,
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "message_count": case.message_count,
            "closure_reason": case.closure_reason,
            "turn_history": [t.model_dump() for t in case.turn_history],
            "uploaded_files": [f.model_dump() for f in case.uploaded_files],
            "evidence": [e.model_dump() for e in case.evidence],
            "hypotheses": {k: v.model_dump() for k, v in case.hypotheses.items()},
            "solutions": [s.model_dump() for s in case.solutions],
            "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
            "closed_at": case.closed_at.isoformat() if case.closed_at else None,
            "last_activity_at": case.last_activity_at.isoformat() if case.last_activity_at else None,
        }

        return CaseModel(
            case_id=case.case_id,
            user_id=case.user_id,
            title=case.title,
            status=case.status.value,
            created_at=case.created_at,
            updated_at=case.updated_at,
            consulting=consulting_json,
            problem_verification=problem_verification_json,
            working_conclusion=working_conclusion_json,
            root_cause_conclusion=root_cause_conclusion_json,
            path_selection=path_selection_json,
            degraded_mode=degraded_mode_json,
            escalation_state=escalation_state_json,
            documentation=documentation_json,
            progress=progress_json,
            case_metadata=json.dumps(metadata),
            org_id=case.organization_id,
        )

    # ========================================================================
    # Model Conversion: ORM -> Domain
    # ========================================================================

    def _model_to_case(self, model: CaseModel) -> Case:
        """Convert CaseModel ORM model to Case domain model."""
        # Parse JSONB fields
        consulting = self._parse_consulting(model.consulting)
        progress = self._parse_progress(model.progress)
        documentation = self._parse_documentation(model.documentation)
        metadata = self._parse_json(model.case_metadata, {})

        # Optional JSONB fields
        problem_verification = self._parse_problem_verification(model.problem_verification)
        working_conclusion = self._parse_working_conclusion(model.working_conclusion)
        root_cause_conclusion = self._parse_root_cause_conclusion(model.root_cause_conclusion)
        path_selection = self._parse_path_selection(model.path_selection)
        degraded_mode = self._parse_degraded_mode(model.degraded_mode)
        escalation_state = self._parse_escalation_state(model.escalation_state)

        # Extract fields from metadata
        description = metadata.get("description", "")
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

        # Parse messages from relationship
        messages = []
        if model.messages:
            for msg in model.messages:
                messages.append({
                    "message_id": msg.message_id,
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.timestamp.isoformat() if msg.timestamp else None,
                    "metadata": self._parse_json(msg.message_metadata, {}),
                })

        # Parse status transitions from relationship
        status_history = []
        if model.status_transitions:
            for t in model.status_transitions:
                try:
                    transition = CaseStatusTransition(
                        from_status=CaseStatus(t.from_status) if t.from_status else CaseStatus.CONSULTING,
                        to_status=CaseStatus(t.to_status),
                        triggered_at=t.transitioned_at,
                        triggered_by=self._parse_json(t.transition_metadata, {}).get("triggered_by", "system"),
                        reason=t.reason or "",
                    )
                    status_history.append(transition)
                except Exception:
                    pass  # Skip invalid transitions

        # Parse timestamps from metadata
        resolved_at = self._parse_datetime(metadata.get("resolved_at"))
        closed_at = self._parse_datetime(metadata.get("closed_at"))
        last_activity_at = self._parse_datetime(metadata.get("last_activity_at")) or self._ensure_tz_aware(model.updated_at)

        return Case(
            case_id=model.case_id,
            user_id=model.user_id,
            organization_id=model.org_id or "",
            title=model.title,
            description=description,
            status=CaseStatus(model.status),
            status_history=status_history,
            closure_reason=closure_reason,
            progress=progress,
            current_turn=current_turn,
            turns_without_progress=turns_without_progress,
            turn_history=turn_history,
            messages=messages,
            message_count=message_count,
            path_selection=path_selection,
            investigation_strategy=investigation_strategy,
            consulting=consulting,
            problem_verification=problem_verification,
            uploaded_files=uploaded_files,
            evidence=evidence,
            hypotheses=hypotheses,
            solutions=solutions,
            working_conclusion=working_conclusion,
            root_cause_conclusion=root_cause_conclusion,
            degraded_mode=degraded_mode,
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

    def _parse_consulting(self, value: Optional[str]) -> ConsultingData:
        """Parse ConsultingData from JSON."""
        data = self._parse_json(value, {})
        try:
            return ConsultingData(**data)
        except Exception:
            return ConsultingData()

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

    def _parse_problem_verification(self, value: Optional[str]) -> Optional[ProblemVerification]:
        """Parse ProblemVerification from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return ProblemVerification(**data)
        except Exception:
            return None

    def _parse_working_conclusion(self, value: Optional[str]) -> Optional[WorkingConclusion]:
        """Parse WorkingConclusion from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return WorkingConclusion(**data)
        except Exception:
            return None

    def _parse_root_cause_conclusion(self, value: Optional[str]) -> Optional[RootCauseConclusion]:
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

    def _parse_degraded_mode(self, value: Optional[str]) -> Optional[DegradedMode]:
        """Parse DegradedMode from JSON."""
        data = self._parse_json(value)
        if data is None:
            return None
        try:
            return DegradedMode(**data)
        except Exception:
            return None

    def _parse_escalation_state(self, value: Optional[str]) -> Optional[EscalationState]:
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

    # ========================================================================
    # Clear (Testing Utility)
    # ========================================================================

    def clear(self) -> None:
        """Clear all cases (testing utility) - not async for compatibility."""
        pass  # Not implemented for database; use drop_database for testing
