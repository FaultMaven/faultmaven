"""
Case Module Public Contracts

This file defines the public interface of the case module.
Other modules MUST only import from this file, never from
domain/ or infrastructure/ directories.

Contracts include:
- DTOs: Data transfer objects for cross-module communication
- Protocols: Interface definitions for dependency injection
- Exceptions: Re-exported domain exceptions
- Enums: Public enumerations needed by other modules
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ============================================
# Public Enumerations
# ============================================


class CaseStatusDTO(str, Enum):
    """Public representation of case lifecycle status.

    Lifecycle Flow:
      CONSULTING → INVESTIGATING → RESOLVED (terminal)
                                 → CLOSED (terminal)
               ↘ CLOSED (terminal)
    """

    CONSULTING = "consulting"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    CLOSED = "closed"

    @property
    def is_terminal(self) -> bool:
        """Check if this status is terminal."""
        return self in [CaseStatusDTO.RESOLVED, CaseStatusDTO.CLOSED]

    @property
    def is_active(self) -> bool:
        """Check if case is active (not terminal)."""
        return self in [CaseStatusDTO.CONSULTING, CaseStatusDTO.INVESTIGATING]


class CaseSeverityDTO(str, Enum):
    """Public representation of case severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class InvestigationProgressDTO:
    """Public representation of investigation progress.

    Milestone-based progress tracking for case investigation.
    """

    # Verification Milestones
    symptom_verified: bool = False
    scope_assessed: bool = False
    timeline_established: bool = False
    changes_identified: bool = False

    # Investigation Milestones
    root_cause_identified: bool = False
    root_cause_confidence: float = 0.0

    # Resolution Milestones
    solution_proposed: bool = False
    solution_applied: bool = False
    solution_verified: bool = False

    @property
    def completion_percentage(self) -> float:
        """Overall progress percentage (0.0 to 1.0)."""
        milestones = [
            self.symptom_verified,
            self.scope_assessed,
            self.timeline_established,
            self.changes_identified,
            self.root_cause_identified,
            self.solution_proposed,
            self.solution_applied,
            self.solution_verified,
        ]
        completed = sum(milestones)
        return completed / len(milestones) if milestones else 0.0


@dataclass(frozen=True)
class CaseDTO:
    """Public representation of a case.

    This is the cross-module representation of a case entity.
    Contains essential case information for other modules.
    """

    case_id: str
    organization_id: str
    user_id: str
    title: str
    description: str
    status: CaseStatusDTO
    severity: CaseSeverityDTO
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    closure_reason: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class CaseSummaryDTO:
    """Lightweight case summary for list views.

    Contains minimal information for efficient listing.
    """

    case_id: str
    title: str
    status: CaseStatusDTO
    severity: CaseSeverityDTO
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InvestigationSessionDTO:
    """Public representation of an investigation session.

    Represents an active investigation conversation/session.
    """

    session_id: str
    case_id: str
    user_id: str
    created_at: datetime
    last_activity_at: datetime
    is_active: bool = True
    device_id: Optional[str] = None


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class ICaseQuery(Protocol):
    """Read-only case operations for cross-module use.

    Provides read access to case data for other modules.
    """

    async def get_case(self, case_id: str) -> Optional[CaseDTO]:
        """Get case by ID.

        Args:
            case_id: The case's unique identifier

        Returns:
            CaseDTO if found, None otherwise
        """
        ...

    async def get_cases_by_ids(self, case_ids: list[str]) -> list[CaseDTO]:
        """Bulk case lookup - prevents N+1 queries.

        Args:
            case_ids: List of case IDs to fetch

        Returns:
            List of CaseDTO objects (may be fewer than requested if some not found)
        """
        ...

    async def get_cases_for_user(
        self,
        user_id: str,
        organization_id: str,
        status: Optional[CaseStatusDTO] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CaseSummaryDTO]:
        """Get cases for a user with optional filtering.

        Args:
            user_id: The user's unique identifier
            organization_id: The organization's unique identifier
            status: Optional status filter
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of CaseSummaryDTO objects
        """
        ...

    async def get_case_progress(self, case_id: str) -> Optional[InvestigationProgressDTO]:
        """Get investigation progress for a case.

        Args:
            case_id: The case's unique identifier

        Returns:
            InvestigationProgressDTO if case exists, None otherwise
        """
        ...


@runtime_checkable
class ICaseCommand(Protocol):
    """Write operations for case management.

    Provides write access to case data. Use with caution from other modules.
    """

    async def create_case(
        self,
        organization_id: str,
        user_id: str,
        title: str,
        description: str,
        severity: CaseSeverityDTO = CaseSeverityDTO.MEDIUM,
        tags: Optional[List[str]] = None,
    ) -> CaseDTO:
        """Create a new case.

        Args:
            organization_id: The organization's unique identifier
            user_id: The creating user's identifier
            title: Case title
            description: Case description
            severity: Case severity level
            tags: Optional tags for categorization

        Returns:
            Created CaseDTO
        """
        ...

    async def update_case_status(
        self,
        case_id: str,
        status: CaseStatusDTO,
        reason: Optional[str] = None,
    ) -> CaseDTO:
        """Update case status.

        Args:
            case_id: The case's unique identifier
            status: New status
            reason: Optional reason for status change

        Returns:
            Updated CaseDTO
        """
        ...

    async def close_case(
        self,
        case_id: str,
        closure_reason: str,
        closed_by: str,
    ) -> CaseDTO:
        """Close a case.

        Args:
            case_id: The case's unique identifier
            closure_reason: Reason for closure
            closed_by: User ID of who closed the case

        Returns:
            Closed CaseDTO
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
from faultmaven.modules.case.exceptions import (
    CaseAccessError,
    CaseException,
    CaseNotFoundError,
    CaseOperationError,
    CaseStateError,
    CaseValidationError,
)

__all__ = [
    # Enums
    "CaseStatusDTO",
    "CaseSeverityDTO",
    # DTOs
    "CaseDTO",
    "CaseSummaryDTO",
    "InvestigationProgressDTO",
    "InvestigationSessionDTO",
    # Protocols
    "ICaseQuery",
    "ICaseCommand",
    # Exceptions
    "CaseException",
    "CaseNotFoundError",
    "CaseStateError",
    "CaseAccessError",
    "CaseValidationError",
    "CaseOperationError",
]
