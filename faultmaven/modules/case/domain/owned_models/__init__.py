"""Case Module Owned Models.

This package contains all domain models owned by the Case module, including:
- Case-owned evidence models (EvidenceArtifact, etc.)
- Case-owned report models (CaseReport, etc.)
- Case-owned agent execution models (AgentExecution, AgentToolCall)

Per module-organization-design.md:
- Case module owns 11 tables including evidence, reports, and agent executions
- Evidence, Agent, and Report modules are Domain Services that operate on Case-owned data
- These models are canonical and should be imported from Case contracts
"""

# Agent execution models (Case owns agent audit data per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

# Evidence models (Case owns evidence table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactResponse,
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
    # Evidence models
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceArtifact",
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    "EvidenceArtifactResponse",
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
    # Agent execution models
    "ExecutionStatus",
    "AgentType",
    "AgentToolCall",
    "AgentExecution",
]
