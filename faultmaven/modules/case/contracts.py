"""Case Module Contracts

This module defines the public interfaces (contracts) for the Case vertical module.
Other modules should import from here, not from infrastructure or domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication
- Case module owns evidence, reports, and agent execution data (via FK relationships)

Per module-organization-design.md (lines 592-605, 757-770):
- Domain Services (Evidence, Agent, Report) should import from Case contracts
- Case contracts export models for Case-owned tables (evidence, reports)
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

    async def list_all_case_ids(self) -> List[str]:
        """Every case row's id, regardless of state or owner.

        The reference set for the orphaned-collection sweep (case_cleanup):
        a collection is orphaned only when no case row exists at all. Under
        the multi-tenant provider a complete set requires the maintenance DB
        role (the jobs runner enforces this for cross_tenant jobs).
        """
        ...

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        state: Optional["CaseState"] = None,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        include_empty: bool = True,
    ) -> tuple[List["Case"], int]:
        """List cases with optional filters.

        ``include_empty`` gates empty cases (``current_turn == 0``): when
        ``False`` the ``current_turn > 0`` predicate is applied in SQL so it
        constrains BOTH the returned page and the total count (keeping the
        page/total pagination contract sound).

        ``shared_case_ids`` widens the owner-only scope to
        ``owned ∪ shared-to-my-teams`` (ADR-013 §D4): case ids the requester can
        read via a team share, resolved from ``resource_shares`` by the caller.
        Empty/omitted leaves the pre-existing owner-only filter.

        ``restrict_case_ids`` is the filter-by-team facet: an explicit case-id
        allowlist ANDed onto the visibility scope to narrow results to one team's
        shares (the caller resolves and authorizes the team). ``None`` = no facet;
        a non-``None`` empty list matches nothing.
        """
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
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
    ) -> tuple[List["Case"], int]:
        """Search cases by text query.

        ``shared_case_ids`` widens the owner-only scope to
        ``owned ∪ shared-to-my-teams`` (ADR-013 §D4); ``restrict_case_ids`` is the
        filter-by-team facet that narrows to one team's shares. See ``list``.
        """
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

        The aggregate save(case) does NOT delete these rows (its upserts are
        purely additive), so removal has to be explicit — this is the only
        path that removes one.
        Returns True if a row was removed, False if no such evidence existed.
        """
        ...

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Delete a single uploaded_file row.

        The aggregate save(case) does NOT delete these rows (its upserts are
        purely additive), so removal has to be explicit — this is the only
        path that removes one.
        Returns True if a row was removed, False if no such file existed.
        """
        ...

    async def add_uploaded_file(
        self, case_id: str, uploaded_file: "UploadedFile", organization_id: str
    ) -> None:
        """Commit ONE uploaded_file row on its own, outside the aggregate save.

        An upload is a user-initiated fact: the bytes are already in storage by
        the time this is called, and whether the turn's LLM later succeeds has
        no bearing on whether the user uploaded the file. Committing the row
        here keeps the row and the bytes consistent. Previously the row rode
        along on the end-of-turn ``save(case)``, so a failed turn left the bytes
        stored and unreferenced, and the retry stored a second copy —
        ``find_uploaded_file_by_content_hash`` could not dedup against a row
        that was never written.

        Scoped rather than ``save(case)`` because the aggregate save commits the
        WHOLE case: mid-turn that would make the half-built turn durable (the
        user message appended at step 2, the bumped ``current_turn``), which is
        exactly what deferring the save exists to avoid. This commits the upload
        without committing the turn.

        Ordering with the later aggregate save is safe because
        ``_upsert_uploaded_files`` is purely additive — it re-upserts this row
        rather than deleting it. (Note for anyone extending this: that is NOT
        true of ``causal_nodes``/``causal_edges``, which the aggregate save does
        reconcile destructively.) Idempotent — re-committing the same file_id
        updates in place.
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


class CaseStateDTO(str, Enum):
    """Public case state enum for cross-module use.

    MUST mirror ``domain.models.CaseState``, which is the single authority on
    the lifecycle; the persistence enum mirrors it too. Adding a state means
    changing all three plus a migration. Parity is enforced by
    ``tests/unit/modules/case/test_case_state_dto_parity.py``.
    """

    INQUIRY = "inquiry"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass
class CaseDTO:
    """Public case representation for cross-module use.

    This DTO exposes only the fields needed by other modules,
    hiding internal case implementation details.
    """

    case_id: str
    title: str
    state: CaseStateDTO
    user_id: str
    organization_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# Re-export domain models so other modules can import them from
# ``case.contracts`` instead of reaching into ``case.domain.models``
# directly (per the layer-boundary import-linter contract).
from faultmaven.modules.case.domain.models import (  # noqa: E402
    TERMINAL_HYPOTHESIS_STATES,
    ActionAttempt,
    Case,
    CaseAction,
    CaseEntity,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    CauseAssuranceGrade,
    CauseState,
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
    HypothesisState,
    InquiryData,
    InterventionQuadrant,
    InvestigationActionType,
    InvestigationMomentum,
    InvestigationProgress,
    InvestigationStage,
    InvestigationStrategy,
    JournalEntry,
    KnowledgeMatch,
    KnowledgeResolution,
    MitigationRecord,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    PreliminaryUrgency,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionFeasible,
    SolutionOutcome,
    SolutionState,
    SolutionType,
    TemporalState,
    TurnOutcome,
    TurnProgress,
    UploadedFile,
    UrgencyLevel,
    ValidationMethod,
    VerificationStatus,
    WorkingConclusion,
    classify_solution_outcome,
)

# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # Repository and Service Contracts
    "ICaseRepository",
    # DTOs
    "CaseStateDTO",
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
    # Case-owned Checkpoint models
    "CaseCheckpoint",
    # Investigation models from Agent module (shared for investigation coordination)
    # Case domain models
    "Case",
    "CaseAction",
    "CaseSeverity",
    "CaseState",
    "CausalEdge",
    "CausalNode",
    "CauseAssuranceGrade",
    "CauseState",
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
    "HypothesisState",
    "InvestigationMomentum",
    "InvestigationProgress",
    "InvestigationStage",
    "InvestigationStrategy",
    "JournalEntry",
    "KnowledgeMatch",
    "KnowledgeResolution",
    "NeedObtainability",
    "NeedPriority",
    "NeedPurpose",
    "NeedState",
    "NodeEvidenceLink",
    "NodeState",
    "NodeType",
    "InterventionQuadrant",
    "ValidationMethod",
    "PreliminaryUrgency",
    "ProblemVerification",
    "RootCauseConclusion",
    "Solution",
    "SolutionFeasible",
    "SolutionOutcome",
    "SolutionState",
    "SolutionType",
    "classify_solution_outcome",
    "MitigationRecord",
    "TemporalState",
    "TERMINAL_HYPOTHESIS_STATES",
    "TurnOutcome",
    "TurnProgress",
    "UploadedFile",
    "UrgencyLevel",
    "VerificationStatus",
    "WorkingConclusion",
]
