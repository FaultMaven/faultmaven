"""Evidence Domain Models - Owned by Case Module.

Per module-organization-design.md:
- Case module owns the evidence table (FK to cases)
- Evidence module is a Domain Service that operates on Case-owned data
- These models are canonical and should be imported from Case contracts

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field as PydanticField

# =============================================================================
# Enumerations
# =============================================================================


class EvidenceArtifactType(str, Enum):
    """Types of evidence artifacts.

    Categorizes the kind of evidence artifact stored.
    """

    SCREENSHOT = "screenshot"
    LOG_FILE = "log_file"
    NETWORK_TRACE = "network_trace"
    CODE_SNIPPET = "code_snippet"
    CONFIGURATION = "configuration"
    VIDEO_RECORDING = "video_recording"
    HAR_FILE = "har_file"  # HTTP Archive
    CRASH_DUMP = "crash_dump"
    HEAP_DUMP = "heap_dump"
    THREAD_DUMP = "thread_dump"
    METRICS_EXPORT = "metrics_export"
    OTHER = "other"


class StorageBackend(str, Enum):
    """Storage backend types.

    Defines where evidence files are stored.
    """

    LOCAL_FILESYSTEM = "local_filesystem"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    GCS = "gcs"


# =============================================================================
# API DTOs (Pydantic models for request/response)
# =============================================================================


class EvidenceUploadRequest(BaseModel):
    """Request to upload evidence."""

    filename: str
    content_type: str
    description: Optional[str] = None
    tags: List[str] = PydanticField(default_factory=list)
    case_id: Optional[UUID] = None  # Auto-link to case if provided


class EvidenceLinkRequest(BaseModel):
    """Request to link evidence to a case."""

    case_id: UUID


class EvidenceListFilter(BaseModel):
    """Filters for listing evidence."""

    case_id: Optional[str] = None
    uploaded_by: Optional[UUID] = None
    tags: Optional[List[str]] = None
    filename_contains: Optional[str] = None
    limit: int = PydanticField(default=50, le=10000)
    offset: int = PydanticField(default=0, ge=0)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Enumerations
    "EvidenceArtifactType",
    "StorageBackend",
    # API DTOs
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
]
