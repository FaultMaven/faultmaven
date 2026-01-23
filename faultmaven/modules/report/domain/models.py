"""Report Module Domain Models - Re-export from Case Module.

Per module-organization-design.md:
- Case module owns the reports table (FK to cases)
- Report module is a Domain Service that operates on Case-owned data
- These models are re-exported from Case contracts for backward compatibility

For new code, import from:
    from faultmaven.modules.case.contracts import CaseReport, ReportType, ...

This file re-exports from Case contracts for backward compatibility.
"""

# Re-export all report models from Case contracts
# This maintains backward compatibility while adhering to the design
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

__all__ = [
    # Enumerations
    "ReportType",
    "ReportStatus",
    "RunbookSource",
    # Models
    "RunbookMetadata",
    "CaseReport",
    "SimilarRunbook",
    "RunbookRecommendation",
    "ReportRecommendation",
    # Request/Response DTOs
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
]
