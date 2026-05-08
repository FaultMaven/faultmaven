"""Evidence Module Domain Models - Re-export from Case Module.

Per module-organization-design.md:
- Case module owns the evidence table (FK to cases)
- Evidence module is a Domain Service that operates on Case-owned data
- These DTOs are re-exported from Case contracts so Domain Services
  don't reach into the Case module's internal layout.

For new code, import from:
    from faultmaven.modules.case.contracts import EvidenceUploadRequest, ...
"""

from faultmaven.modules.case.contracts import (
    EvidenceArtifactType,
    EvidenceLinkRequest,
    EvidenceListFilter,
    EvidenceUploadRequest,
    StorageBackend,
)

__all__ = [
    # Enumerations
    "EvidenceArtifactType",
    "StorageBackend",
    # API DTOs
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
]
