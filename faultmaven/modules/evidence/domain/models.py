"""Evidence Module Domain Models.

This module contains all evidence-related domain models including:
- EvidenceArtifact: The core domain model for evidence files
- API DTOs: Request/response models for Evidence API endpoints

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, List
from uuid import UUID
from pydantic import BaseModel, Field as PydanticField


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
# Domain Model
# =============================================================================


@dataclass
class EvidenceArtifact:
    """Evidence artifact associated with a case.

    Represents a file or data artifact collected as evidence during
    investigation. This handles the storage metadata for evidence files.

    Attributes:
        evidence_id: Unique identifier (UUID format recommended)
        case_id: Case this evidence belongs to
        user_id: User who uploaded the evidence
        organization_id: Organization that owns the evidence
        original_filename: Original filename when uploaded
        stored_filename: Filename as stored (may be renamed for uniqueness)
        file_path: Path to file (relative to storage root)
        evidence_type: Type of evidence (screenshot, log, etc.)
        mime_type: MIME type (e.g., image/png, text/plain)
        file_size: Size in bytes
        storage_backend: Where file is stored (local, s3, etc.)
        created_at: Upload timestamp
        updated_at: Last modification timestamp
        metadata: Additional evidence-specific metadata (JSON)
        description: Optional description of evidence
        is_primary: Whether this is primary/featured evidence for case
    """

    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    stored_filename: str
    file_path: str
    evidence_type: EvidenceArtifactType
    mime_type: str
    file_size: int
    storage_backend: StorageBackend = StorageBackend.LOCAL_FILESYSTEM
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    is_primary: bool = False
    tags: List[str] = field(default_factory=list)
    linked_case_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate evidence artifact data."""
        if not self.evidence_id:
            raise ValueError("evidence_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
        if not self.organization_id:
            raise ValueError("organization_id is required")
        if not self.original_filename:
            raise ValueError("original_filename is required")
        if not self.stored_filename:
            raise ValueError("stored_filename is required")
        if not self.file_path:
            raise ValueError("file_path is required")
        if self.file_size < 0:
            raise ValueError("file_size cannot be negative")

    def get_display_name(self) -> str:
        """Get user-friendly display name.

        Returns:
            Description if set, otherwise original filename.
        """
        return self.description or self.original_filename

    def is_image(self) -> bool:
        """Check if evidence is an image.

        Returns:
            True if MIME type indicates an image.
        """
        return self.mime_type.startswith("image/")

    def is_text(self) -> bool:
        """Check if evidence is text-based.

        Returns:
            True if MIME type indicates text content.
        """
        return (
            self.mime_type.startswith("text/")
            or self.mime_type == "application/json"
            or self.mime_type == "application/xml"
        )

    def is_video(self) -> bool:
        """Check if evidence is a video.

        Returns:
            True if MIME type indicates video content.
        """
        return self.mime_type.startswith("video/")

    def get_extension(self) -> str:
        """Get file extension from original filename.

        Returns:
            File extension including the dot, or empty string if none.
        """
        if "." in self.original_filename:
            return "." + self.original_filename.rsplit(".", 1)[-1].lower()
        return ""

    def get_size_display(self) -> str:
        """Get human-readable file size.

        Returns:
            Formatted file size (e.g., '1.5 MB').
        """
        size = self.file_size
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return (
            f"EvidenceArtifact(evidence_id={self.evidence_id!r}, "
            f"case_id={self.case_id!r}, "
            f"original_filename={self.original_filename!r}, "
            f"evidence_type={self.evidence_type.value!r})"
        )


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

    case_id: Optional[UUID] = None
    uploaded_by: Optional[UUID] = None
    tags: Optional[List[str]] = None
    filename_contains: Optional[str] = None
    limit: int = PydanticField(default=50, le=200)
    offset: int = PydanticField(default=0, ge=0)


class EvidenceArtifactResponse(BaseModel):
    """API response model for EvidenceArtifact."""

    # New field names (canonical)
    evidence_id: str
    case_id: str
    user_id: str
    organization_id: str
    original_filename: str
    stored_filename: str
    file_path: str
    evidence_type: EvidenceArtifactType
    mime_type: str
    file_size: int
    storage_backend: StorageBackend = StorageBackend.LOCAL_FILESYSTEM
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = PydanticField(default_factory=dict)
    description: Optional[str] = None
    is_primary: bool = False
    tags: List[str] = PydanticField(default_factory=list)
    linked_case_ids: List[str] = PydanticField(default_factory=list)

    model_config = {
        "from_attributes": True,
        # Include computed properties in serialization
        "json_encoders": {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        },
    }

    @classmethod
    def from_domain(cls, evidence: EvidenceArtifact) -> "EvidenceArtifactResponse":
        """Convert domain model to response model."""
        return cls(
            evidence_id=evidence.evidence_id,
            case_id=evidence.case_id,
            user_id=evidence.user_id,
            organization_id=evidence.organization_id,
            original_filename=evidence.original_filename,
            stored_filename=evidence.stored_filename,
            file_path=evidence.file_path,
            evidence_type=evidence.evidence_type,
            mime_type=evidence.mime_type,
            file_size=evidence.file_size,
            storage_backend=evidence.storage_backend,
            created_at=evidence.created_at,
            updated_at=evidence.updated_at,
            metadata=evidence.metadata or {},
            description=evidence.description,
            is_primary=evidence.is_primary,
            tags=evidence.tags,
            linked_case_ids=evidence.linked_case_ids,
        )
