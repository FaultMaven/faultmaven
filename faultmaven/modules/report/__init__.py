"""Report Module - Vertical Slice

Generates case documentation including incident reports, runbooks, and post-mortems.

Public API:
    From modules.case.contracts (reports are Case-owned):
        - ReportType, ReportStatus, RunbookSource
        - CaseReport, ReportRecommendation
        - ReportGenerationRequest, ReportGenerationResponse

    From domain.services (import directly to avoid circular imports):
        - ReportGenerationService, ReportRecommendationService

Structure:
- domain/: Report domain models and services
- api/: FastAPI routes for report endpoints
"""

# Domain models
from faultmaven.modules.case.contracts import (
    CaseClosureRequest,
    CaseClosureResponse,
    CaseReport,
    ReportGenerationRequest,
    ReportGenerationResponse,
    ReportRecommendation,
    ReportStatus,
    ReportType,
    RunbookMetadata,
    RunbookRecommendation,
    RunbookSource,
    SimilarRunbook,
)

# Domain services - import directly to avoid circular imports:
# from faultmaven.modules.report.domain.services.report_generation_service import ReportGenerationService
# from faultmaven.modules.report.domain.services.report_recommendation_service import ReportRecommendationService

__all__ = [
    # Models - Enums
    "ReportType",
    "ReportStatus",
    "RunbookSource",
    # Models - Data
    "RunbookMetadata",
    "CaseReport",
    "SimilarRunbook",
    "RunbookRecommendation",
    "ReportRecommendation",
    # Models - Request/Response
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
]
