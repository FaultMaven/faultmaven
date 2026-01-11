"""Evidence Domain Adapters.

Provides adapter components for the Evidence module:
- EvidenceStorageAdapter: File storage operations (not persistence infrastructure)
"""

from faultmaven.modules.evidence.domain.adapters.storage_adapter import (
    EvidenceStorageAdapter,
)

__all__ = [
    "EvidenceStorageAdapter",
]
