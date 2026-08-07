"""Case Module Owned Models.

This package contains all domain models owned by the Case module, including:
- Case-owned evidence DTOs (EvidenceUploadRequest, etc.)
- Case-owned report models (CaseReport, etc.)

Per module-organization-design.md:
- Case module owns evidence and report tables
- Evidence and Report modules are Domain Services that operate on Case-owned data
- These models are canonical and should be imported from Case contracts
"""

# Evidence DTOs (Case owns evidence table per module-organization-design.md).
# The canonical Evidence domain model lives at
# faultmaven.modules.case.domain.models.Evidence; this submodule carries the
# upload/link/list DTOs only.
from faultmaven.modules.case.domain.owned_models.evidence import (
    EvidenceArtifactType,
    EvidenceLinkRequest,
    EvidenceListFilter,
    EvidenceUploadRequest,
    StorageBackend,
)

# Report models (Case owns reports table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.report import (
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
    # Evidence DTOs
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    # Report models
    "ReportType",
    "ReportStatus",
    "RunbookSource",
    "RunbookMetadata",
    "CaseReport",
    "SimilarRunbook",
    "RunbookRecommendation",
    "ReportRecommendation",
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
]
