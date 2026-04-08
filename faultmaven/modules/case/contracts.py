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

from abc import ABC
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
# Removed deprecated OODA models


# Case-owned Agent Execution models (Case module owns agent audit data per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

# Case-owned Checkpoint models (Case module owns checkpoints table)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint

# Case-owned Evidence models (Case module owns evidence table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactResponse,
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

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for a case."""
        ...

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired/old cases."""
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

    async def update_report(self, report: CaseReport) -> CaseReport:
        """Update an existing report."""
        ...

    async def delete_report(self, report_id: str) -> bool:
        """Delete a report by ID."""
        ...

    # Standalone Evidence Operations (migrated from Evidence module)
    async def create_standalone_evidence(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: UUID,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> EvidenceArtifact:
        """Create standalone evidence record (can link to multiple cases)."""
        ...

    async def get_standalone_evidence(
        self, evidence_id: UUID
    ) -> Optional[EvidenceArtifact]:
        """Get standalone evidence by ID."""
        ...

    async def list_standalone_evidence(
        self, filters: EvidenceListFilter
    ) -> tuple[List[EvidenceArtifact], int]:
        """List standalone evidence with filters."""
        ...

    async def delete_standalone_evidence(self, evidence_id: UUID) -> bool:
        """Delete standalone evidence record."""
        ...

    async def link_standalone_evidence_to_case(
        self, evidence_id: UUID, case_id: UUID
    ) -> Optional[EvidenceArtifact]:
        """Link standalone evidence to a case."""
        ...

    async def update_standalone_evidence(
        self, evidence: EvidenceArtifact
    ) -> EvidenceArtifact:
        """Update standalone evidence record."""
        ...

    async def set_primary_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Set evidence as primary for a case (unsets others for the same case)."""
        ...

    async def get_primary_evidence(self, case_id: str) -> Optional[EvidenceArtifact]:
        """Get primary evidence for a case."""
        ...

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
# Hypothesis & Solution Repository Contracts
# ============================================================


class IHypothesisRepository(Protocol):
    """Repository interface for Hypothesis persistence operations.

    Agent module should import this Protocol instead of the concrete
    HypothesisRepository ABC from infrastructure.persistence.
    """

    async def create_hypothesis(
        self,
        case_id: str,
        organization_id: str,
        statement: str,
        created_by: str,
        status: Optional[str] = "captured",
        likelihood: Optional[Decimal] = None,
        category: str = "other",
        evidence_links: Optional[Dict] = None,
        metadata: Optional[Dict] = None,
    ) -> "Hypothesis": ...

    async def get_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> Optional["Hypothesis"]: ...

    async def list_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List["Hypothesis"], int]: ...

    async def update_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
        updated_by: str,
        statement: Optional[str] = None,
        status: Optional[str] = None,
        likelihood: Optional[Decimal] = None,
        evidence_links: Optional[Dict] = None,
        retirement_reason: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional["Hypothesis"]: ...

    async def delete_hypothesis(
        self,
        hypothesis_id: str,
        organization_id: str,
    ) -> bool: ...

    async def count_hypotheses_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
    ) -> int: ...


class ISolutionRepository(Protocol):
    """Repository interface for Solution persistence operations.

    Agent module should import this Protocol instead of the concrete
    SolutionRepository ABC from infrastructure.persistence.
    """

    async def create_solution(
        self,
        case_id: str,
        organization_id: str,
        description: str,
        created_by: str,
        status: Optional[str] = "proposed",
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> "Solution": ...

    async def get_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> Optional["Solution"]: ...

    async def list_solutions_by_case(
        self,
        case_id: str,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List["Solution"], int]: ...

    async def update_solution(
        self,
        solution_id: str,
        organization_id: str,
        updated_by: str,
        description: Optional[str] = None,
        status: Optional[str] = None,
        implementation_steps: Optional[List[str]] = None,
        risk_level: Optional[str] = None,
        estimated_effort: Optional[str] = None,
        verification_result: Optional[str] = None,
        implemented: Optional[bool] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional["Solution"]: ...

    async def delete_solution(
        self,
        solution_id: str,
        organization_id: str,
    ) -> bool: ...


# ============================================================
# Service Contract
# ============================================================


class ICaseService(ABC):
    """
    Service interface for Case business logic and orchestration.

    This interface defines the contract for case management business operations,
    coordinating between case storage, session management, and other services.
    """

    # Note: Using ABC here because ICaseService is already defined in models/interfaces_case.py
    # We'll import and re-export it, or define a simplified version here.
    # For now, we'll reference the existing interface.
    pass


# ============================================================
# Import and Re-export existing interfaces from models
# ============================================================

# Re-export ICaseService from models/interfaces_case for backward compatibility
# Eventually, this should be migrated fully to contracts.py
from faultmaven.models.interfaces_case import ICaseService as _ICaseService

ICaseService = _ICaseService  # Re-export with same name


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


# Re-export domain models for backward compatibility
# These can be used directly until full DTO migration is complete
# Services should import from contracts.py (not domain.models) per Principle 2
from faultmaven.modules.case.domain.models import (  # noqa: E402
    ActionAttempt,
    Case,
    CaseAction,
    CaseSeverity,
    CaseStatus,
    CaseStatusTransition,  # Backward compat alias for CaseAction
    ConfidenceLevel,
    DocumentationData,
    DocumentType,
    EscalationState,
    EscalationType,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
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
    "ICaseService",
    "IHypothesisRepository",
    "ISolutionRepository",
    # DTOs
    "CaseStatusDTO",
    "CaseDTO",
    # Case-owned Evidence models (per module-organization-design.md)
    "EvidenceArtifactType",
    "StorageBackend",
    "EvidenceArtifact",
    "EvidenceUploadRequest",
    "EvidenceLinkRequest",
    "EvidenceListFilter",
    "EvidenceArtifactResponse",
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
    # Case domain models (backward compatibility)
    "Case",
    "CaseAction",
    "CaseSeverity",
    "CaseStatus",
    "CaseStatusTransition",  # Backward compat alias for CaseAction
    "ConfidenceLevel",
    "InquiryData",
    "DocumentationData",
    "DocumentType",
    "EscalationState",
    "EscalationType",
    "Evidence",
    "EvidenceCategory",
    "EvidenceForm",
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
