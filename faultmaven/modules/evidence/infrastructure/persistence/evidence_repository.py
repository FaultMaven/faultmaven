"""Evidence Repository - Database Operations

Handles CRUD operations for evidence records in the database.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.models import Evidence as EvidenceModel  # SQLAlchemy model
from faultmaven.modules.evidence.domain.models import (
    Evidence,
    EvidenceListFilter,
)

logger = logging.getLogger(__name__)


class EvidenceRepository:
    """Repository for evidence database operations."""

    def __init__(self, session: AsyncSession):
        """Initialize repository.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def create(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: UUID,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Evidence:
        """Create evidence record.

        Args:
            filename: Original filename
            content_type: MIME type
            size_bytes: File size in bytes
            storage_path: Storage location
            uploaded_by: User ID
            description: Optional description
            tags: Optional tags

        Returns:
            Created evidence domain model
        """
        evidence_id = uuid4()
        evidence = EvidenceModel(
            id=evidence_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            uploaded_by=uploaded_by,
            uploaded_at=datetime.now(timezone.utc),
            description=description,
            tags=tags or [],
            linked_cases=[],
            metadata={},
        )

        self.session.add(evidence)
        await self.session.commit()
        await self.session.refresh(evidence)

        return self._to_domain(evidence)

    async def get(self, evidence_id: UUID) -> Optional[Evidence]:
        """Get evidence by ID.

        Args:
            evidence_id: Evidence UUID

        Returns:
            Evidence domain model or None
        """
        stmt = select(EvidenceModel).where(EvidenceModel.id == evidence_id)
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        return self._to_domain(evidence) if evidence else None

    async def list(
        self, filters: EvidenceListFilter
    ) -> Tuple[List[Evidence], int]:
        """List evidence with filters.

        Args:
            filters: Filter criteria

        Returns:
            Tuple of (evidence list, total count)
        """
        # Build query with filters
        conditions = []

        if filters.case_id:
            conditions.append(
                EvidenceModel.linked_cases.contains([str(filters.case_id)])
            )

        if filters.uploaded_by:
            conditions.append(EvidenceModel.uploaded_by == filters.uploaded_by)

        if filters.tags:
            # Match any of the provided tags
            for tag in filters.tags:
                conditions.append(EvidenceModel.tags.contains([tag]))

        if filters.filename_contains:
            conditions.append(
                EvidenceModel.filename.ilike(f"%{filters.filename_contains}%")
            )

        # Count query
        count_stmt = select(func.count()).select_from(EvidenceModel)
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total = await self.session.scalar(count_stmt)

        # List query
        stmt = select(EvidenceModel).offset(filters.offset).limit(filters.limit)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        result = await self.session.execute(stmt)
        evidence_records = result.scalars().all()

        return (
            [self._to_domain(e) for e in evidence_records],
            total or 0,
        )

    async def delete(self, evidence_id: UUID) -> bool:
        """Delete evidence record.

        Args:
            evidence_id: Evidence UUID

        Returns:
            True if deleted, False if not found
        """
        stmt = select(EvidenceModel).where(EvidenceModel.id == evidence_id)
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        if not evidence:
            return False

        await self.session.delete(evidence)
        await self.session.commit()
        return True

    async def link_to_case(
        self, evidence_id: UUID, case_id: UUID
    ) -> Optional[Evidence]:
        """Link evidence to a case.

        Args:
            evidence_id: Evidence UUID
            case_id: Case UUID

        Returns:
            Updated evidence domain model or None if not found
        """
        stmt = select(EvidenceModel).where(EvidenceModel.id == evidence_id)
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        if not evidence:
            return None

        # Add case_id to linked_cases if not already linked
        if str(case_id) not in evidence.linked_cases:
            evidence.linked_cases = evidence.linked_cases + [str(case_id)]
            await self.session.commit()
            await self.session.refresh(evidence)

        return self._to_domain(evidence)

    def _to_domain(self, evidence: EvidenceModel) -> Evidence:
        """Convert SQLAlchemy model to domain model.

        Args:
            evidence: SQLAlchemy model

        Returns:
            Evidence domain model
        """
        return Evidence(
            id=evidence.id,
            filename=evidence.filename,
            content_type=evidence.content_type,
            size_bytes=evidence.size_bytes,
            storage_path=evidence.storage_path,
            uploaded_by=evidence.uploaded_by,
            uploaded_at=evidence.uploaded_at,
            description=evidence.description,
            tags=evidence.tags or [],
            linked_cases=[UUID(cid) for cid in (evidence.linked_cases or [])],
            metadata=evidence.metadata or {},
        )
