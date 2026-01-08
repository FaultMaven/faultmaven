"""Evidence Repository - Database Operations

Handles CRUD operations for evidence records in the database.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import StandaloneEvidenceModel
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    StorageBackend,
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
    ) -> EvidenceArtifact:
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
        # Store tags in both the tags column and metadata for compatibility
        metadata = {"tags": tags or []}
        evidence = StandaloneEvidenceModel(
            id=str(evidence_id),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            uploaded_by=str(uploaded_by),
            uploaded_at=datetime.now(timezone.utc),
            description=description,
            tags=json.dumps(tags or []),
            linked_cases=json.dumps([]),
            evidence_metadata=json.dumps(metadata),
        )

        self.session.add(evidence)
        await self.session.commit()
        await self.session.refresh(evidence)

        return self._to_domain(evidence)

    async def get(self, evidence_id: UUID) -> Optional[EvidenceArtifact]:
        """Get evidence by ID.

        Args:
            evidence_id: Evidence UUID

        Returns:
            EvidenceArtifact domain model or None
        """
        stmt = select(StandaloneEvidenceModel).where(
            StandaloneEvidenceModel.id == str(evidence_id)
        )
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        return self._to_domain(evidence) if evidence else None

    async def list(
        self, filters: EvidenceListFilter
    ) -> Tuple[List[EvidenceArtifact], int]:
        """List evidence with filters.

        Args:
            filters: Filter criteria

        Returns:
            Tuple of (evidence list, total count)
        """
        # Build query with filters
        conditions = []

        if filters.case_id:
            # For JSON array in TEXT column, use LIKE for SQLite compatibility
            conditions.append(
                StandaloneEvidenceModel.linked_cases.like(f'%"{filters.case_id}"%')
            )

        if filters.uploaded_by:
            conditions.append(
                StandaloneEvidenceModel.uploaded_by == str(filters.uploaded_by)
            )

        if filters.tags:
            # Match any of the provided tags using LIKE for SQLite compatibility
            tag_conditions = []
            for tag in filters.tags:
                tag_conditions.append(
                    StandaloneEvidenceModel.tags.like(f'%"{tag}"%')
                )
            if tag_conditions:
                conditions.append(or_(*tag_conditions))

        if filters.filename_contains:
            conditions.append(
                StandaloneEvidenceModel.filename.ilike(f"%{filters.filename_contains}%")
            )

        # Count query
        count_stmt = select(func.count()).select_from(StandaloneEvidenceModel)
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total = await self.session.scalar(count_stmt)

        # List query
        stmt = select(StandaloneEvidenceModel).offset(filters.offset).limit(filters.limit)
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
        stmt = select(StandaloneEvidenceModel).where(
            StandaloneEvidenceModel.id == str(evidence_id)
        )
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        if not evidence:
            return False

        await self.session.delete(evidence)
        await self.session.commit()
        return True

    async def link_to_case(
        self, evidence_id: UUID, case_id: UUID
    ) -> Optional[EvidenceArtifact]:
        """Link evidence to a case.

        Args:
            evidence_id: Evidence UUID
            case_id: Case UUID

        Returns:
            Updated EvidenceArtifact domain model or None if not found
        """
        stmt = select(StandaloneEvidenceModel).where(
            StandaloneEvidenceModel.id == str(evidence_id)
        )
        result = await self.session.execute(stmt)
        evidence = result.scalar_one_or_none()

        if not evidence:
            return None

        # Parse JSON, add case_id, serialize back
        linked_cases = json.loads(evidence.linked_cases or "[]")
        metadata = json.loads(evidence.evidence_metadata or "{}")
        case_id_str = str(case_id)
        if case_id_str not in linked_cases:
            linked_cases.append(case_id_str)
            evidence.linked_cases = json.dumps(linked_cases)
            # Also update metadata to include linked_cases for backward compatibility
            metadata["linked_cases"] = linked_cases
            evidence.evidence_metadata = json.dumps(metadata)
            await self.session.commit()
            await self.session.refresh(evidence)

        return self._to_domain(evidence)

    def _to_domain(self, evidence: StandaloneEvidenceModel) -> EvidenceArtifact:
        """Convert SQLAlchemy model to domain model.

        Args:
            evidence: SQLAlchemy model

        Returns:
            EvidenceArtifact domain model
        """
        # Parse JSON fields
        linked_cases_str = json.loads(evidence.linked_cases or "[]")
        tags = json.loads(evidence.tags or "[]")
        metadata = json.loads(evidence.evidence_metadata or "{}")

        # Ensure tags and linked_cases are in metadata for backward compatibility
        if "tags" not in metadata:
            metadata["tags"] = tags
        if "linked_cases" not in metadata:
            metadata["linked_cases"] = linked_cases_str

        # Map old schema to new domain model
        # Use first linked case as primary case_id, or generate a standalone marker
        case_id = linked_cases_str[0] if linked_cases_str else "standalone"

        # Infer evidence type from content_type
        evidence_type = self._infer_evidence_type(evidence.content_type)

        # Extract organization_id from metadata or use default
        organization_id = metadata.get("organization_id", "default_org")

        return EvidenceArtifact(
            evidence_id=evidence.id,
            case_id=case_id,
            user_id=evidence.uploaded_by,
            organization_id=organization_id,
            original_filename=evidence.filename,
            stored_filename=evidence.filename,  # Assume same for now
            file_path=evidence.storage_path,
            evidence_type=evidence_type,
            mime_type=evidence.content_type,
            file_size=evidence.size_bytes,
            storage_backend=StorageBackend.LOCAL_FILESYSTEM,
            created_at=evidence.uploaded_at,
            updated_at=evidence.uploaded_at,
            description=evidence.description,
            metadata=metadata,
        )

    def _infer_evidence_type(self, content_type: str) -> EvidenceArtifactType:
        """Infer evidence type from MIME type.

        Args:
            content_type: MIME type

        Returns:
            EvidenceArtifactType enum value
        """
        if content_type.startswith("image/"):
            return EvidenceArtifactType.SCREENSHOT
        elif content_type.startswith("video/"):
            return EvidenceArtifactType.VIDEO_RECORDING
        elif "json" in content_type or content_type.startswith("text/"):
            return EvidenceArtifactType.LOG_FILE
        elif "application/x-har" in content_type:
            return EvidenceArtifactType.HAR_FILE
        else:
            return EvidenceArtifactType.OTHER
