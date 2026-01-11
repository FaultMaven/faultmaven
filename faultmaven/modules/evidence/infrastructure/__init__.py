"""Evidence Infrastructure Layer (DEPRECATED - Persistence migrated to Case module)

This directory is being phased out as part of the module organization migration.
- EvidenceRepository: DEPRECATED - Migrated to Case module's ICaseRepository
- EvidenceStorageAdapter: MOVED to domain/adapters/ (file storage, not persistence)

Note: This infrastructure directory should be removed once all references are updated.
For file storage adapter, use: from faultmaven.modules.evidence.domain.adapters import EvidenceStorageAdapter
For persistence operations, use: CaseRepository (via ICaseRepository contract)
"""

# DEPRECATED: Import for backward compatibility only
# TODO: Remove this file once all references are updated
from faultmaven.modules.evidence.infrastructure.persistence.evidence_repository import (
    EvidenceRepository,
)

__all__ = [
    "EvidenceRepository",  # DEPRECATED - use CaseRepository instead
]
