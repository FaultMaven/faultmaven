"""Evidence Domain Models

Represents evidence files uploaded to FaultMaven for investigation.
"""

from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """Evidence file uploaded for investigation.

    Evidence can be uploaded standalone or linked to specific cases.
    Supports multiple cases linking to the same evidence file.
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    storage_path: str
    uploaded_by: UUID
    uploaded_at: datetime
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    linked_cases: List[UUID] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    class Config:
        from_attributes = True


class EvidenceUploadRequest(BaseModel):
    """Request to upload evidence."""

    filename: str
    content_type: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
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
    limit: int = Field(default=50, le=200)
    offset: int = Field(default=0, ge=0)
