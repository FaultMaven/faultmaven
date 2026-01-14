"""Report Module Contracts

This module defines the public interfaces (contracts) for the Report vertical module.
Other modules should import from here, not from domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication

Per Principle 2 (Vertical Modules with Contracts):
- External code imports from contracts.py
- Internal domain models are re-exported for backward compatibility
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, Optional, List, Dict, Any, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    pass  # Type-only imports if needed


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================


class ReportTypeDTO(str, Enum):
    """Public report type enum for cross-module use."""

    RCA = "rca"  # Root Cause Analysis
    EXECUTIVE_SUMMARY = "executive_summary"
    RUNBOOK = "runbook"
    TECHNICAL_POSTMORTEM = "technical_postmortem"


class ReportStatusDTO(str, Enum):
    """Public report status enum for cross-module use."""

    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ReportDTO:
    """Public report representation for cross-module use.

    This DTO exposes only the fields needed by other modules,
    hiding internal report implementation details.
    """

    report_id: UUID
    case_id: str
    report_type: ReportTypeDTO
    status: ReportStatusDTO
    title: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    content: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# Re-export domain models for backward compatibility
# ============================================================

from faultmaven.modules.report.domain.models import (
    # Enums
    ReportType,
    ReportStatus,
    RunbookSource,
    # Models
    CaseReport,
    RunbookMetadata,
    SimilarRunbook,
    RunbookRecommendation,
    ReportRecommendation,
    # Request/Response DTOs
    ReportGenerationRequest,
    ReportGenerationResponse,
    CaseClosureRequest,
    CaseClosureResponse,
)


# ============================================================
# Service Protocols
# ============================================================


class IReportQuery(Protocol):
    """Read-only report query interface for cross-module use."""

    async def get_report(self, report_id: "UUID") -> Optional[CaseReport]:
        """Get report by ID."""
        ...

    async def list_reports_for_case(
        self,
        case_id: str,
        report_type: Optional[ReportType] = None,
        limit: int = 100,
    ) -> List[CaseReport]:
        """List reports for a case with optional filtering."""
        ...


class IReportGenerationService(Protocol):
    """Report generation service interface for cross-module use."""

    async def generate_report(
        self,
        request: ReportGenerationRequest,
    ) -> ReportGenerationResponse:
        """Generate a report for a case."""
        ...

    async def get_recommendations(
        self,
        case_id: str,
    ) -> List[ReportRecommendation]:
        """Get report recommendations for a case."""
        ...


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # DTOs (preferred for new code)
    "ReportTypeDTO",
    "ReportStatusDTO",
    "ReportDTO",
    # Domain re-exports (backward compatibility)
    "ReportType",
    "ReportStatus",
    "RunbookSource",
    "CaseReport",
    "RunbookMetadata",
    "SimilarRunbook",
    "RunbookRecommendation",
    "ReportRecommendation",
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
    # Protocols
    "IReportQuery",
    "IReportGenerationService",
]
