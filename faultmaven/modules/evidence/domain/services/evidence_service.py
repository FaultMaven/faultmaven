"""Evidence Service - Business Logic

Handles evidence upload, linking, retrieval, and deletion.
"""

import logging
from typing import List, Optional
from uuid import UUID
from fastapi import UploadFile

from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceListFilter,
    EvidenceLinkRequest,
)

logger = logging.getLogger(__name__)


class EvidenceService:
    """Service for managing evidence files."""

    def __init__(self, storage_provider, case_repository):
        """Initialize evidence service.

        Args:
            storage_provider: File storage provider (local/S3/Azure)
            case_repository: Case repository (migrated from EvidenceRepository)
        """
        self.storage = storage_provider
        self.case_repository = case_repository

    async def upload_evidence(
        self,
        file: UploadFile,
        uploaded_by: UUID,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        case_id: Optional[UUID] = None,
    ) -> EvidenceArtifact:
        """Upload evidence file.

        Args:
            file: Uploaded file
            uploaded_by: User ID who uploaded
            description: Optional description
            tags: Optional tags
            case_id: Optional case to auto-link

        Returns:
            EvidenceArtifact: Created evidence record
        """
        # Store file
        storage_path = await self.storage.store_file(
            file, namespace="evidence"
        )

        # Create evidence record via Case repository
        evidence = await self.case_repository.create_standalone_evidence(
            filename=file.filename or "unknown",
            content_type=file.content_type or "application/octet-stream",
            size_bytes=file.size or 0,
            storage_path=storage_path,
            uploaded_by=str(uploaded_by),
            description=description,
            tags=tags or [],
        )

        # Auto-link to case if provided
        if case_id:
            await self.link_to_case(UUID(evidence.evidence_id), case_id)

        logger.info(f"Evidence {evidence.evidence_id} uploaded: {file.filename}")
        return evidence

    async def get_evidence(self, evidence_id: UUID) -> Optional[EvidenceArtifact]:
        """Get evidence by ID.

        Args:
            evidence_id: Evidence UUID

        Returns:
            EvidenceArtifact or None if not found
        """
        return await self.case_repository.get_standalone_evidence(str(evidence_id))

    async def list_evidence(
        self, filters: EvidenceListFilter
    ) -> tuple[List[EvidenceArtifact], int]:
        """List evidence with filters.

        Args:
            filters: Filtering criteria

        Returns:
            Tuple of (evidence list, total count)
        """
        return await self.case_repository.list_standalone_evidence(filters)

    async def delete_evidence(self, evidence_id: UUID) -> bool:
        """Delete evidence file and record.

        Args:
            evidence_id: Evidence UUID

        Returns:
            True if deleted, False if not found
        """
        evidence = await self.case_repository.get_standalone_evidence(str(evidence_id))
        if not evidence:
            return False

        # Delete file from storage
        await self.storage.delete_file(evidence.file_path)

        # Delete database record via Case repository
        deleted = await self.case_repository.delete_standalone_evidence(str(evidence_id))
        if deleted:
            logger.info(f"Evidence {evidence_id} deleted")
        return deleted

    async def link_to_case(
        self, evidence_id: UUID, case_id: UUID
    ) -> EvidenceArtifact:
        """Link evidence to a case.

        Args:
            evidence_id: Evidence UUID
            case_id: Case UUID

        Returns:
            Updated evidence record

        Raises:
            ValueError: If evidence not found
        """
        evidence = await self.case_repository.link_standalone_evidence_to_case(
            str(evidence_id), str(case_id)
        )
        if not evidence:
            raise ValueError(f"Evidence {evidence_id} not found")

        logger.info(f"Evidence {evidence_id} linked to case {case_id}")
        return evidence

    async def get_file_url(self, evidence_id: UUID) -> Optional[str]:
        """Get download URL for evidence file.

        Args:
            evidence_id: Evidence UUID

        Returns:
            Download URL or None if not found
        """
        evidence = await self.case_repository.get_standalone_evidence(str(evidence_id))
        if not evidence:
            return None

        return await self.storage.get_download_url(evidence.file_path)
