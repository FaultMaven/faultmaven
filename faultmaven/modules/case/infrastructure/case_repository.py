"""Case Repository for milestone-based investigation persistence.

This module provides the repository pattern for Case domain model persistence.
It abstracts database operations and provides clean interfaces for the service layer.
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseEntity,
    CaseState,
    DocumentationData,
    EntityType,
    EscalationState,
    Evidence,
    Hypothesis,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
)

if TYPE_CHECKING:
    # Report models now owned by Case module - import from case domain models
    from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportType,
    )


# ============================================================
# Repository Interface
# ============================================================

logger = logging.getLogger(__name__)


class CaseRepository(ABC):
    """
    Abstract repository interface for Case persistence.

    Implementations:
    - SQLiteCaseRepository: Local deployment (SQLite)
    - PostgreSQLHybridCaseRepository: Cloud deployment (PostgreSQL)
    - InMemoryCaseRepository: Testing and development
    """

    @abstractmethod
    async def save(self, case: Case) -> Case:
        """
        Save case to persistence layer.

        Args:
            case: Case domain object

        Returns:
            Saved case (may have updated timestamps)

        Raises:
            RepositoryException: If save fails
        """
        pass

    @abstractmethod
    async def get(self, case_id: str) -> Optional[Case]:
        """
        Retrieve case by ID.

        Args:
            case_id: Case identifier

        Returns:
            Case if found, None otherwise

        Raises:
            RepositoryException: If retrieval fails
        """
        pass

    @abstractmethod
    async def list_all_case_ids(self) -> List[str]:
        """
        Return the id of every case row, regardless of state or owner.

        The reference set for the orphaned-collection sweep (case_cleanup):
        a ChromaDB case collection is orphaned only when NO case row exists
        for it at all — terminal/archived cases still count, so the sweep
        never deletes vector data for a case that merely closed. Deliberately
        unfiltered; under the multi-tenant provider a complete (cross-tenant)
        result requires the maintenance DB role, which the jobs runner
        enforces (an RLS-scoped role returns a partial set).

        Returns:
            List of case ids (unordered).

        Raises:
            RepositoryException: If the query fails
        """
        pass

    @abstractmethod
    async def list_all_storage_refs(self) -> Set[str]:
        """
        Return every non-null ``uploaded_files.storage_ref`` value.

        The AUTHORITY the orphan-file sweep (storage_cleanup) consults before
        deleting a stored object: an object named by any row here is live,
        whatever its storage sidecar happens to say. The sidecar is a cache of
        this state written once at upload and never revisited, so a sweep that
        decides from the sidecar alone deletes files a case still references
        whenever the cache is stale in that direction (issue #1232).

        Deliberately unfiltered by case state or owner — a terminal case still
        references its evidence. Under the multi-tenant provider a complete
        (cross-tenant) result requires the maintenance DB role, which the jobs
        runner enforces for cross_tenant jobs: ``uploaded_files`` is RLS-tenanted
        and fail-closed, so an unbound app-role session returns ZERO rows, which
        for a delete-deciding sweep reads as "nothing is referenced".

        Returned as a set because the only supported use is membership testing
        against a sweep candidate; ``storage_ref`` carries no index, so callers
        must not turn this into N point lookups.

        Returns:
            Set of storage refs (nulls dropped).

        Raises:
            RepositoryException: If the query fails
        """
        pass

    @abstractmethod
    async def list(
        self,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        include_empty: bool = True,
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters.

        Args:
            user_id: Filter by user
            enterprise_id: Retained for interface symmetry; does NOT scope
                reads (single-tenant standalone; multi-tenant isolation is
                PostgreSQL RLS keyed on the enterprise, ADR-010/ADR-017)
            state: Filter by state
            limit: Maximum results
            offset: Pagination offset
            include_empty: When False, exclude empty cases (current_turn == 0)
                via the query WHERE clause so the predicate constrains both the
                returned page and the total count.
            shared_case_ids: Case ids the requester can read via a team share
                (ADR-013 §D4). Widens the owner-only scope to
                ``owned ∪ shared-to-my-teams``; empty leaves owner-only.
            restrict_case_ids: Filter-by-team facet — narrows the result to one
                team's shared case ids (the caller resolves/authorizes the team).
                ``None`` = no facet; a non-``None`` empty list matches nothing.

        Returns:
            Tuple of (cases, total_count)

        Raises:
            RepositoryException: If query fails
        """
        pass

    @abstractmethod
    async def count_user_cases_on_date(self, user_id: str, date: Any) -> int:
        """
        Count cases created by a user on a specific date.

        Args:
            user_id: User identifier
            date: Date to count cases for (date object or YYYY-MM-DD string)

        Returns:
            Number of cases created on that date

        Raises:
            RepositoryException: If query fails
        """
        pass

    @abstractmethod
    async def delete(self, case_id: str) -> bool:
        """
        Delete case by ID.

        Args:
            case_id: Case identifier

        Returns:
            True if deleted, False if not found

        Raises:
            RepositoryException: If deletion fails
        """
        pass

    @abstractmethod
    async def find_uploaded_file_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional[UploadedFile]:
        """
        Return the oldest UploadedFile in this case whose content_hash matches.

        Post-010 strict evidence model: file uploads create only an
        UploadedFile row (no auto-Evidence at intake), so dedup is a
        file-level concern. An attachment whose SHA-256 content hash
        already exists on the same case returns the existing
        UploadedFile instead of creating a new row.

        Args:
            case_id: Case to search within (scope is per-case, not global).
            content_hash: SHA-256 hex of UTF-8 text (as produced by
                PreprocessingService.classify_and_extract).

        Returns:
            The oldest matching UploadedFile (by upload timestamp) if
            found, None otherwise. NULL content_hash rows are never
            matched.

        Raises:
            RepositoryException: If lookup fails
        """
        pass

    @abstractmethod
    async def upsert_case_entities(
        self,
        case_id: str,
        evidence_id: str,
        entities: List[CaseEntity],
    ) -> None:
        """Replace this evidence's entity rows with *entities*.

        Phase 4 — invoked by the preprocessing pipeline after each
        extraction. Semantics: delete all existing
        ``case_entities`` rows for ``(case_id, evidence_id)``, then
        insert the new list. This "replace per evidence" contract keeps
        re-extraction (Phase 1.5) and alt-extractor retries (Phase 2)
        idempotent — stale entity observations from a previous
        extraction don't linger.

        When ``entities`` is empty, the delete fires but nothing is
        inserted — correct for timeless evidence or evidence where
        no entity-bearing content was present.

        Args:
            case_id: Case that owns the evidence.
            evidence_id: Evidence being (re-)indexed.
            entities: New row set. Every entry's ``case_id`` and
                ``evidence_id`` must match the method arguments;
                implementations may trust the caller.

        Raises:
            RepositoryException: If the write fails.
        """
        pass

    @abstractmethod
    async def find_entity(
        self,
        case_id: str,
        entity_value: str,
        entity_type: Optional[EntityType] = None,
    ) -> List[CaseEntity]:
        """Return registry rows matching ``entity_value`` in the case.

        Phase 4 — primary case-level lookup. Exact-value match on
        ``entity_value`` (case-sensitive). When ``entity_type`` is
        provided, restricts to that type; else returns hits across all
        types (an IP and a hostname with the same string, for example,
        would both surface — rare but possible).

        Returns:
            Zero or more ``CaseEntity`` rows, ordered by
            ``mention_count`` descending so the most-mentioned
            instances come first.

        Raises:
            RepositoryException: If the query fails.
        """
        pass

    @abstractmethod
    async def list_top_entities(
        self,
        case_id: str,
        entity_type: EntityType,
        limit: int = 10,
    ) -> List[CaseEntity]:
        """Return the top-N entities of a given type in the case.

        Phase 4 — aggregation query used by the context builder's
        entity-highlights block and by ops dashboards. Aggregation
        key is ``entity_value``; rows are combined across evidence by
        summing ``mention_count``. The rows returned are representative
        samples (one per aggregated entity) sorted by total mentions
        desc.

        Args:
            case_id: Case to query within.
            entity_type: Required — aggregating across types is
                semantically unclear (an IP's count plus a PID's count
                is meaningless). Callers that want "all entities"
                should iterate the enum and combine client-side.
            limit: Max number of distinct entities to return.

        Returns:
            List of ``CaseEntity`` rows, one per distinct
            ``entity_value``, ordered by aggregated mention count
            descending.

        Raises:
            RepositoryException: If the query fails.
        """
        pass

    @abstractmethod
    async def list_evidence_by_time_window(
        self,
        case_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Evidence]:
        """
        Return evidence whose coverage overlaps ``[start, end]``.

        Phase 3 — case-level timeline query. Overlap semantics:
        an evidence row overlaps the window when
        ``coverage_start_ts <= end AND coverage_end_ts >= start``.
        Evidence with NULL coverage timestamps (timeless content —
        configs, code, screenshots, short pastes) is excluded from
        results; the caller who wants all evidence regardless of
        time should use ``get(case_id).evidence`` instead.

        Both bounds are optional:
        - ``start=None, end=None`` → all evidence in the case with
          non-NULL coverage, ordered by coverage_start_ts ascending.
        - ``start=None`` → evidence ending before ``end``.
        - ``end=None`` → evidence starting on or after ``start``.

        Args:
            case_id: Case to search within.
            start: Lower bound of the time window (inclusive). None
                means no lower bound.
            end: Upper bound of the time window (inclusive). None
                means no upper bound.

        Returns:
            Evidence rows ordered by coverage_start_ts ascending.
            Empty list when no evidence matches or the case doesn't
            exist.

        Raises:
            RepositoryException: If lookup fails
        """
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        limit: int = 20,
        shared_case_ids: Optional[List[str]] = None,
    ) -> tuple[List[Case], int]:
        """
        Search cases by text query.

        Args:
            query: Search query
            user_id: Filter by user
            enterprise_id: Retained for interface symmetry; does NOT scope
                reads (single-tenant standalone; multi-tenant isolation is
                PostgreSQL RLS keyed on the enterprise, ADR-010/ADR-017)
            limit: Maximum results
            shared_case_ids: Case ids the requester can read via a team share
                (ADR-013 §D4). Widens the owner-only scope to
                ``owned ∪ shared-to-my-teams``; empty leaves owner-only.

        Returns:
            Tuple of (cases, total_count)

        Raises:
            RepositoryException: If search fails
        """
        pass

    @abstractmethod
    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """
        Add a message to a case.

        Implementation note: Storage backends may handle this differently:
        - Redis: Store messages separately in a list
        - PostgreSQL: Messages stored as JSONB array in case record
        - In-Memory: Messages stored in Case.messages list

        Args:
            case_id: Case identifier
            message_dict: Message data as dictionary

        Returns:
            True if message was added successfully

        Raises:
            RepositoryException: If add fails
        """
        pass

    @abstractmethod
    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """
        Get messages for a case with pagination.

        Args:
            case_id: Case identifier
            limit: Maximum messages to return
            offset: Pagination offset

        Returns:
            List of message dictionaries

        Raises:
            RepositoryException: If retrieval fails
        """
        pass

    @abstractmethod
    async def update_activity_timestamp(self, case_id: str) -> bool:
        """
        Update case last_activity_at timestamp.

        Implementation note: Efficient implementations should update
        only the timestamp field, not reload the entire case.

        Args:
            case_id: Case identifier

        Returns:
            True if updated successfully

        Raises:
            RepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """
        Scoped UPDATE of cosmetic metadata fields — does NOT bump version.

        ``title`` and ``description`` are user-facing labels, not part of
        the investigation state machine. Writing them through the
        versioned ``save(case)`` path would stale-conflict any concurrent
        turn save (an in-flight LLM tool loop can hold the case in memory
        for tens of seconds).

        Pass only the fields to change; ``None`` means "leave as-is".

        Status, closure_reason, and other investigation-state fields
        still go through ``save`` so OCC protects them.

        Returns:
            True if a row was updated.

        Raises:
            RepositoryException: If the update fails.
        """
        pass

    @abstractmethod
    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """
        Update the `vectorized` flag on a single evidence row.

        Scoped single-field update — does NOT rewrite the case aggregate.
        Safe to call from background tasks holding a stale Case snapshot,
        since it touches only the one column on the one row.

        Args:
            case_id: Case the evidence belongs to (used for authorisation/scoping).
            evidence_id: Evidence row to update.
            vectorized: New value for the flag.

        Returns:
            True if a row was updated, False if no such evidence existed.

        Raises:
            RepositoryException: If the update fails.
        """
        pass

    @abstractmethod
    async def delete_evidence(self, case_id: str, evidence_id: str) -> bool:
        """
        Delete a single evidence row.

        The aggregate `save(case)` does NOT remove these rows — its upserts are
        purely additive — so targeted removal must be explicit. This is that
        path (deleting the whole case also removes them, via ON DELETE CASCADE)
        via `_upsert_evidence`. Prefer this method for intentional removals —
        it states intent clearly and does not require a case-aggregate save.

        Args:
            case_id: Case the evidence belongs to.
            evidence_id: Evidence row to remove.

        Returns:
            True if a row was removed, False if no such evidence existed.

        Raises:
            RepositoryException: If the delete fails.
        """
        pass

    @abstractmethod
    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """
        Delete a single uploaded_file row.

        The aggregate `save(case)` does NOT remove these rows — its upserts are
        purely additive — so targeted removal must be explicit. This is that
        path (deleting the whole case also removes them, via ON DELETE CASCADE)
        via `_upsert_uploaded_files`. Prefer this method for intentional removals.

        Args:
            case_id: Case the file belongs to.
            file_id: Uploaded file row to remove.

        Returns:
            True if a row was removed, False if no such file existed.

        Raises:
            RepositoryException: If the delete fails.
        """
        pass

    @abstractmethod
    async def add_uploaded_file(
        self,
        case_id: str,
        uploaded_file: UploadedFile,
        enterprise_id: str,
        organization_id: Optional[str] = None,
    ) -> None:
        """
        Commit ONE uploaded_file row on its own, outside the aggregate save.

        An upload is a user-initiated fact whose durability must not depend on
        the rest of the turn succeeding. The bytes are already in storage when
        this is called; committing the row here keeps the two consistent. When
        the row rode along on the end-of-turn `save(case)` instead, a turn that
        raised left the bytes stored and unreferenced, and the retry stored a
        second copy — `find_uploaded_file_by_content_hash` cannot dedup against
        a row that was never written.

        Scoped rather than `save(case)` because the aggregate save commits the
        WHOLE case: mid-turn that makes the half-built turn durable (the user
        message appended at step 2, the bumped `current_turn`), which is exactly
        what deferring the save exists to avoid. This commits the upload without
        committing the turn.

        Ordering with the later aggregate save is safe because
        `_upsert_uploaded_files` is purely additive — it re-upserts this row
        rather than deleting it. That is NOT true of `causal_nodes`/`causal_edges`,
        which the aggregate save reconciles destructively; do not generalise this
        method's safety to those tables.

        Args:
            case_id: Case the file belongs to.
            uploaded_file: The row to commit.
            enterprise_id: Tenant that owns the row (the RLS key).
            organization_id: Billing attribution, or ``None`` when nobody pays
                for the account that owns the case.

        Raises:
            RepositoryException: If the write fails.
        """
        pass

    @abstractmethod
    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """
        Compute analytics for a case.

        Implementation note: Can compute on-the-fly or from cached data.

        Args:
            case_id: Case identifier

        Returns:
            Dictionary with analytics data

        Raises:
            RepositoryException: If computation fails
        """
        pass

    @abstractmethod
    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """
        Clean up expired/old cases.

        Implementation note: Different backends may use different strategies:
        - Redis: Use TTL
        - PostgreSQL: Query by closed_at date
        - In-Memory: Iterate and filter

        Args:
            max_age_days: Maximum age in days for closed cases
            batch_size: Maximum cases to process in one batch

        Returns:
            Number of cases deleted

        Raises:
            RepositoryException: If cleanup fails
        """
        pass

    @abstractmethod
    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """
        Save report to persistence layer.

        Args:
            report: CaseReport domain object

        Returns:
            Saved report (may have updated timestamps)

        Raises:
            RepositoryException: If save fails
        """
        pass

    @abstractmethod
    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """
        Retrieve a report by ID.

        Args:
            report_id: Report identifier (UUID)

        Returns:
            CaseReport if found, None otherwise

        Raises:
            RepositoryException: If retrieval fails
        """
        pass

    @abstractmethod
    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List["CaseReport"]:
        """
        Get reports for a case with optional filtering.

        Args:
            case_id: Case identifier
            report_type: Optional filter by report type
            include_history: If True, return all versions; if False, only current
            only_current: If True, return only is_current=True reports

        Returns:
            List of CaseReport objects

        Raises:
            RepositoryException: If query fails
        """
        pass

    @abstractmethod
    async def count_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
    ) -> int:
        """
        Count persisted reports for a case, optionally filtered by type.

        Counts ALL rows (every regeneration adds a new row), not just the
        current one — this is the metric the regeneration cap enforces.

        Args:
            case_id: Case identifier
            report_type: Optional filter by report type

        Returns:
            Total number of report rows matching the filter

        Raises:
            RepositoryException: If query fails
        """
        pass

    @abstractmethod
    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """
        Update an existing report.

        Args:
            report: CaseReport with updated fields

        Returns:
            Updated report

        Raises:
            RepositoryException: If update fails
        """
        pass

    @abstractmethod
    async def delete_report(self, report_id: str) -> bool:
        """
        Delete a report by ID.

        Args:
            report_id: Report identifier (UUID)

        Returns:
            True if deleted, False if not found

        Raises:
            RepositoryException: If deletion fails
        """
        pass

    # Checkpoint Operations
    @abstractmethod
    async def create_checkpoint(self, checkpoint: "CaseCheckpoint") -> "CaseCheckpoint":
        """
        Create a new case checkpoint.

        Args:
            checkpoint: CaseCheckpoint domain object

        Returns:
            Created checkpoint

        Raises:
            RepositoryException: If creation fails
        """
        pass

    @abstractmethod
    async def get_checkpoint(self, checkpoint_id: str) -> Optional["CaseCheckpoint"]:
        """
        Get a checkpoint by ID.

        Args:
            checkpoint_id: Checkpoint identifier

        Returns:
            CaseCheckpoint if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_checkpoints(self, case_id: str) -> List["CaseCheckpoint"]:
        """
        Get all checkpoints for a case.

        Args:
            case_id: Case identifier

        Returns:
            List of checkpoints ordered by turn_number
        """
        pass

    async def begin_transaction(self):
        """
        Begin a transaction context (optional feature).

        Default implementation is a no-op context manager.
        Databases that support transactions can override this.

        Returns:
            Context manager for transaction
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_transaction():
            yield

        return noop_transaction()


# ============================================================
# In-Memory Implementation (for Testing)
# ============================================================


class InMemoryCaseRepository(CaseRepository):
    """
    In-memory case repository for testing and development.

    Data stored in dictionary, not persistent across restarts.
    """

    def __init__(self):
        """Initialize empty in-memory store."""
        self._cases: Dict[str, Case] = {}
        self._reports: Dict[str, "CaseReport"] = {}  # report_id -> CaseReport
        self._checkpoints: Dict[str, Any] = {}  # checkpoint_id -> CaseCheckpoint
        # Phase 4 — case entity registry. Keyed by (case_id, evidence_id)
        # for O(1) delete-before-insert on re-extraction, with the
        # composite (case, type, value, evidence) tuple preserved
        # inside each row.
        self._case_entities: Dict[tuple[str, str], List[CaseEntity]] = {}

    async def save(self, case: Case) -> Case:
        """Save case to memory with optimistic concurrency control.

        Upholds the same OCC contract as the SQL-backed repositories so
        unit tests using the in-memory implementation actually exercise
        the concurrency guarantees the rest of the code depends on.

        - New case (no prior write): stored with version = 1.
        - Existing case, `case.version` matches the stored version:
          version is bumped and the case is stored.
        - Existing case, version mismatch: raises StaleCaseException.
        """
        from faultmaven.core.investigation.terminal_transitions import (
            derive_disposition_eligibility,
        )
        from faultmaven.modules.case.exceptions import StaleCaseException

        # Update timestamp
        case.updated_at = datetime.now(case.updated_at.tzinfo)

        # Self-heal any turn-sequence anomaly before storing, matching the
        # SQL-backed repositories so the in-memory path can't wedge or drift.
        case.reconcile_turn_sequence()

        # P3 chokepoint: refresh denormalized disposition_eligibility from
        # current case content. Same site as the SQL-backed repositories
        # so all tests using the in-memory repo also exercise the
        # eligibility-maintenance invariant.
        case.disposition_eligibility = derive_disposition_eligibility(case)

        existing = self._cases.get(case.case_id)
        if existing is None:
            # New case
            case.version = 1
        else:
            if existing.version != case.version:
                raise StaleCaseException(
                    case_id=case.case_id,
                    expected_version=case.version,
                    actual_version=existing.version,
                )
            case.version = case.version + 1

        self._cases[case.case_id] = case
        return case

    async def get(self, case_id: str) -> Optional[Case]:
        """Get case from memory.

        Returns the stored object by reference (no deserialization round-trip),
        so we do NOT reconcile here — that would mutate the shared in-flight
        object on a read. save() already heals before storing, and the in-memory
        store is ephemeral (no pre-fix wedged cases to repair on load), so the
        stored object is always consecutive.
        """
        return self._cases.get(case_id)

    async def list_all_case_ids(self) -> List[str]:
        """Every stored case id, regardless of state (see CaseRepository)."""
        return list(self._cases.keys())

    async def list_all_storage_refs(self) -> Set[str]:
        """Every stored file's storage_ref, any case (see CaseRepository)."""
        return {
            uploaded_file.storage_ref
            for case in self._cases.values()
            for uploaded_file in (getattr(case, "uploaded_files", None) or [])
            if getattr(uploaded_file, "storage_ref", None)
        }

    async def list(
        self,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        include_empty: bool = True,
    ) -> tuple[List[Case], int]:
        """List cases with filters."""
        # Filter cases
        filtered = list(self._cases.values())

        if user_id:
            # owned ∪ shared-to-my-teams (ADR-013 §D4). Empty shared set leaves
            # the owner-only filter unchanged (behavior-neutral until U10).
            shared = set(shared_case_ids or ())
            filtered = [
                c for c in filtered if c.user_id == user_id or c.case_id in shared
            ]

        # Filter-by-team facet: narrow to an explicit case-id allowlist (one
        # team's shares). None = no facet; empty = match nothing (mirrors SQL).
        if restrict_case_ids is not None:
            restrict = set(restrict_case_ids)
            filtered = [c for c in filtered if c.case_id in restrict]

        # No tenant filter: standalone is single-tenant; multi-tenant isolation
        # is PostgreSQL RLS keyed on the enterprise (ADR-010/ADR-017). The
        # enterprise_id param is retained for interface symmetry.

        if state:
            filtered = [c for c in filtered if c.state == state]

        # Exclude empty cases (current_turn == 0) when requested, BEFORE the
        # total count is computed so the count and the returned page agree
        # (mirrors the SQL WHERE-clause predicate in the DB repositories).
        if not include_empty:
            filtered = [c for c in filtered if c.current_turn > 0]

        # Sort by last_activity_at descending
        filtered.sort(key=lambda c: c.last_activity_at, reverse=True)

        total_count = len(filtered)

        # Paginate
        paginated = filtered[offset : offset + limit]

        return paginated, total_count

    async def count_user_cases_on_date(self, user_id: str, date: Any) -> int:
        """Count cases for a user on a specific date in memory."""
        from datetime import date as date_type
        from datetime import datetime

        target_date = date
        if isinstance(target_date, str):
            try:
                target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
            except ValueError:
                # Handle ISO format if needed, or fallback
                pass

        count = 0
        for case in self._cases.values():
            if case.user_id != user_id:
                continue

            # Ensure we compare dates properly
            case_date = (
                case.created_at.date()
                if isinstance(case.created_at, datetime)
                else case.created_at
            )

            if case_date == target_date:
                count += 1

        return count

    async def delete(self, case_id: str) -> bool:
        """Delete case from memory."""
        if case_id in self._cases:
            del self._cases[case_id]
            return True
        return False

    async def find_uploaded_file_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional[UploadedFile]:
        """Find oldest UploadedFile in a case whose ``content_hash``
        matches. Post-010: dedup is a file-level concern (no Evidence
        rows at intake).
        """
        if not content_hash:
            return None
        case = self._cases.get(case_id)
        if case is None:
            return None
        matches = [uf for uf in case.uploaded_files if uf.content_hash == content_hash]
        if not matches:
            return None
        matches.sort(key=lambda uf: getattr(uf, "uploaded_at_turn", 0) or 0)
        return matches[0]

    async def list_evidence_by_time_window(
        self,
        case_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Evidence]:
        """In-memory impl of Phase 3b timeline query.

        Filters the case's evidence list in Python — same overlap
        semantics as the SQL implementations. Unsuitable for production
        volume but perfectly fine for tests that don't need a real DB.
        """
        case = self._cases.get(case_id)
        if case is None:
            return []
        matches: List[Evidence] = []
        for ev in case.evidence or []:
            ev_start = getattr(ev, "coverage_start_ts", None)
            ev_end = getattr(ev, "coverage_end_ts", None)
            if ev_start is None or ev_end is None:
                continue
            if end is not None and ev_start > end:
                continue
            if start is not None and ev_end < start:
                continue
            matches.append(ev)
        matches.sort(key=lambda ev: ev.coverage_start_ts)
        return matches

    async def upsert_case_entities(
        self,
        case_id: str,
        evidence_id: str,
        entities: List[CaseEntity],
    ) -> None:
        """Replace this evidence's entity rows — Phase 4 in-memory impl."""
        key = (case_id, evidence_id)
        if entities:
            self._case_entities[key] = list(entities)
        else:
            # Clear rather than store an empty list so list queries
            # don't see a residual entry.
            self._case_entities.pop(key, None)

    async def find_entity(
        self,
        case_id: str,
        entity_value: str,
        entity_type: Optional[EntityType] = None,
    ) -> List[CaseEntity]:
        """Phase 4 in-memory find — scans the in-memory dict."""
        matches: List[CaseEntity] = []
        for (cid, _), rows in self._case_entities.items():
            if cid != case_id:
                continue
            for row in rows:
                if row.entity_value != entity_value:
                    continue
                if entity_type is not None and row.entity_type != entity_type:
                    continue
                matches.append(row)
        # Highest mention_count first — matches SQL implementations.
        matches.sort(key=lambda r: r.mention_count, reverse=True)
        return matches

    async def list_top_entities(
        self,
        case_id: str,
        entity_type: EntityType,
        limit: int = 10,
    ) -> List[CaseEntity]:
        """Phase 4 in-memory aggregation.

        Sums mention_count across evidence per entity_value, returns
        one representative row per value (the one with the highest
        individual count) carrying the aggregated total in
        ``mention_count`` so the contract matches the SQL version.
        """
        aggregated: Dict[str, CaseEntity] = {}
        totals: Dict[str, int] = {}
        for (cid, _), rows in self._case_entities.items():
            if cid != case_id:
                continue
            for row in rows:
                if row.entity_type != entity_type:
                    continue
                totals[row.entity_value] = (
                    totals.get(row.entity_value, 0) + row.mention_count
                )
                existing = aggregated.get(row.entity_value)
                if existing is None or row.mention_count > existing.mention_count:
                    aggregated[row.entity_value] = row

        # Re-stamp each representative with its aggregated total.
        result = [
            r.model_copy(update={"mention_count": totals[r.entity_value]})
            for r in aggregated.values()
        ]
        result.sort(key=lambda r: r.mention_count, reverse=True)
        return result[:limit]

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        limit: int = 20,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
    ) -> tuple[List[Case], int]:
        """Search cases by text query (simple substring match)."""
        query_lower = query.lower()
        shared = set(shared_case_ids or ())
        restrict = None if restrict_case_ids is None else set(restrict_case_ids)

        # Filter cases
        filtered = []
        for case in self._cases.values():
            # Search in title and description
            if (
                query_lower in case.title.lower()
                or query_lower in case.description.lower()
                or query_lower in case.case_id.lower()
            ):

                # Apply user filter: owned ∪ shared-to-my-teams (ADR-013 §D4).
                # Empty shared set leaves owner-only (behavior-neutral until U10).
                if user_id and case.user_id != user_id and case.case_id not in shared:
                    continue

                # Filter-by-team facet: narrow to one team's shares.
                if restrict is not None and case.case_id not in restrict:
                    continue

                # No tenant filter: single-tenant standalone; multi-tenant
                # isolation is PostgreSQL RLS keyed on the enterprise
                # (ADR-010/ADR-017). enterprise_id param retained for symmetry.

                filtered.append(case)

        # Sort by relevance (simple: contains in title > contains in description)
        def relevance_score(case: Case) -> int:
            score = 0
            if query_lower in case.title.lower():
                score += 100
            if query_lower in case.description.lower():
                score += 10
            return score

        filtered.sort(key=relevance_score, reverse=True)

        total_count = len(filtered)

        # Limit results
        limited = filtered[:limit]

        return limited, total_count

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add message to case in memory."""
        from datetime import timezone

        case = self._cases.get(case_id)
        if not case:
            return False

        case.messages.append(message_dict)
        case.message_count += 1
        case.last_activity_at = datetime.now(timezone.utc)
        return True

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """Get messages from case in memory."""
        case = self._cases.get(case_id)
        if not case:
            return []

        return case.messages[offset : offset + limit]

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update last activity timestamp in memory."""
        from datetime import timezone

        case = self._cases.get(case_id)
        if not case:
            return False

        case.last_activity_at = datetime.now(timezone.utc)
        return True

    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """In-memory scoped update of title/description (no version bump)."""
        case = self._cases.get(case_id)
        if not case:
            return False
        if title is None and description is None:
            return False
        if title is not None:
            case.title = title
        if description is not None:
            case.description = description
        case.updated_at = datetime.now(timezone.utc)
        return True

    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """Flip `vectorized` on a single evidence row in memory."""
        case = self._cases.get(case_id)
        if not case:
            return False
        for ev in case.evidence or []:
            if getattr(ev, "evidence_id", None) == evidence_id:
                ev.vectorized = vectorized
                return True
        return False

    async def delete_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Remove a single evidence row in memory."""
        case = self._cases.get(case_id)
        if not case or not case.evidence:
            return False
        before = len(case.evidence)
        case.evidence = [
            ev
            for ev in case.evidence
            if getattr(ev, "evidence_id", None) != evidence_id
        ]
        return len(case.evidence) < before

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Remove a single uploaded_file row in memory."""
        case = self._cases.get(case_id)
        if not case or not case.uploaded_files:
            return False
        before = len(case.uploaded_files)
        case.uploaded_files = [
            f for f in case.uploaded_files if getattr(f, "file_id", None) != file_id
        ]
        return len(case.uploaded_files) < before

    async def add_uploaded_file(
        self,
        case_id: str,
        uploaded_file: UploadedFile,
        enterprise_id: str,
        organization_id: Optional[str] = None,
    ) -> None:
        """Commit one uploaded_file row in memory (idempotent by file_id)."""
        case = self._cases.get(case_id)
        if not case:
            # Do NOT no-op. The SQL implementations fail on the FK, and the
            # contract documents RepositoryException — a silent return would
            # let a wrong case_id certify green in every in-memory-backed test.
            raise RepositoryException(
                f"Cannot add uploaded_file to unknown case {case_id}"
            )
        if case.uploaded_files is None:
            case.uploaded_files = []
        file_id = getattr(uploaded_file, "file_id", None)
        for i, existing in enumerate(case.uploaded_files):
            if getattr(existing, "file_id", None) == file_id:
                case.uploaded_files[i] = uploaded_file
                return
        case.uploaded_files.append(uploaded_file)

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for case in memory."""
        from faultmaven.utils.serialization import to_json_compatible

        case = self._cases.get(case_id)
        if not case:
            return {}

        analytics = {
            "case_id": case.case_id,
            "state": case.state.value,
            "created_at": to_json_compatible(case.created_at),
            "last_activity_at": to_json_compatible(case.last_activity_at),
            "message_count": case.message_count,
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "evidence_count": len(case.evidence),
            "hypothesis_count": len(case.hypotheses),
            "solution_count": len(case.solutions),
            "investigation_strategy": case.investigation_strategy.value,
            "has_working_conclusion": case.working_conclusion is not None,
            "has_root_cause": case.root_cause_conclusion is not None,
            "is_escalated": case.escalation_state is not None,
        }

        if case.resolved_at:
            analytics["resolved_at"] = to_json_compatible(case.resolved_at)
            duration = (case.resolved_at - case.created_at).total_seconds()
            analytics["resolution_time_seconds"] = duration

        return analytics

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired cases from memory."""
        from datetime import timedelta, timezone

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        deleted_count = 0

        # Collect case IDs to delete (avoid modifying dict during iteration)
        to_delete = []
        for case_id, case in self._cases.items():
            if (
                case.state == CaseState.CLOSED
                and case.closed_at
                and case.closed_at < cutoff_date
            ):
                to_delete.append(case_id)
                if len(to_delete) >= batch_size:
                    break

        # Delete collected cases
        for case_id in to_delete:
            del self._cases[case_id]
            deleted_count += 1

        return deleted_count

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report to memory."""
        from datetime import timezone

        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportType,
        )
        from faultmaven.utils.serialization import to_json_compatible

        # If this is marked as current, unmark other reports of the same type for this case
        if report.is_current:
            for existing_report in self._reports.values():
                if (
                    existing_report.case_id == report.case_id
                    and existing_report.report_type == report.report_type
                    and existing_report.report_id != report.report_id
                    and existing_report.is_current
                ):
                    existing_report.is_current = False

        # Set updated_at if not already set (for new reports, same as generated_at)
        if report.updated_at is None:
            report.updated_at = report.generated_at

        self._reports[report.report_id] = report

        return report

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Get report from memory."""
        return self._reports.get(report_id)

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List["CaseReport"]:
        """Get reports for a case with optional filtering."""
        from faultmaven.modules.case.domain.owned_models.report import ReportType

        # Filter by case_id
        reports = [r for r in self._reports.values() if r.case_id == case_id]

        # Filter by report_type if specified
        if report_type:
            reports = [r for r in reports if r.report_type == report_type]

        # Filter by is_current if only_current is True
        if only_current:
            reports = [r for r in reports if r.is_current]
        elif not include_history:
            # If include_history is False, default to only current
            reports = [r for r in reports if r.is_current]

        # Sort by version descending
        reports.sort(key=lambda r: (r.report_type.value, r.version), reverse=True)

        return reports

    async def count_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
    ) -> int:
        """Count persisted reports for a case (all versions)."""
        reports = [r for r in self._reports.values() if r.case_id == case_id]
        if report_type:
            reports = [r for r in reports if r.report_type == report_type]
        return len(reports)

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update report in memory."""
        from datetime import timezone

        from faultmaven.utils.serialization import to_json_compatible

        if report.report_id not in self._reports:
            raise RepositoryException(f"Report {report.report_id} not found")

        # If this is marked as current, unmark other reports of the same type for this case
        if report.is_current:
            for existing_report in self._reports.values():
                if (
                    existing_report.case_id == report.case_id
                    and existing_report.report_type == report.report_type
                    and existing_report.report_id != report.report_id
                    and existing_report.is_current
                ):
                    existing_report.is_current = False

        # Always update updated_at timestamp to reflect the modification
        report.updated_at = to_json_compatible(datetime.now(timezone.utc))

        self._reports[report.report_id] = report

        return report

    async def delete_report(self, report_id: str) -> bool:
        """Delete report from memory."""
        if report_id in self._reports:
            del self._reports[report_id]
            return True
        return False

    async def create_checkpoint(self, checkpoint: "CaseCheckpoint") -> "CaseCheckpoint":
        """Create checkpoint in memory."""
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    async def get_checkpoint(self, checkpoint_id: str) -> Optional["CaseCheckpoint"]:
        """Get checkpoint from memory."""
        return self._checkpoints.get(checkpoint_id)

    async def get_checkpoints(self, case_id: str) -> List["CaseCheckpoint"]:
        """Get checkpoints for case from memory."""
        checkpoints = [cp for cp in self._checkpoints.values() if cp.case_id == case_id]
        # Sort by turn number
        checkpoints.sort(key=lambda x: x.turn_number)
        return checkpoints

    # ============================================================
    def clear(self):
        """Clear all cases and reports (testing utility)."""
        self._cases.clear()
        self._reports.clear()


# NOTE: Legacy PostgreSQLCaseRepository was removed (2026-03-18).
# It used a single-table JSONB schema incompatible with the current normalized schema.
# Active implementations: SQLiteCaseRepository, PostgreSQLHybridCaseRepository


# ============================================================
# Repository Exception
# ============================================================


class RepositoryException(Exception):
    """Base exception for repository errors."""

    pass
