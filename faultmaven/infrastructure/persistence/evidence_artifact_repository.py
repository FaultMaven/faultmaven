"""Evidence Artifact Repository - Database and In-Memory Implementations.

This module provides evidence artifact repository implementations for managing
file artifacts (screenshots, logs, traces, etc.) associated with cases.

Features:
- Full CRUD operations for evidence artifacts
- Query by case with optional type filtering
- Primary evidence management (one per case)
- Support for both in-memory and database backends

Usage:
    from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
        DatabaseEvidenceArtifactRepository,
        InMemoryEvidenceArtifactRepository,
    )
    from faultmaven.infrastructure.persistence.database import get_db_session

    # Database usage
    async with get_db_session() as session:
        repo = DatabaseEvidenceArtifactRepository(session)
        artifact = await repo.create_evidence(evidence)

    # In-memory usage (for testing)
    repo = InMemoryEvidenceArtifactRepository()
    artifact = await repo.create_evidence(evidence)
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import EvidenceArtifactModel
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)

logger = logging.getLogger(__name__)


class EvidenceArtifactRepositoryException(Exception):
    """Exception raised when evidence artifact repository operations fail."""

    pass


class EvidenceArtifactRepository(ABC):
    """Abstract base class for evidence artifact repositories."""

    @abstractmethod
    async def create_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Create new evidence artifact record.

        Args:
            evidence: Evidence artifact to create

        Returns:
            Created evidence artifact with timestamps

        Raises:
            ValueError: If evidence_id already exists or case_id not found
            EvidenceArtifactRepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_evidence(self, evidence_id: str) -> Optional[EvidenceArtifact]:
        """Get evidence artifact by ID.

        Args:
            evidence_id: Evidence identifier

        Returns:
            Evidence artifact if found, None otherwise
        """
        pass

    @abstractmethod
    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[EvidenceArtifact], int]:
        """List evidence artifacts for a case.

        Args:
            case_id: Case identifier
            evidence_type: Optional filter by evidence type
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            Tuple of (evidence list, total count)
        """
        pass

    @abstractmethod
    async def update_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Update existing evidence artifact.

        Args:
            evidence: Evidence artifact with updated fields

        Returns:
            Updated evidence artifact

        Raises:
            ValueError: If evidence not found
            EvidenceArtifactRepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence artifact by ID.

        Args:
            evidence_id: Evidence identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def get_primary_evidence(self, case_id: str) -> Optional[EvidenceArtifact]:
        """Get primary/featured evidence artifact for case.

        Args:
            case_id: Case identifier

        Returns:
            Primary evidence artifact if set, None otherwise
        """
        pass

    @abstractmethod
    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str,
    ) -> bool:
        """Set primary/featured evidence artifact for case.

        Only one evidence artifact can be primary per case.
        Setting a new primary unsets the previous one.

        Args:
            case_id: Case identifier
            evidence_id: Evidence artifact to mark as primary

        Returns:
            True if set, False if evidence not found or doesn't belong to case
        """
        pass

    @abstractmethod
    async def count_by_case(self, case_id: str) -> int:
        """Count evidence artifacts for a case.

        Args:
            case_id: Case identifier

        Returns:
            Number of evidence artifacts for the case
        """
        pass


class DatabaseEvidenceArtifactRepository(EvidenceArtifactRepository):
    """Database-backed evidence artifact repository using SQLAlchemy ORM."""

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session for database operations
        """
        self.db = db_session

    async def create_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Create new evidence artifact record in the database."""
        try:
            # Convert domain model to ORM model
            evidence_model = EvidenceArtifactModel(
                evidence_id=evidence.evidence_id,
                case_id=evidence.case_id,
                user_id=evidence.user_id,
                organization_id=evidence.organization_id,
                original_filename=evidence.original_filename,
                stored_filename=evidence.stored_filename,
                file_path=evidence.file_path,
                evidence_type=evidence.evidence_type.value,
                mime_type=evidence.mime_type,
                file_size=evidence.file_size,
                storage_backend=evidence.storage_backend.value,
                created_at=evidence.created_at,
                updated_at=evidence.updated_at,
                artifact_metadata=(
                    json.dumps(evidence.metadata) if evidence.metadata else "{}"
                ),
                description=evidence.description,
                is_primary=evidence.is_primary,
            )

            self.db.add(evidence_model)
            await self.db.commit()
            await self.db.refresh(evidence_model)

            logger.debug(f"Created evidence artifact {evidence.evidence_id}")
            return self._to_domain(evidence_model)

        except IntegrityError as e:
            await self.db.rollback()
            error_str = str(e).lower()
            if "foreign key" in error_str or "fk_" in error_str:
                logger.error(f"Case {evidence.case_id} not found")
                raise ValueError(f"Case {evidence.case_id} not found") from e
            logger.error(f"Evidence artifact {evidence.evidence_id} already exists")
            raise ValueError(
                f"Evidence artifact {evidence.evidence_id} already exists"
            ) from e

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to create evidence artifact: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to create evidence artifact: {e}"
            ) from e

    async def get_evidence(self, evidence_id: str) -> Optional[EvidenceArtifact]:
        """Get evidence artifact by ID."""
        try:
            stmt = select(EvidenceArtifactModel).where(
                EvidenceArtifactModel.evidence_id == evidence_id
            )
            result = await self.db.execute(stmt)
            evidence_model = result.scalar_one_or_none()

            if evidence_model is None:
                return None

            return self._to_domain(evidence_model)

        except Exception as e:
            logger.error(f"Failed to get evidence artifact {evidence_id}: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to get evidence artifact {evidence_id}: {e}"
            ) from e

    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[EvidenceArtifact], int]:
        """List evidence artifacts for a case."""
        try:
            # Build query conditions
            conditions = [EvidenceArtifactModel.case_id == case_id]
            if evidence_type:
                conditions.append(
                    EvidenceArtifactModel.evidence_type == evidence_type.value
                )

            where_clause = and_(*conditions)

            # Get total count
            count_stmt = (
                select(func.count())
                .select_from(EvidenceArtifactModel)
                .where(where_clause)
            )
            count_result = await self.db.execute(count_stmt)
            total = count_result.scalar() or 0

            # Get paginated results ordered by created_at descending
            stmt = (
                select(EvidenceArtifactModel)
                .where(where_clause)
                .order_by(EvidenceArtifactModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await self.db.execute(stmt)
            evidence_models = result.scalars().all()

            evidence_list = [self._to_domain(model) for model in evidence_models]

            return evidence_list, total

        except Exception as e:
            logger.error(f"Failed to list evidence artifacts for case {case_id}: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to list evidence artifacts for case {case_id}: {e}"
            ) from e

    async def update_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Update existing evidence artifact."""
        try:
            evidence.updated_at = datetime.now(timezone.utc)

            stmt = (
                update(EvidenceArtifactModel)
                .where(EvidenceArtifactModel.evidence_id == evidence.evidence_id)
                .values(
                    description=evidence.description,
                    is_primary=evidence.is_primary,
                    artifact_metadata=(
                        json.dumps(evidence.metadata) if evidence.metadata else "{}"
                    ),
                    updated_at=evidence.updated_at,
                )
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(stmt)

            if result.rowcount == 0:
                raise ValueError(f"Evidence artifact {evidence.evidence_id} not found")

            await self.db.commit()
            logger.debug(f"Updated evidence artifact {evidence.evidence_id}")
            return evidence

        except ValueError:
            raise

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to update evidence artifact: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to update evidence artifact: {e}"
            ) from e

    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence artifact by ID."""
        try:
            stmt = delete(EvidenceArtifactModel).where(
                EvidenceArtifactModel.evidence_id == evidence_id
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.debug(f"Deleted evidence artifact {evidence_id}")
            return deleted

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to delete evidence artifact {evidence_id}: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to delete evidence artifact {evidence_id}: {e}"
            ) from e

    async def get_primary_evidence(self, case_id: str) -> Optional[EvidenceArtifact]:
        """Get primary/featured evidence artifact for case."""
        try:
            stmt = select(EvidenceArtifactModel).where(
                and_(
                    EvidenceArtifactModel.case_id == case_id,
                    EvidenceArtifactModel.is_primary == True,
                )
            )

            result = await self.db.execute(stmt)
            evidence_model = result.scalar_one_or_none()

            if evidence_model is None:
                return None

            return self._to_domain(evidence_model)

        except Exception as e:
            logger.error(f"Failed to get primary evidence for case {case_id}: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to get primary evidence for case {case_id}: {e}"
            ) from e

    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str,
    ) -> bool:
        """Set primary/featured evidence artifact for case."""
        try:
            # First, verify evidence exists and belongs to case
            check_stmt = select(EvidenceArtifactModel.evidence_id).where(
                and_(
                    EvidenceArtifactModel.evidence_id == evidence_id,
                    EvidenceArtifactModel.case_id == case_id,
                )
            )
            check_result = await self.db.execute(check_stmt)
            if check_result.scalar_one_or_none() is None:
                return False

            # Unset all primary flags for this case
            unset_stmt = (
                update(EvidenceArtifactModel)
                .where(EvidenceArtifactModel.case_id == case_id)
                .values(is_primary=False)
                .execution_options(synchronize_session=False)
            )
            await self.db.execute(unset_stmt)

            # Set the new primary evidence
            set_stmt = (
                update(EvidenceArtifactModel)
                .where(
                    and_(
                        EvidenceArtifactModel.evidence_id == evidence_id,
                        EvidenceArtifactModel.case_id == case_id,
                    )
                )
                .values(is_primary=True, updated_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )

            result = await self.db.execute(set_stmt)
            await self.db.commit()

            success = result.rowcount > 0
            if success:
                logger.debug(f"Set primary evidence {evidence_id} for case {case_id}")
            return success

        except Exception as e:
            await self.db.rollback()
            logger.error(f"Failed to set primary evidence: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to set primary evidence: {e}"
            ) from e

    async def count_by_case(self, case_id: str) -> int:
        """Count evidence artifacts for a case."""
        try:
            stmt = (
                select(func.count())
                .select_from(EvidenceArtifactModel)
                .where(EvidenceArtifactModel.case_id == case_id)
            )
            result = await self.db.execute(stmt)
            return result.scalar() or 0

        except Exception as e:
            logger.error(f"Failed to count evidence artifacts for case {case_id}: {e}")
            raise EvidenceArtifactRepositoryException(
                f"Failed to count evidence artifacts for case {case_id}: {e}"
            ) from e

    def _to_domain(self, model: EvidenceArtifactModel) -> EvidenceArtifact:
        """Convert SQLAlchemy model to domain model."""
        metadata = None
        if model.artifact_metadata:
            try:
                metadata = json.loads(model.artifact_metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        return EvidenceArtifact(
            evidence_id=model.evidence_id,
            case_id=model.case_id,
            user_id=model.user_id,
            organization_id=model.organization_id,
            original_filename=model.original_filename,
            stored_filename=model.stored_filename,
            file_path=model.file_path,
            evidence_type=EvidenceArtifactType(model.evidence_type),
            mime_type=model.mime_type,
            file_size=model.file_size,
            storage_backend=StorageBackend(model.storage_backend),
            created_at=self._ensure_tz_aware(model.created_at),
            updated_at=self._ensure_tz_aware(model.updated_at),
            metadata=metadata,
            description=model.description,
            is_primary=model.is_primary,
        )

    def _ensure_tz_aware(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime is timezone-aware (UTC if naive)."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


class InMemoryEvidenceArtifactRepository(EvidenceArtifactRepository):
    """In-memory evidence artifact repository for testing."""

    def __init__(self):
        """Initialize empty evidence artifact storage."""
        self._evidence: Dict[str, EvidenceArtifact] = {}

    async def create_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Create new evidence artifact record in memory."""
        if evidence.evidence_id in self._evidence:
            raise ValueError(f"Evidence artifact {evidence.evidence_id} already exists")

        self._evidence[evidence.evidence_id] = deepcopy(evidence)
        return deepcopy(evidence)

    async def get_evidence(self, evidence_id: str) -> Optional[EvidenceArtifact]:
        """Get evidence artifact by ID."""
        evidence = self._evidence.get(evidence_id)
        return deepcopy(evidence) if evidence else None

    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[EvidenceArtifact], int]:
        """List evidence artifacts for a case."""
        # Filter by case
        evidence_list = [e for e in self._evidence.values() if e.case_id == case_id]

        # Filter by type if specified
        if evidence_type:
            evidence_list = [
                e for e in evidence_list if e.evidence_type == evidence_type
            ]

        # Sort by created_at descending
        evidence_list.sort(key=lambda e: e.created_at, reverse=True)

        total = len(evidence_list)

        # Apply pagination
        paginated = evidence_list[offset : offset + limit]

        return [deepcopy(e) for e in paginated], total

    async def update_evidence(self, evidence: EvidenceArtifact) -> EvidenceArtifact:
        """Update existing evidence artifact."""
        if evidence.evidence_id not in self._evidence:
            raise ValueError(f"Evidence artifact {evidence.evidence_id} not found")

        evidence.updated_at = datetime.now(timezone.utc)
        self._evidence[evidence.evidence_id] = deepcopy(evidence)
        return deepcopy(evidence)

    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence artifact by ID."""
        if evidence_id in self._evidence:
            del self._evidence[evidence_id]
            return True
        return False

    async def get_primary_evidence(self, case_id: str) -> Optional[EvidenceArtifact]:
        """Get primary/featured evidence artifact for case."""
        for evidence in self._evidence.values():
            if evidence.case_id == case_id and evidence.is_primary:
                return deepcopy(evidence)
        return None

    async def set_primary_evidence(
        self,
        case_id: str,
        evidence_id: str,
    ) -> bool:
        """Set primary/featured evidence artifact for case."""
        # Verify evidence exists and belongs to case
        evidence = self._evidence.get(evidence_id)
        if not evidence or evidence.case_id != case_id:
            return False

        # Unset all primary flags for this case
        for e in self._evidence.values():
            if e.case_id == case_id:
                e.is_primary = False

        # Set new primary
        self._evidence[evidence_id].is_primary = True
        self._evidence[evidence_id].updated_at = datetime.now(timezone.utc)

        return True

    async def count_by_case(self, case_id: str) -> int:
        """Count evidence artifacts for a case."""
        return sum(1 for e in self._evidence.values() if e.case_id == case_id)

    def clear(self) -> None:
        """Clear all evidence artifacts (for testing)."""
        self._evidence.clear()
