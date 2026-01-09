"""
Evidence Module Public Contracts

This file defines the public interface of the evidence module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.

Contracts include:
- DTOs: Data transfer objects for cross-module communication
- Protocols: Interface definitions for dependency injection
- Exceptions: Re-exported domain exceptions
- Enums: Public enumerations needed by other modules
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ============================================
# Public Enumerations
# ============================================


class EvidenceArtifactTypeDTO(str, Enum):
    """Public representation of evidence artifact types."""

    SCREENSHOT = "screenshot"
    LOG_FILE = "log_file"
    NETWORK_TRACE = "network_trace"
    CODE_SNIPPET = "code_snippet"
    CONFIGURATION = "configuration"
    VIDEO_RECORDING = "video_recording"
    HAR_FILE = "har_file"
    CRASH_DUMP = "crash_dump"
    HEAP_DUMP = "heap_dump"
    THREAD_DUMP = "thread_dump"
    METRICS_EXPORT = "metrics_export"
    OTHER = "other"


class StorageBackendDTO(str, Enum):
    """Public representation of storage backend types."""

    LOCAL_FILESYSTEM = "local_filesystem"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    GCS = "gcs"


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class EvidenceDTO:
    """Public representation of an evidence artifact.

    This is the cross-module representation of evidence.
    Contains metadata about the stored file.
    """

    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    evidence_type: EvidenceArtifactTypeDTO
    mime_type: str
    file_size: int
    storage_backend: StorageBackendDTO
    created_at: datetime
    updated_at: datetime
    description: Optional[str] = None
    is_primary: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvidenceMetadataDTO:
    """Lightweight evidence metadata for list views.

    Contains minimal information for efficient listing.
    """

    evidence_id: str
    case_id: str
    original_filename: str
    evidence_type: EvidenceArtifactTypeDTO
    mime_type: str
    file_size: int
    created_at: datetime
    is_primary: bool = False


@dataclass(frozen=True)
class EvidenceUploadResultDTO:
    """Result of evidence upload operation.

    Returned after successfully uploading evidence.
    """

    evidence_id: str
    original_filename: str
    stored_filename: str
    file_size: int
    evidence_type: EvidenceArtifactTypeDTO
    upload_time_ms: int


@dataclass(frozen=True)
class EvidenceDownloadDTO:
    """Evidence download information.

    Contains the file path or URL for downloading evidence.
    """

    evidence_id: str
    original_filename: str
    mime_type: str
    file_size: int
    download_url: Optional[str] = None
    file_path: Optional[str] = None
    expires_at: Optional[datetime] = None


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class IEvidenceQuery(Protocol):
    """Read-only evidence operations for cross-module use.

    Provides read access to evidence data for other modules.
    """

    async def get_evidence(self, evidence_id: str) -> Optional[EvidenceDTO]:
        """Get evidence by ID.

        Args:
            evidence_id: The evidence's unique identifier

        Returns:
            EvidenceDTO if found, None otherwise
        """
        ...

    async def get_evidence_by_ids(self, evidence_ids: list[str]) -> list[EvidenceDTO]:
        """Bulk evidence lookup - prevents N+1 queries.

        Args:
            evidence_ids: List of evidence IDs to fetch

        Returns:
            List of EvidenceDTO objects (may be fewer than requested)
        """
        ...

    async def list_evidence_for_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactTypeDTO] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvidenceMetadataDTO]:
        """List evidence for a case with optional filtering.

        Args:
            case_id: The case's unique identifier
            evidence_type: Optional type filter
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of EvidenceMetadataDTO objects
        """
        ...

    async def get_primary_evidence(self, case_id: str) -> Optional[EvidenceDTO]:
        """Get primary evidence for a case.

        Args:
            case_id: The case's unique identifier

        Returns:
            EvidenceDTO if primary evidence exists, None otherwise
        """
        ...

    async def get_download_info(self, evidence_id: str) -> Optional[EvidenceDownloadDTO]:
        """Get download information for evidence.

        Args:
            evidence_id: The evidence's unique identifier

        Returns:
            EvidenceDownloadDTO with download details, None if not found
        """
        ...


@runtime_checkable
class IEvidenceCommand(Protocol):
    """Write operations for evidence management.

    Provides write access to evidence data. Use with caution from other modules.
    """

    async def upload_evidence(
        self,
        case_id: str,
        user_id: str,
        organization_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        evidence_type: EvidenceArtifactTypeDTO,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_primary: bool = False,
    ) -> EvidenceUploadResultDTO:
        """Upload evidence for a case.

        Args:
            case_id: The case's unique identifier
            user_id: The uploading user's identifier
            organization_id: The organization's unique identifier
            filename: Original filename
            content: File content as bytes
            mime_type: MIME type of the file
            evidence_type: Type of evidence
            description: Optional description
            tags: Optional tags
            is_primary: Whether this is primary evidence

        Returns:
            EvidenceUploadResultDTO with upload results
        """
        ...

    async def delete_evidence(self, evidence_id: str) -> bool:
        """Delete evidence.

        Args:
            evidence_id: The evidence's unique identifier

        Returns:
            True if deleted, False if not found
        """
        ...

    async def set_primary_evidence(self, case_id: str, evidence_id: str) -> EvidenceDTO:
        """Set evidence as primary for a case.

        Args:
            case_id: The case's unique identifier
            evidence_id: The evidence to set as primary

        Returns:
            Updated EvidenceDTO
        """
        ...

    async def update_evidence_metadata(
        self,
        evidence_id: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> EvidenceDTO:
        """Update evidence metadata.

        Args:
            evidence_id: The evidence's unique identifier
            description: Optional new description
            tags: Optional new tags

        Returns:
            Updated EvidenceDTO
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
from faultmaven.modules.evidence.exceptions import (
    EvidenceAccessError,
    EvidenceException,
    EvidenceNotFoundError,
    EvidenceProcessingError,
    EvidenceStorageError,
    EvidenceUploadError,
    EvidenceValidationError,
)

__all__ = [
    # Enums
    "EvidenceArtifactTypeDTO",
    "StorageBackendDTO",
    # DTOs
    "EvidenceDTO",
    "EvidenceMetadataDTO",
    "EvidenceUploadResultDTO",
    "EvidenceDownloadDTO",
    # Protocols
    "IEvidenceQuery",
    "IEvidenceCommand",
    # Exceptions
    "EvidenceException",
    "EvidenceNotFoundError",
    "EvidenceUploadError",
    "EvidenceValidationError",
    "EvidenceStorageError",
    "EvidenceAccessError",
    "EvidenceProcessingError",
]
