"""Evidence Module - Vertical Slice

Manages evidence upload, storage, linking, and retrieval across cases.

Public API:
    From domain.models:
        - EvidenceArtifact, EvidenceArtifactType, StorageBackend
        - EvidenceUploadRequest, EvidenceLinkRequest, EvidenceListFilter

    From domain.services:
        - EvidenceService

Structure:
- domain/: Evidence domain models and business logic
- infrastructure/: File storage and database repositories
- api/: FastAPI routes for evidence endpoints
"""

# Domain models
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceLinkRequest,
    EvidenceListFilter,
    EvidenceUploadRequest,
    StorageBackend,
)

# Domain services
from faultmaven.modules.evidence.domain.services import EvidenceService

__all__ = [
    # Models
    "EvidenceArtifact",
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    # Services
    "EvidenceService",
]
