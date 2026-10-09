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
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)
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

# Case-owned Evidence DTOs (Case module owns evidence table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.evidence import (
    EvidenceArtifactType,
    EvidenceLinkRequest,
    EvidenceListFilter,
    EvidenceUploadRequest,
    StorageBackend,
)

# case_messages rows: markers, flags, predicates, and the ONE constructor. They
# are the shape of a row in a table this module owns; their readers and writers
# are in other modules, which import them from here. ``append_message_row`` is
# the only way a row reaches ``Case.messages`` (#1452).
from faultmaven.modules.case.domain.owned_models.message_row import (
    EMPTY_AGENT_RESPONSE_TEXT,
    EMPTY_TURN_TEXT,
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_KB_SOURCES,
    MESSAGE_METADATA_USER_EMPTY,
    MessageRowKind,
    append_message_row,
    is_server_written_assistant_row,
    is_server_written_user_row,
)

# Case-owned Report models (Case module owns reports table per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.report import (
    PERSISTED_REPORT_TYPES,
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
    RunbookRef,
    RunbookSource,
)

# The turn receipt (#1888): the row a keyed turn commits with itself, so a
# retry under the same Idempotency-Key is answered with the committed turn.
from faultmaven.modules.case.domain.owned_models.turn_receipt import (
    TurnReceipt,
    TurnReceiptExistsError,
    TurnReceiptKey,
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

    async def save(
        self,
        case: "Case",
        *,
        reports: Sequence[CaseReport] = (),
        receipt: Optional[TurnReceipt] = None,
    ) -> "Case":
        """Save case to persistence layer.

        ``reports`` and ``receipt`` are written in the SAME transaction as the
        case, after it and before the commit: all of them commit, or none does
        (#1882, #1888). A ``StaleCaseException`` writes nothing. Under
        PostgreSQL RLS the rows are written under the tenant the transaction's
        BEGIN bound, the case's own, and carry the case's enterprise. Every row
        must name ``case`` (``ValueError`` otherwise). A receipt whose key the
        case already holds is refused by the table's unique key, and the whole
        save with it: ``TurnReceiptExistsError``, raised unwrapped, as
        ``StaleCaseException`` is.

        MUTATES ``case.messages``: a row missing ``message_id`` or
        ``created_at`` is completed in place, so the in-memory list carries
        what the stored rows carry (#1418). Both fields are read back — the id
        is the upsert's conflict target and the timestamp is what every read
        orders by — so a row left incomplete in memory would be re-minted and
        re-inserted, or re-stamped and reordered, on the next save. Every
        implementation does this, including the in-memory one, so a caller may
        read either field back after ``save`` on any backend.
        """
        ...

    async def get(self, case_id: str) -> Optional["Case"]:
        """Retrieve case by ID."""
        ...

    async def get_turn_receipt(
        self,
        *,
        enterprise_id: str,
        case_id: str,
        author_id: str,
        idempotency_key: str,
    ) -> Optional[TurnReceipt]:
        """The receipt a committed keyed turn left, or ``None`` (#1888).

        Keyed exactly as the table's unique key, enterprise first: that is the
        index this reads, and under RLS the enterprise the session is bound to.
        ``author_id`` is the CALLER, so a principal can only ever read back its
        own receipts.
        """
        ...

    async def list_all_case_ids(self) -> List[str]:
        """Every case row's id, regardless of state or owner.

        The reference set for the orphaned-collection sweep (case_cleanup):
        a collection is orphaned only when no case row exists at all. Under
        the multi-tenant provider a complete set requires the maintenance DB
        role (the jobs runner enforces this for cross_tenant jobs).
        """
        ...

    async def list_all_storage_refs(self) -> Set[str]:
        """Every non-null ``uploaded_files.storage_ref``, any case, any owner.

        The authority the orphan-file sweep (storage_cleanup) consults before
        deleting a stored object. Under the multi-tenant provider a complete
        set requires the maintenance DB role (the jobs runner enforces this
        for cross_tenant jobs); an RLS-scoped partial view would report live
        files as unreferenced.
        """
        ...

    async def list(
        self,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional["CaseState"] = None,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        include_empty: bool = True,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        driven_only: bool = False,
    ) -> tuple[List["Case"], int]:
        """List cases with optional filters.

        ``source`` narrows to the surface a case originated on (``copilot`` /
        ``slack`` / ``api``). It is applied in the same WHERE clause as the
        count, like every other predicate here. **A FALSY value means "no
        filter"** — implementations test ``if source:``, not
        ``if source is not None:``, so ``None`` and ``""`` both answer
        unfiltered. That is a contract, not an accident of spelling: the
        alternative makes ``""`` mean "match cases whose source is the empty
        string", which matches nothing on a column whose domain is those three
        values, and an implementation that chose differently would answer the
        same call differently from its siblings (faultmaven#1424, where
        ``InMemoryCaseRepository`` accepted ``source`` and applied nothing at
        all). Pinned by
        ``tests/unit/modules/case/test_list_applies_declared_source_1424.py``.

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

        ``created_after``/``created_before`` bound ``created_at`` as a HALF-OPEN
        window, ``[created_after, created_before)`` — inclusive lower, exclusive
        upper. Implementations normalize both to UTC first; see
        ``infrastructure/created_bounds.py`` for why each of those is load-
        bearing rather than a matter of taste.

        Like ``include_empty`` they belong in the query, not in a Python
        post-filter: a bound applied after pagination would drop rows from an
        already-sliced page and disagree with the total.

        ``driven_only`` (``access=write``, ADR-020 D8) narrows the scope to the
        cases whose EFFECTIVE driver is ``user_id``
        (``COALESCE(driver_id, user_id)``), ANDed with the read scope, in the
        same WHERE clause as the count.
        """
        ...

    async def delete(self, case_id: str) -> bool:
        """Delete case by ID."""
        ...

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional["CaseState"] = None,
        limit: int = 20,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        driven_only: bool = False,
    ) -> tuple[List["Case"], int]:
        """Search cases by text query.

        ``state`` narrows the result to one lifecycle state, as ``list`` does,
        and belongs in the same WHERE clause as the text predicate for the same
        reason ``list``'s filters do: search applies its ``limit`` in SQL, so a
        state applied in Python afterwards would thin an already-limited page —
        and would answer "no matching cases" whenever the limit happened to be
        filled by rows in other states.

        ``shared_case_ids`` widens the owner-only scope to
        ``owned ∪ shared-to-my-teams`` (ADR-013 §D4); ``restrict_case_ids`` is the
        filter-by-team facet that narrows to one team's shares;
        ``driven_only`` narrows to the cases the caller drives. See ``list``.

        Returns ``(page, total_count)``, where the total is the count of ALL
        matches — computed from the same WHERE clause, before the ``limit``,
        exactly as ``list`` computes it. Three implementations used to return
        ``len(page)`` here, which is the page length wearing the name of a
        total; nothing reads the value today (``search_cases`` discards it and
        the route is ``response_model=List[CaseSummary]``), which is why it
        stayed wrong. Same safe-direction divergence ``list`` documents: a raw
        COUNT over-reports if a row fails to hydrate, and never hides one.
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
        purely additive), so targeted removal has to be explicit. (Deleting
        the whole case still removes them — the FK is ON DELETE CASCADE.)
        Returns True if a row was removed, False if no such evidence existed.
        """
        ...

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Delete a single uploaded_file row.

        The aggregate save(case) does NOT delete these rows (its upserts are
        purely additive), so targeted removal has to be explicit. (Deleting
        the whole case still removes them — the FK is ON DELETE CASCADE.)
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

    async def reassign_driver(
        self,
        case_id: str,
        *,
        driver_id: Optional[str],
        expected_version: int,
        change: "CaseDriverChange",
    ) -> Optional[int]:
        """Set the stored driver (ADR-020 D4), versioned: one compare-and-swap
        on ``cases.version`` that bumps it, and the ``case_driver_changed``
        audit row, in ONE transaction.

        ``driver_id`` is the STORED value — ``None`` when the creator is to
        drive. Returns the new version, or ``None`` when the row's version is
        no longer ``expected_version`` (nothing written); the caller reloads
        and re-decides. The bump is what makes an in-flight turn's save fail
        with a version conflict.
        """
        ...

    async def release_driver(
        self, case_id: str, *, driver_id: str, change: "CaseDriverChange"
    ) -> bool:
        """Hand a case back to its creator (ADR-020 D3), iff ``driver_id`` still
        drives it by assignment: ``driver_id`` set to NULL, ``version`` bumped,
        and the audit row, in ONE transaction. Conditional on the stored driver
        rather than on a version, so a concurrent turn's save cannot make a
        release miss. Returns whether the row changed.
        """
        ...

    async def list_cases_driven_by(self, user_id: str) -> List["DrivenCase"]:
        """Every case ``user_id`` drives BY ASSIGNMENT (``driver_id = user_id``),
        as the light rows a release decides on. A case its creator drives with
        the column NULL is not one: there is nothing to release.
        """
        ...

    # Standalone evidence operations (create/get/list/delete/link/update,
    # set/get primary) were removed in storage redesign 2026-04 phase 2.
    # Standalone evidence path is deleted; evidence is case-tied only and
    # accessed via `case.evidence` loaded by the case repository.


