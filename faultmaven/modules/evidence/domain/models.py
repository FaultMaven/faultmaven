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

    # Backward compatibility properties for old field names
    @property
    def id(self):
        """Backward compatibility: id -> evidence_id."""
        from uuid import UUID
        try:
            return UUID(self.evidence_id) if isinstance(self.evidence_id, str) else self.evidence_id
        except (ValueError, AttributeError):
            return self.evidence_id

    @property
    def filename(self):
        """Backward compatibility: filename -> original_filename."""
        return self.original_filename

    @property
    def content_type(self):
        """Backward compatibility: content_type -> mime_type."""
        return self.mime_type

    @property
    def size_bytes(self):
        """Backward compatibility: size_bytes -> file_size."""
        return self.file_size

    @property
    def storage_path(self):
        """Backward compatibility: storage_path -> file_path."""
        return self.file_path

    @property
    def uploaded_by(self):
        """Backward compatibility: uploaded_by -> user_id."""
        from uuid import UUID
        try:
            return UUID(self.user_id) if isinstance(self.user_id, str) else self.user_id
        except (ValueError, AttributeError):
            return self.user_id

    @property
    def uploaded_at(self):
        """Backward compatibility: uploaded_at -> created_at."""
        return self.created_at

    @property
    def tags(self):
        """Backward compatibility: tags from metadata."""
        if self.metadata:
            return self.metadata.get("tags", [])
        return []

    @property
    def linked_cases(self):
        """Backward compatibility: linked_cases as list with case_id."""
        from uuid import UUID
        # Always get linked_cases from metadata if available
        if self.metadata and "linked_cases" in self.metadata:
            cases = self.metadata["linked_cases"]
            # Convert strings to UUIDs
            return [UUID(c) if isinstance(c, str) else c for c in cases]
        # Fallback to case_id if not standalone
        if self.case_id != "standalone":
            try:
                return [UUID(self.case_id) if isinstance(self.case_id, str) else self.case_id]
            except (ValueError, AttributeError):
                return [self.case_id]
        return []

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
