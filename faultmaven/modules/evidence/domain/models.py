"""Evidence Module Domain Models - Re-export from Case Module.

Per module-organization-design.md:
- Case module owns the evidence table (FK to cases)
- Evidence module is a Domain Service that operates on Case-owned data
- These models are re-exported from Case contracts for backward compatibility

For new code, import from:
    from faultmaven.modules.case.contracts import EvidenceArtifact, ...

This file re-exports from Case contracts for backward compatibility.
"""

# Re-export all evidence models from Case contracts
# This maintains backward compatibility while adhering to the design
from faultmaven.modules.case.contracts import (
    EvidenceArtifact,
    EvidenceArtifactResponse,
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
    # Domain Model
    "EvidenceArtifact",
    # API DTOs
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    "EvidenceArtifactResponse",
]
