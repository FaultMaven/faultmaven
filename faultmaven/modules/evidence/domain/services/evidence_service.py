"""Evidence Service - Business Logic

Handles evidence upload, linking, retrieval, and deletion.
"""

import logging
from uuid import UUID

from fastapi import UploadFile

from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceListFilter,
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
        description: str | None = None,
        tags: list[str] | None = None,
        case_id: UUID | None = None,
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
        storage_path = await self.storage.store_file(file, namespace="evidence")

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
            await self.link_to_case(str(evidence.evidence_id), str(case_id))

        logger.info(f"Evidence {evidence.evidence_id} uploaded: {file.filename}")
        return evidence

    async def get_evidence(self, evidence_id: str | UUID) -> EvidenceArtifact | None:
        """Get evidence by ID.

        Args:
            evidence_id: Evidence ID (string or UUID)

        Returns:
            EvidenceArtifact or None if not found
        """
        return await self.case_repository.get_standalone_evidence(str(evidence_id))

    async def list_evidence(
        self, filters: EvidenceListFilter
    ) -> tuple[list[EvidenceArtifact], int]:
        """List evidence with filters.

        Args:
            filters: Filtering criteria

        Returns:
            Tuple of (evidence list, total count)
        """
        return await self.case_repository.list_standalone_evidence(filters)

    async def delete_evidence(self, evidence_id: str | UUID) -> bool:
        """Delete evidence file and record.

        Args:
            evidence_id: Evidence ID (string or UUID)

        Returns:
            True if deleted, False if not found
        """
        evidence = await self.case_repository.get_standalone_evidence(str(evidence_id))
        if not evidence:
            return False

        # Delete file from storage
        await self.storage.delete_file(evidence.file_path)

        # Delete database record via Case repository
        deleted = await self.case_repository.delete_standalone_evidence(
            str(evidence_id)
        )
        if deleted:
            logger.info(f"Evidence {evidence_id} deleted")
        return deleted

    async def link_to_case(
        self, evidence_id: str | UUID, case_id: str | UUID
    ) -> EvidenceArtifact:
        """Link evidence to a case.

        Args:
            evidence_id: Evidence ID (string or UUID)
            case_id: Case ID (string or UUID)

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

    async def get_file_url(self, evidence_id: str | UUID) -> str | None:
        """Get download URL for evidence file.

        Args:
            evidence_id: Evidence ID (string or UUID)

        Returns:
            Download URL or None if not found
        """
        evidence = await self.case_repository.get_standalone_evidence(str(evidence_id))
        if not evidence:
            return None

        return await self.storage.get_download_url(evidence.file_path)

    async def download_evidence(
        self, evidence_id: str | UUID
    ) -> tuple[bytes, str, str] | None:
        """Download evidence file content.

        Args:
            evidence_id: Evidence ID (string or UUID)

        Returns:
            Tuple of (file_bytes, filename, content_type) or None if not found
        """
        evidence = await self.case_repository.get_standalone_evidence(str(evidence_id))
        if not evidence:
            return None

        file_data = await self.storage.get_file_content(evidence.file_path)
        if file_data is None:
            return None

        return file_data, evidence.original_filename, evidence.mime_type

    async def list_evidence_by_case(
        self,
        case_id: str,
        evidence_type=None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvidenceArtifact]:
        """List evidence linked to a case.

        Args:
            case_id: Case ID string
            evidence_type: Optional filter by evidence type
            limit: Max results
            offset: Pagination offset

        Returns:
            List of evidence artifacts for the case
        """
        filters = EvidenceListFilter(case_id=case_id, limit=limit, offset=offset)
        evidence_list, _ = await self.case_repository.list_standalone_evidence(filters)

        if evidence_type:
            evidence_list = [
                e
                for e in evidence_list
                if getattr(e, "evidence_type", None) == evidence_type
            ]

        return evidence_list
