"""Evidence Module Contracts

This module defines the public interfaces (contracts) for the Evidence module.
Other modules should import from here, not from domain directly.

Following the design in module-organization-design.md:
- Modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
"""

from typing import Protocol, Optional, List, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from uuid import UUID
    from datetime import datetime


# ============================================================
# Enums for Cross-Module Use
# ============================================================

# Re-export enums from domain models for cross-module use
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifactType,
    StorageBackend,
)


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================

# EvidenceArtifact can be used directly or via DTOs
# For now, re-export from domain for backward compatibility
from faultmaven.modules.evidence.domain.models import EvidenceArtifact


# ============================================================
# Service Protocols
# ============================================================

class IEvidenceQuery(Protocol):
    """Read-only evidence query interface for cross-module use."""

    async def get_evidence(self, evidence_id: 'UUID') -> Optional['EvidenceArtifact']:
        """Get evidence by ID."""
        ...

    async def list_evidence_for_case(
        self,
        case_id: str,
        evidence_type: Optional[EvidenceArtifactType] = None,
        limit: int = 100,
    ) -> List['EvidenceArtifact']:
        """List evidence for a case with optional filtering."""
        ...


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # Enums
    "EvidenceArtifactType",
    "StorageBackend",
    # Models
    "EvidenceArtifact",
    # Protocols
    "IEvidenceQuery",
]
