"""Case Repository for milestone-based investigation persistence.

This module provides the repository pattern for Case domain model persistence.
It abstracts database operations and provides clean interfaces for the service layer.
"""

import json
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseStatus,
    DocumentationData,
    EscalationState,
    Evidence,
    Hypothesis,
    InquiryData,
    InvestigationProgress,
    PathSelection,
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
    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters.

        Args:
            user_id: Filter by user
            organization_id: Filter by organization
            status: Filter by status
            limit: Maximum results
            offset: Pagination offset

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
    async def find_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional[Evidence]:
        """
        Return the oldest Evidence in this case whose content_hash matches.

        Used for per-case content-based deduplication: an attachment whose
        SHA-256 content hash already exists on the same case returns the
        existing Evidence instead of creating a new row.

        Args:
            case_id: Case to search within (scope is per-case, not global).
            content_hash: SHA-256 hex of UTF-8 text (as produced by
                PreprocessingService.classify_and_extract).

        Returns:
            The oldest matching Evidence (by collection timestamp) if found,
            None otherwise. NULL content_hash rows are never matched.

        Raises:
            RepositoryException: If lookup fails
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
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """
        Search cases by text query.

        Args:
            query: Search query
            user_id: Filter by user
            organization_id: Filter by organization
            limit: Maximum results

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
        # Agent execution storage (migrated from Agent module)
        self._agent_executions: Dict[str, Any] = (
            {}
        )  # execution_id -> AgentExecution (tool_calls stored separately)
        self._agent_executions: Dict[str, Any] = (
            {}
        )  # execution_id -> AgentExecution (tool_calls stored separately)
        self._agent_tool_calls: Dict[str, Any] = {}  # tool_call_id -> AgentToolCall
        self._checkpoints: Dict[str, Any] = {}  # checkpoint_id -> CaseCheckpoint

    async def save(self, case: Case) -> Case:
        """Save case to memory."""
        # Update timestamp
        case.updated_at = datetime.now(case.updated_at.tzinfo)

        # Store (deep copy to simulate persistence)
        self._cases[case.case_id] = case

        return case

    async def get(self, case_id: str) -> Optional[Case]:
        """Get case from memory."""
        return self._cases.get(case_id)

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """List cases with filters."""
        # Filter cases
        filtered = list(self._cases.values())

        if user_id:
            filtered = [c for c in filtered if c.user_id == user_id]

        if organization_id:
            filtered = [c for c in filtered if c.organization_id == organization_id]

        if status:
            filtered = [c for c in filtered if c.status == status]

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

    async def find_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional[Evidence]:
        """Find oldest Evidence matching content_hash within a single case."""
        if not content_hash:
            return None
        case = self._cases.get(case_id)
        if case is None:
            return None
        matches = [
            ev
            for ev in case.evidence
            if getattr(ev, "content_hash", None) == content_hash
        ]
        if not matches:
            return None
        matches.sort(key=lambda ev: getattr(ev, "collected_at_turn", 0) or 0)
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

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """Search cases by text query (simple substring match)."""
        query_lower = query.lower()

        # Filter cases
        filtered = []
        for case in self._cases.values():
            # Search in title and description
            if (
                query_lower in case.title.lower()
                or query_lower in case.description.lower()
                or query_lower in case.case_id.lower()
            ):

                # Apply user filter
                if user_id and case.user_id != user_id:
                    continue

                # Apply org filter
                if organization_id and case.organization_id != organization_id:
                    continue

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

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for case in memory."""
        from faultmaven.utils.serialization import to_json_compatible

        case = self._cases.get(case_id)
        if not case:
            return {}

        analytics = {
            "case_id": case.case_id,
            "status": case.status.value,
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
                case.status == CaseStatus.CLOSED
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
    # Agent Execution Operations (migrated from Agent module)
    # ============================================================

    async def create_agent_execution(self, execution: Any) -> Any:
        """Create new agent execution record (in-memory implementation)."""
        from faultmaven.modules.case.domain.owned_models.agent_execution import (
            AgentExecution,
        )

        if execution.execution_id in self._agent_executions:
            raise ValueError(f"Agent execution {execution.execution_id} already exists")

        # Store a deep copy (tool_calls stored separately)
        stored = deepcopy(execution)
        # Clear tool_calls from execution (stored separately)
        if hasattr(stored, "tool_calls"):
            stored.tool_calls = []
        self._agent_executions[execution.execution_id] = stored

        # Return with tool calls attached (empty initially)
        result = deepcopy(stored)
        if hasattr(result, "tool_calls"):
            result.tool_calls = []
        return result

    async def get_agent_execution(self, execution_id: str) -> Optional[Any]:
        """Get agent execution by ID with tool calls loaded (in-memory implementation)."""

        execution = self._agent_executions.get(execution_id)
        if execution is None:
            return None

        # Deep copy and attach tool calls
        result = deepcopy(execution)
        result.tool_calls = [
            deepcopy(tc)
            for tc in self._agent_tool_calls.values()
            if getattr(tc, "execution_id", None) == execution_id
        ]
        if hasattr(result, "tool_calls") and result.tool_calls:
            result.tool_calls.sort(key=lambda x: getattr(x, "created_at", datetime.min))

        return result

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional[Any] = None,
        agent_type: Optional[Any] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a case with optional filters (in-memory implementation)."""
        from faultmaven.modules.case.domain.owned_models.agent_execution import (
            AgentType,
            ExecutionStatus,
        )

        # Filter by case
        executions = [
            e
            for e in self._agent_executions.values()
            if getattr(e, "case_id", None) == case_id
        ]

        # Filter by status (ExecutionStatus is StrEnum - == works with enum, string, or .value)
        if status:
            status_val = status.value if hasattr(status, "value") else status
            executions = [
                e
                for e in executions
                if getattr(e, "status", None)
                and (
                    getattr(e, "status", None) == status
                    or (
                        hasattr(getattr(e, "status", None), "value")
                        and getattr(e, "status", None).value == status_val
                    )
                    or str(getattr(e, "status", "")) == str(status_val)
                )
            ]

        # Filter by agent_type (AgentType is StrEnum - == works with enum, string, or .value)
        if agent_type:
            agent_type_val = (
                agent_type.value if hasattr(agent_type, "value") else agent_type
            )
            executions = [
                e
                for e in executions
                if getattr(e, "agent_type", None)
                and (
                    getattr(e, "agent_type", None) == agent_type
                    or (
                        hasattr(getattr(e, "agent_type", None), "value")
                        and getattr(e, "agent_type", None).value == agent_type_val
                    )
                    or str(getattr(e, "agent_type", "")) == str(agent_type_val)
                )
            ]

        # Sort by created_at descending
        executions.sort(
            key=lambda e: getattr(e, "created_at", datetime.min), reverse=True
        )

        total = len(executions)

        # Apply pagination
        paginated = executions[offset : offset + limit]

        # Deep copy and attach tool calls
        result = []
        for e in paginated:
            exec_copy = deepcopy(e)
            exec_copy.tool_calls = [
                deepcopy(tc)
                for tc in self._agent_tool_calls.values()
                if getattr(tc, "execution_id", None) == e.execution_id
            ]
            if exec_copy.tool_calls:
                exec_copy.tool_calls.sort(
                    key=lambda x: getattr(x, "created_at", datetime.min)
                )
            result.append(exec_copy)

        return result, total

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional[Any] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a session with optional filters (in-memory implementation)."""

        # Filter by session_id via metadata
        executions = []
        for e in self._agent_executions.values():
            metadata = getattr(e, "metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id") == session_id:
                if status is None:
                    executions.append(e)
                else:
                    exec_status = getattr(e, "status", None)
                    status_val = status.value if hasattr(status, "value") else status
                    # Compare (works with enum, string, or .value)
                    if exec_status and (
                        exec_status == status
                        or (
                            hasattr(exec_status, "value")
                            and exec_status.value == status_val
                        )
                        or str(exec_status) == str(status_val)
                    ):
                        executions.append(e)

        # Sort by created_at ascending
        executions.sort(key=lambda e: getattr(e, "created_at", datetime.min))

        total = len(executions)

        # Apply pagination
        paginated = executions[offset : offset + limit]

        # Deep copy and attach tool calls
        result = []
        for e in paginated:
            exec_copy = deepcopy(e)
            exec_copy.tool_calls = [
                deepcopy(tc)
                for tc in self._agent_tool_calls.values()
                if getattr(tc, "execution_id", None) == e.execution_id
            ]
            if exec_copy.tool_calls:
                exec_copy.tool_calls.sort(
                    key=lambda x: getattr(x, "created_at", datetime.min)
                )
            result.append(exec_copy)

        return result, total

    async def update_agent_execution(self, execution: Any) -> Any:
        """Update agent execution status and results (in-memory implementation)."""

        if execution.execution_id not in self._agent_executions:
            raise ValueError(f"Agent execution {execution.execution_id} not found")

        # Update timestamp
        if hasattr(execution, "updated_at"):
            execution.updated_at = datetime.now(timezone.utc)

        # Update stored execution (preserve tool calls separately)
        stored = deepcopy(execution)
        if hasattr(stored, "tool_calls"):
            stored.tool_calls = []  # Tool calls stored separately
        self._agent_executions[execution.execution_id] = stored

        # Return with tool calls attached
        result = deepcopy(stored)
        result.tool_calls = [
            deepcopy(tc)
            for tc in self._agent_tool_calls.values()
            if getattr(tc, "execution_id", None) == execution.execution_id
        ]
        if result.tool_calls:
            result.tool_calls.sort(key=lambda x: getattr(x, "created_at", datetime.min))

        return result

    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution by ID (cascades to tool calls) (in-memory implementation)."""
        if execution_id not in self._agent_executions:
            return False

        # Delete execution
        del self._agent_executions[execution_id]

        # Delete associated tool calls (CASCADE)
        to_delete = [
            tc_id
            for tc_id, tc in self._agent_tool_calls.items()
            if getattr(tc, "execution_id", None) == execution_id
        ]
        for tc_id in to_delete:
            del self._agent_tool_calls[tc_id]

        return True

    async def create_agent_tool_call(self, tool_call: Any) -> Any:
        """Create new agent tool call record (in-memory implementation)."""

        tool_call_id = getattr(tool_call, "tool_call_id", None)
        if not tool_call_id:
            raise ValueError("tool_call must have tool_call_id")

        if tool_call_id in self._agent_tool_calls:
            raise ValueError(f"Tool call {tool_call_id} already exists")

        execution_id = getattr(tool_call, "execution_id", None)
        if execution_id and execution_id not in self._agent_executions:
            raise ValueError(f"Execution {execution_id} not found")

        self._agent_tool_calls[tool_call_id] = deepcopy(tool_call)
        return deepcopy(tool_call)

    async def update_agent_tool_call(self, tool_call: Any) -> Any:
        """Update agent tool call status and results (in-memory implementation)."""

        tool_call_id = getattr(tool_call, "tool_call_id", None)
        if not tool_call_id:
            raise ValueError("tool_call must have tool_call_id")

        if tool_call_id not in self._agent_tool_calls:
            raise ValueError(f"Tool call {tool_call_id} not found")

        # Update timestamp
        if hasattr(tool_call, "updated_at"):
            tool_call.updated_at = datetime.now(timezone.utc)

        self._agent_tool_calls[tool_call_id] = deepcopy(tool_call)
        return deepcopy(tool_call)

    async def get_agent_tool_calls_for_execution(self, execution_id: str) -> List[Any]:
        """Get all tool calls for an execution (in-memory implementation)."""

        tool_calls = [
            deepcopy(tc)
            for tc in self._agent_tool_calls.values()
            if getattr(tc, "execution_id", None) == execution_id
        ]
        tool_calls.sort(key=lambda x: getattr(x, "created_at", datetime.min))
        return tool_calls

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case (in-memory implementation)."""
        return len(
            [
                e
                for e in self._agent_executions.values()
                if getattr(e, "case_id", None) == case_id
            ]
        )

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional[Any] = None,
    ) -> Optional[Any]:
        """Get the most recent agent execution for a case (in-memory implementation)."""

        executions = [
            e
            for e in self._agent_executions.values()
            if getattr(e, "case_id", None) == case_id
        ]

        if agent_type:
            agent_type_val = (
                agent_type.value if hasattr(agent_type, "value") else agent_type
            )
            executions = [
                e
                for e in executions
                if getattr(e, "agent_type", None)
                and (
                    getattr(e, "agent_type", None) == agent_type
                    or (
                        hasattr(getattr(e, "agent_type", None), "value")
                        and getattr(e, "agent_type", None).value == agent_type_val
                    )
                    or str(getattr(e, "agent_type", "")) == str(agent_type_val)
                )
            ]

        if not executions:
            return None

        # Get most recent by created_at
        latest = max(executions, key=lambda e: getattr(e, "created_at", datetime.min))

        # Deep copy and attach tool calls
        result = deepcopy(latest)
        result.tool_calls = [
            deepcopy(tc)
            for tc in self._agent_tool_calls.values()
            if getattr(tc, "execution_id", None) == latest.execution_id
        ]
        if result.tool_calls:
            result.tool_calls.sort(key=lambda x: getattr(x, "created_at", datetime.min))

        return result

    def clear(self):
        """Clear all cases, reports, and agent executions (testing utility)."""
        self._cases.clear()
        self._reports.clear()
        self._agent_executions.clear()
        self._agent_tool_calls.clear()


# NOTE: Legacy PostgreSQLCaseRepository was removed (2026-03-18).
# It used a single-table JSONB schema incompatible with the current normalized schema.
# Active implementations: SQLiteCaseRepository, PostgreSQLHybridCaseRepository


# ============================================================
# Repository Exception
# ============================================================


class RepositoryException(Exception):
    """Base exception for repository errors."""

    pass
