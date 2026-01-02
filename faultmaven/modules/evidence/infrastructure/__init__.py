"""Evidence Infrastructure Layer

Provides infrastructure components for the Evidence module:
- EvidenceRepository: Database operations for evidence records
- EvidenceStorageAdapter: File storage operations
"""

from faultmaven.modules.evidence.infrastructure.storage_adapter import (
    EvidenceStorageAdapter,
)
from faultmaven.modules.evidence.infrastructure.persistence.evidence_repository import (
    EvidenceRepository,
)

__all__ = [
    "EvidenceStorageAdapter",
    "EvidenceRepository",
]
