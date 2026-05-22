"""Case Module Contracts

This module defines the public interfaces (contracts) for the Case vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
- Case module owns evidence, reports, and agent execution data (via FK relationships)

Per module-organization-design.md (lines 592-605, 757-770):
- Domain Services (Evidence, Agent, Report) should import from Case contracts
- Case contracts export models for Case-owned tables (evidence, reports, agent_executions)
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple
from uuid import UUID

# ============================================================
# TYPE_CHECKING imports - for type hints only
# ============================================================

if TYPE_CHECKING:
    pass  # All types now imported directly below


# ============================================================
# Import and Re-export Case-owned models
# ============================================================

# Investigation models from Agent module (shared for investigation coordination)


# Case-owned Agent Execution models (Case module owns agent audit data per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

# Case-owned Checkpoint models (Case module owns checkpoints table)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint

# Case-owned Evidence DTOs (Case module owns evidence table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.evidence import (
    EvidenceArtifactType,
    EvidenceLinkRequest,
    EvidenceListFilter,
    EvidenceUploadRequest,
    StorageBackend,
)

# Case-owned Report models (Case module owns reports table per module-organization-design.md)
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

# ============================================================
# Repository Contract
# ============================================================


class ICaseRepository(Protocol):
    """
    Repository interface for Case persistence operations.

    This is a Protocol (structural typing) that allows any implementation
    that matches this interface to be used. Concrete implementations are:
    - CaseRepository (abstract base class in infrastructure/case_repository.py)
    - InMemoryCaseRepository
    - PostgreSQLHybridCaseRepository
    """

    async def save(self, case: "Case") -> "Case":
        """Save case to persistence layer."""
        ...

    async def get(self, case_id: str) -> Optional["Case"]:
        """Retrieve case by ID."""
        ...

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional["CaseStatus"] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List["Case"], int]:
        """List cases with optional filters."""
        ...

    async def delete(self, case_id: str) -> bool:
        """Delete case by ID."""
        ...

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List["Case"], int]:
        """Search cases by text query."""
        ...

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add a message to a case."""
        ...

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """Get messages for a case with pagination."""
        ...

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update case last_activity_at timestamp."""
        ...

    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Scoped update of cosmetic metadata fields (title, description).

        Does NOT bump ``cases.version``. These fields are not part of
        the investigation state machine — they're labels shown to the
        user. Concurrent writes to title/description must not invalidate
        an in-flight turn's save (which can take tens of seconds during
        an LLM tool loop).

        Status/closure_reason still go through the versioned ``save``
        path because they are investigation state.
        """
        ...

    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """Update the `vectorized` flag on a single evidence row.

        Scoped single-field update — does not rewrite the case aggregate.
        Safe to call from background tasks holding a stale Case snapshot,
        since it touches only the one column on the one row.
        """
        ...

    async def delete_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Delete a single evidence row.

        Explicit alternative to mirror-delete via aggregate save(case).
        Returns True if a row was removed, False if no such evidence existed.
        """
        ...

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Delete a single uploaded_file row.

        Explicit alternative to mirror-delete via aggregate save(case).
        Returns True if a row was removed, False if no such file existed.
        """
        ...

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for a case."""
        ...

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired/old cases."""
        ...

    async def find_uploaded_file_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional["UploadedFile"]:
        """Return the oldest UploadedFile in this case whose content_hash matches.

        Post-010 strict evidence model: file uploads create only an
        UploadedFile row (no auto-Evidence at intake), so dedup is
        a file-level concern now. An attachment whose SHA-256 content
        hash already exists on the same case returns the existing
        UploadedFile instead of creating a new row.

        Args:
            case_id: Case to search within (scope is per-case, not global).
            content_hash: SHA-256 hex of UTF-8 text (as produced by
                PreprocessingService.classify_and_extract).

        Returns:
            The oldest matching UploadedFile (by upload timestamp) if
            found, None otherwise. NULL content_hash rows are never
            matched.
        """
        ...

    # Report operations (TD-001: reports stored via Case repository)
    async def add_report(self, report: CaseReport) -> CaseReport:
        """Save report to persistence layer."""
        ...

    async def get_report(self, report_id: str) -> Optional[CaseReport]:
        """Retrieve a report by ID."""
        ...

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional[ReportType] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List[CaseReport]:
        """Get reports for a case with optional filtering."""
        ...

    async def count_reports(
        self,
        case_id: str,
        report_type: Optional[ReportType] = None,
    ) -> int:
        """Count persisted reports for a case, optionally filtered by type.

        Counts ALL rows (every regeneration adds a new row), not just the
        current one — this is the metric the regeneration cap enforces.
        Used by ReportGenerationService to gate further regenerations and
        by the milestone engine to compute the "N regenerations remaining"
        label on the regen affordance.
        """
        ...

    async def update_report(self, report: CaseReport) -> CaseReport:
        """Update an existing report."""
        ...

    async def delete_report(self, report_id: str) -> bool:
        """Delete a report by ID."""
        ...

    # Standalone evidence operations (create/get/list/delete/link/update,
    # set/get primary) were removed in storage redesign 2026-04 phase 2.
    # Standalone evidence path is deleted; evidence is case-tied only and
    # accessed via `case.evidence` loaded by the case repository.

    # Agent Execution Operations (migrated from Agent module)
    async def create_agent_execution(self, execution: AgentExecution) -> AgentExecution:
        """Create new agent execution record."""
        ...

    async def get_agent_execution(self, execution_id: str) -> Optional[AgentExecution]:
        """Get agent execution by ID with tool calls loaded."""
        ...

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        agent_type: Optional[AgentType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[AgentExecution], int]:
        """List agent executions for a case with optional filters."""
        ...

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional[ExecutionStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[AgentExecution], int]:
        """List agent executions for a session with optional filters."""
        ...

    async def update_agent_execution(self, execution: AgentExecution) -> AgentExecution:
        """Update agent execution status and results."""
        ...

    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution by ID (cascades to tool calls)."""
        ...

    async def create_agent_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Create new agent tool call record."""
        ...

    async def update_agent_tool_call(self, tool_call: AgentToolCall) -> AgentToolCall:
        """Update agent tool call status and results."""
        ...

    async def get_agent_tool_calls_for_execution(
        self,
        execution_id: str,
    ) -> List[AgentToolCall]:
        """Get all tool calls for an execution."""
        ...

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case."""
        ...

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> Optional[AgentExecution]:
        """Get the most recent agent execution for a case."""
        ...

    # Checkpoint Operations
    async def create_checkpoint(self, checkpoint: CaseCheckpoint) -> CaseCheckpoint:
        """Create a new case checkpoint."""
        ...

    async def get_checkpoint(self, checkpoint_id: str) -> Optional[CaseCheckpoint]:
        """Get a checkpoint by ID."""
        ...

    async def get_checkpoints(self, case_id: str) -> List[CaseCheckpoint]:
        """Get all checkpoints for a case."""
        ...


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================


class CaseStatusDTO(str, Enum):
    """Public case status enum for cross-module use."""

    INQUIRY = "inquiry"
    INVESTIGATING = "investigating"
    DOCUMENTING = "documenting"
    RESOLVED = "resolved"
    RESOLVED_WITH_WORKAROUND = "resolved_with_workaround"
    RESOLVED_BY_USER = "resolved_by_user"
    CLOSED = "closed"
    ABANDONED = "abandoned"


@dataclass
class CaseDTO:
    """Public case representation for cross-module use.

    This DTO exposes only the fields needed by other modules,
    hiding internal case implementation details.
    """

    case_id: str
    title: str
    status: CaseStatusDTO
    user_id: str
    organization_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# Re-export domain models so other modules can import them from
# ``case.contracts`` instead of reaching into ``case.domain.models``
# directly (per the layer-boundary import-linter contract).
from faultmaven.modules.case.domain.models import (  # noqa: E402
    ActionAttempt,
    Case,
    CaseAction,
    CaseEntity,
    CaseSeverity,
    CaseStatus,
    ConfidenceLevel,
    DocumentationData,
    DocumentType,
    EntityType,
    EscalationState,
    EscalationType,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    GeneratedDocument,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisStatus,
    InquiryData,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationPath,
    InvestigationProgress,
    InvestigationStage,
    InvestigationStrategy,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    NeedPriority,
    NeedPurpose,
    NeedStatus,
    PathSelection,
    PreliminaryUrgency,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionType,
    TemporalState,
    TurnOutcome,
    TurnProgress,
    UploadedFile,
    UrgencyLevel,
    WorkingConclusion,
)

# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # Repository and Service Contracts
    "ICaseRepository",
    # DTOs
    "CaseStatusDTO",
    "CaseDTO",
    # Case-owned Evidence DTOs (per module-organization-design.md)
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    # Case-owned Report models (per module-organization-design.md)
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
    # Case-owned Agent Execution models (per module-organization-design.md)
    "ExecutionStatus",
    "AgentType",
    "AgentToolCall",
    "AgentExecution",
    # Case-owned Checkpoint models
    "CaseCheckpoint",
    # Investigation models from Agent module (shared for investigation coordination)
    # Case domain models
    "Case",
    "CaseAction",
    "CaseSeverity",
    "CaseStatus",
    "ConfidenceLevel",
    "InquiryData",
    "DocumentationData",
    "DocumentType",
    "EscalationState",
    "EscalationType",
    "Evidence",
    "EvidenceCategory",
    "EvidenceNeed",
    "EvidenceSourceType",
    "EvidenceStance",
    "GeneratedDocument",
    "Hypothesis",
    "HypothesisCategory",
    "HypothesisEvidenceLink",
    "HypothesisGenerationMode",
    "HypothesisStatus",
    "InvestigationMomentum",
    "InvestigationPath",
    "InvestigationProgress",
    "InvestigationStage",
    "InvestigationStrategy",
    "JournalEntry",
    "KnowledgeMatch",
    "KnowledgeResolution",
    "NeedPriority",
    "NeedPurpose",
    "NeedStatus",
    "PathSelection",
    "PreliminaryUrgency",
    "ProblemVerification",
    "RootCauseConclusion",
    "Solution",
    "SolutionType",
    "TemporalState",
    "TurnOutcome",
    "TurnProgress",
    "UploadedFile",
    "UrgencyLevel",
    "WorkingConclusion",
]
