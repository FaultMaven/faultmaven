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

from typing import Protocol, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


# ============================================================
# Re-export domain models for cross-module use
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

    async def get_report(self, report_id: 'UUID') -> Optional[CaseReport]:
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
    # Enums
    "ReportType",
    "ReportStatus",
    "RunbookSource",
    # Models
    "CaseReport",
    "RunbookMetadata",
    "SimilarRunbook",
    "RunbookRecommendation",
    "ReportRecommendation",
    # Request/Response DTOs
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
    # Protocols
    "IReportQuery",
    "IReportGenerationService",
]
