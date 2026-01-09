"""
Report Module Public Contracts

This file defines the public interface of the report module.
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
from typing import Any, Dict, List, Literal, Optional, Protocol, runtime_checkable


# ============================================
# Public Enumerations
# ============================================


class ReportTypeDTO(str, Enum):
    """Public representation of report types."""

    INCIDENT_REPORT = "incident_report"
    RUNBOOK = "runbook"
    POST_MORTEM = "post_mortem"


class ReportStatusDTO(str, Enum):
    """Public representation of report generation status."""

    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class RunbookSourceDTO(str, Enum):
    """Public representation of runbook origin."""

    INCIDENT_DRIVEN = "incident_driven"
    DOCUMENT_DRIVEN = "document_driven"


# ============================================
# Data Transfer Objects (DTOs)
# ============================================


@dataclass(frozen=True)
class ReportDTO:
    """Public representation of a case report.

    This is the cross-module representation of a generated report.
    """

    report_id: str
    case_id: str
    report_type: ReportTypeDTO
    title: str
    content: str
    format: str
    generation_status: ReportStatusDTO
    generated_at: str
    generation_time_ms: int
    is_current: bool = True
    version: int = 1
    linked_to_closure: bool = False


@dataclass(frozen=True)
class ReportSummaryDTO:
    """Lightweight report summary for list views.

    Contains minimal information for efficient listing.
    """

    report_id: str
    case_id: str
    report_type: ReportTypeDTO
    title: str
    generation_status: ReportStatusDTO
    generated_at: str
    is_current: bool = True
    version: int = 1


@dataclass(frozen=True)
class ReportRequestDTO:
    """Request to generate reports.

    Contains parameters for report generation.
    """

    case_id: str
    report_types: tuple[ReportTypeDTO, ...]


@dataclass(frozen=True)
class ReportGenerationResultDTO:
    """Result of report generation.

    Contains the generated reports and metadata.
    """

    case_id: str
    reports: tuple["ReportDTO", ...]
    remaining_regenerations: int


@dataclass(frozen=True)
class RunbookRecommendationDTO:
    """Runbook-specific recommendation.

    Contains recommendation for runbook generation/reuse.
    """

    action: Literal["reuse", "review_or_generate", "generate"]
    existing_runbook: Optional[ReportDTO] = None
    similarity_score: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class ReportRecommendationDTO:
    """Intelligent recommendations for report generation.

    Contains recommendations for which reports to generate.
    """

    case_id: str
    available_for_generation: tuple[ReportTypeDTO, ...]
    runbook_recommendation: RunbookRecommendationDTO


@dataclass(frozen=True)
class CaseClosureResultDTO:
    """Result of case closure with reports.

    Contains closure details and archived reports.
    """

    case_id: str
    closed_at: str
    archived_reports: tuple[ReportDTO, ...]
    download_available_until: str


# ============================================
# Service Protocols (Interfaces)
# ============================================


@runtime_checkable
class IReportQuery(Protocol):
    """Read-only report operations for cross-module use.

    Provides read access to report data for other modules.
    """

    async def get_report(self, report_id: str) -> Optional[ReportDTO]:
        """Get report by ID.

        Args:
            report_id: The report's unique identifier

        Returns:
            ReportDTO if found, None otherwise
        """
        ...

    async def get_reports_by_ids(self, report_ids: list[str]) -> list[ReportDTO]:
        """Bulk report lookup - prevents N+1 queries.

        Args:
            report_ids: List of report IDs to fetch

        Returns:
            List of ReportDTO objects (may be fewer than requested)
        """
        ...

    async def list_reports_for_case(
        self,
        case_id: str,
        report_type: Optional[ReportTypeDTO] = None,
        current_only: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReportSummaryDTO]:
        """List reports for a case with optional filtering.

        Args:
            case_id: The case's unique identifier
            report_type: Optional report type filter
            current_only: Only return current versions
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of ReportSummaryDTO objects
        """
        ...

    async def get_current_report(
        self, case_id: str, report_type: ReportTypeDTO
    ) -> Optional[ReportDTO]:
        """Get the current version of a specific report type for a case.

        Args:
            case_id: The case's unique identifier
            report_type: The report type to retrieve

        Returns:
            ReportDTO if found, None otherwise
        """
        ...

    async def get_recommendations(self, case_id: str) -> ReportRecommendationDTO:
        """Get report generation recommendations for a case.

        Args:
            case_id: The case's unique identifier

        Returns:
            ReportRecommendationDTO with recommendations
        """
        ...


@runtime_checkable
class IReportCommand(Protocol):
    """Write operations for report generation.

    Provides write access to report generation. Use with caution from other modules.
    """

    async def generate_report(
        self, request: ReportRequestDTO
    ) -> ReportGenerationResultDTO:
        """Generate reports for a case.

        Args:
            request: Report generation request parameters

        Returns:
            ReportGenerationResultDTO with generated reports
        """
        ...

    async def regenerate_report(
        self, case_id: str, report_type: ReportTypeDTO
    ) -> ReportDTO:
        """Regenerate a specific report for a case.

        Args:
            case_id: The case's unique identifier
            report_type: The report type to regenerate

        Returns:
            Newly generated ReportDTO
        """
        ...


# ============================================
# Re-exports for convenience
# ============================================

# Export exceptions from module exceptions
from faultmaven.modules.report.exceptions import (
    ReportAccessError,
    ReportException,
    ReportExportError,
    ReportGenerationError,
    ReportNotFoundError,
    ReportTemplateError,
    ReportValidationError,
)

__all__ = [
    # Enums
    "ReportTypeDTO",
    "ReportStatusDTO",
    "RunbookSourceDTO",
    # DTOs
    "ReportDTO",
    "ReportSummaryDTO",
    "ReportRequestDTO",
    "ReportGenerationResultDTO",
    "RunbookRecommendationDTO",
    "ReportRecommendationDTO",
    "CaseClosureResultDTO",
    # Protocols
    "IReportQuery",
    "IReportCommand",
    # Exceptions
    "ReportException",
    "ReportNotFoundError",
    "ReportGenerationError",
    "ReportTemplateError",
    "ReportExportError",
    "ReportAccessError",
    "ReportValidationError",
]