# ============================================================
# DTOs (Data Transfer Objects) for Cross-Module Use
# ============================================================


class CaseStateDTO(str, Enum):
    """Public case state enum for cross-module use.

    MUST mirror ``domain.models.lifecycle.CaseState``, which is the single authority on
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
    enterprise_id: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# Re-export domain models so other modules can import them from
# ``case.contracts`` instead of reaching into ``case.domain.models``
# directly (per the layer-boundary import-linter contract).
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.causal import (
    CausalEdge,
    CausalNode,
    InterventionQuadrant,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from faultmaven.modules.case.domain.models.conclusion import (
    CONFIRMED_ESTABLISHED_BY,
    ConfidenceLevel,
    RootCauseConclusion,
    WorkingConclusion,
    established_by_for_display,
    mechanism_for_display,
    normalize_stored_report_content,
)
from faultmaven.modules.case.domain.models.documentation import (
    DocumentationData,
    DocumentType,
    EscalationState,
    EscalationType,
    GeneratedDocument,
    JournalEntry,
)
from faultmaven.modules.case.domain.models.driver import (
    CaseDriverChange,
    CaseDriverChangeReason,
    DrivenCase,
)
from faultmaven.modules.case.domain.models.evidence import (
    CaseEntity,
    EntityType,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    UploadedFile,
    is_default_case_title,
    is_minted_filename,
    minted_filename_phrase,
)
from faultmaven.modules.case.domain.models.evidence_needs import (
    EvidenceNeed,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)
from faultmaven.modules.case.domain.models.hypothesis import (
    TERMINAL_HYPOTHESIS_STATES,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
)
from faultmaven.modules.case.domain.models.lifecycle import (
    CaseAction,
    CaseSeverity,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.domain.models.metadata import (
    CaseMetadata,
    CaseMetadataNotGrantedError,
    CaseMetadataRefusedError,
    CaseMetadataUnavailableError,
)
from faultmaven.modules.case.domain.models.problem import (
    InquiryData,
    InvestigationStage,
    KnowledgeMatch,
    KnowledgeResolution,
    PendingRevision,
    PreliminaryUrgency,
    ProblemInvalidation,
    ProblemStatementRecord,
    ProblemStatus,
    ProblemVerification,
    StagedCauseWork,
    StatementRecordKind,
    TemporalState,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.models.progress import (
    CauseAssuranceGrade,
    CauseState,
    InvestigationProgress,
    MitigationRecord,
    SolutionFeasible,
    SolutionState,
    VerificationStatus,
)
from faultmaven.modules.case.domain.models.solution import (
    ActionAttempt,
    InvestigationActionType,
    ProposedAction,
    Solution,
    SolutionOutcome,
    SolutionType,
    classify_solution_outcome,
)
from faultmaven.modules.case.domain.models.turn import (
    NON_INVESTIGATIVE_OUTCOMES,
    InvestigationMomentum,
    TerminalConfirmedVia,
    TurnOutcome,
    TurnProgress,
)

# ============================================================
# Cross-enterprise metadata read (ADR-012 D9)
# ============================================================


class ICaseMetadataReader(Protocol):
    """Every enterprise's cases as :class:`CaseMetadata` — never their content.

    PostgreSQL only, and needed only under ``TENANT_PROVIDER=multi``: there the
    web process's database role is scoped by row-level security to one
    enterprise, so an ordinary case query cannot answer "all tenants". The
    implementation reads through ``SECURITY DEFINER`` functions bounded twice:
    by a result type with no column sourced from a title, a description or
    other user text, and by ``EXECUTE`` granted to the runtime role only.
    """

    async def list_case_metadata(
        self,
        *,
        state: Optional[CaseState],
        source: Optional[str],
        limit: int,
        offset: int,
    ) -> Tuple[List[CaseMetadata], int]:
        """One page of cases across every enterprise, newest update first,
        and the number of matches in all enterprises (not the page length).

        Raises:
            CaseMetadataNotGrantedError: the connected role lacks EXECUTE.
            CaseMetadataRefusedError: the database refused the read for another
                reason (row-level security would have filtered it, a schema
                privilege is missing).
            CaseMetadataUnavailableError: the database function is missing
                (and the base class of the above).
        """
        ...


# ============================================================
# The case driver's ports (ADR-020)
# ============================================================


class ICaseDriverRelease(Protocol):
    """Hand cases back to their creators BEFORE an operation outside the case
    module takes a driver's read access away (ADR-020 D3).

    Implemented by the case service, called by the auth module's team and
    account services. Each method decides which of the account's driven cases
    would lose their driver's read access once the caller's own write lands,
    and releases exactly those — FIRST, in the same request, because the
    caller's write commits in a store this module cannot share a transaction
    with. Release-first fails safe: if the caller's write then fails or is
    refused, the case has handed back to its creator needlessly, with an
    audited reason, and the creator can reassign it again. Returns how many
    cases were released.
    """

    async def release_driver_before_team_leave(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> int:
        """``user_id`` is about to leave ``team_id``: release every case they
        drive that is shared with ``team_id`` and with no other team of
        theirs."""
        ...

    async def release_driver_before_deactivation(
        self, *, user_id: str, actor_user_id: Optional[str]
    ) -> int:
        """``user_id`` is about to be deactivated: release every case they
        drive."""
        ...


class CaseAccount(Protocol):
    """The account fields the case module reads: names for the case rows
    (ADR-020 D5) and the facts that make an account a driver candidate (D4).
    Never an email address."""

    user_id: str
    display_name: str
    is_active: bool
    account_kind: str


class ICaseAccountReader(Protocol):
    """The accounts among ``user_ids`` anchored to ``enterprise_id``, in one
    read; an id anchored elsewhere, or naming nobody, is absent. The user
    repository satisfies it (``get_many_in_enterprise``)."""

    async def get_many_in_enterprise(
        self, enterprise_id: str, user_ids: Sequence[str]
    ) -> List[CaseAccount]: ...


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    "EMPTY_AGENT_RESPONSE_TEXT",
    "EMPTY_TURN_TEXT",
    "MESSAGE_METADATA_AGENT_SYNTHESIZED",
    "MESSAGE_METADATA_KB_SOURCES",
    "MESSAGE_METADATA_USER_EMPTY",
    "MessageRowKind",
    "append_message_row",
    "is_server_written_assistant_row",
    "is_server_written_user_row",
    # Repository and Service Contracts
    "ICaseRepository",
    "ICaseMetadataReader",
    "ICaseDriverRelease",
    "ICaseAccountReader",
    "CaseAccount",
    "CaseMetadataUnavailableError",
    "CaseMetadataNotGrantedError",
    "CaseMetadataRefusedError",
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
    "PERSISTED_REPORT_TYPES",
    "ReportStatus",
    "RunbookSource",
    "RunbookMetadata",
    "CaseReport",
    "RunbookRef",
    "RunbookRecommendation",
    "ReportRecommendation",
    "ReportGenerationRequest",
    "ReportGenerationResponse",
    "CaseClosureRequest",
    "CaseClosureResponse",
    # Case-owned turn receipts (#1888)
    "TurnReceipt",
    "TurnReceiptExistsError",
    "TurnReceiptKey",
    # Case-owned Agent Execution models (per module-organization-design.md)
    # Investigation models from Agent module (shared for investigation coordination)
    # Case domain models
    "Case",
    "CaseAction",
    "CaseDriverChange",
    "CaseDriverChangeReason",
    "CaseMetadata",
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
    "DrivenCase",
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
    "PendingRevision",
    "ProblemInvalidation",
    "ProblemStatementRecord",
    "ProblemStatus",
    "ProblemVerification",
    "StagedCauseWork",
    "StatementRecordKind",
    "RootCauseConclusion",
    "Solution",
    "SolutionFeasible",
    "SolutionOutcome",
    "SolutionState",
    "SolutionType",
    "CONFIRMED_ESTABLISHED_BY",
    "classify_solution_outcome",
    "established_by_for_display",
    "mechanism_for_display",
    "normalize_stored_report_content",
    "MitigationRecord",
    "TemporalState",
    "TERMINAL_HYPOTHESIS_STATES",
    "NON_INVESTIGATIVE_OUTCOMES",
    "is_default_case_title",
    "TerminalConfirmedVia",
    "TurnOutcome",
    "TurnProgress",
    "UploadedFile",
    "UrgencyLevel",
    "VerificationStatus",
    "WorkingConclusion",
]
