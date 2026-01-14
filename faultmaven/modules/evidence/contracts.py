"""Evidence Module Contracts

This module defines the public interfaces (contracts) for the Evidence module.
Other modules should import from here, not from domain directly.

Following the design in module-organization-design.md:
- Modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol
from uuid import UUID

if TYPE_CHECKING:
    pass  # Type-only imports if needed


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================


class EvidenceArtifactTypeDTO(str, Enum):
    """Public evidence type enum for cross-module use."""

    LOG_FILE = "log_file"
    STACK_TRACE = "stack_trace"
    SCREENSHOT = "screenshot"
    METRICS = "metrics"
    CONFIG = "config"
    DOCUMENT = "document"
    CODE_SNIPPET = "code_snippet"
    OTHER = "other"


class StorageBackendDTO(str, Enum):
    """Public storage backend enum for cross-module use."""

    LOCAL = "local"
    S3 = "s3"
    DATABASE = "database"


@dataclass
class EvidenceDTO:
    """Public evidence representation for cross-module use.

    This DTO exposes only the fields needed by other modules,
    hiding internal evidence implementation details.
    """

    evidence_id: UUID
    case_id: str
    artifact_type: EvidenceArtifactTypeDTO
    filename: str
    content_type: str
    size_bytes: int
    storage_backend: StorageBackendDTO
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_primary: bool = False


# ============================================================
# Re-export domain enums for backward compatibility
# ============================================================

# EvidenceArtifact can be used directly or via DTOs
# For now, re-export from domain for backward compatibility
# Services should import from contracts.py (not domain.models) per Principle 2
# Re-export enums from domain models for cross-module use
# Services should import from contracts.py (not domain.models) per Principle 2
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    StorageBackend,
)

# ============================================================
# Re-export domain models for backward compatibility
# ============================================================


# ============================================================
# Service Protocols
# ============================================================


class IEvidenceQuery(Protocol):
    """Read-only evidence query interface for cross-module use."""

    async def get_evidence(self, evidence_id: UUID) -> Optional["EvidenceArtifact"]:
        """Get evidence by ID."""
        ...

    async def list_evidence_for_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 100,
    ) -> List["EvidenceArtifact"]:
        """List evidence for a case with optional filtering."""
        ...


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # DTOs (preferred for new code)
    "EvidenceArtifactTypeDTO",
    "StorageBackendDTO",
    "EvidenceDTO",
    # Domain re-exports (backward compatibility)
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceArtifact",
    "EvidenceListFilter",
    # Protocols
    "IEvidenceQuery",
]
