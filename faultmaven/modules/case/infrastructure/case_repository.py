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
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportType,
    )
    from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint


# ============================================================
# Repository Interface
# ============================================================

logger = logging.getLogger(__name__)


class CaseRepository(ABC):
    """
    Abstract repository interface for Case persistence.

    Implementations:
    - PostgreSQLCaseRepository: Production database
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
        # Standalone evidence storage (migrated from Evidence module)
        self._standalone_evidence: Dict[str, Any] = (
            {}
        )  # evidence_id -> EvidenceArtifact
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
    # Standalone Evidence Operations (migrated from Evidence module)
    # ============================================================

    async def create_standalone_evidence(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Any:
        """Create standalone evidence record (in-memory implementation)."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from faultmaven.modules.case.domain.owned_models.evidence import (
            EvidenceArtifact,
            EvidenceArtifactType,
            StorageBackend,
        )

        evidence_id = str(uuid4())
        now = datetime.now(timezone.utc)

        # Infer evidence type from content_type
        if content_type.startswith("image/"):
            evidence_type = EvidenceArtifactType.SCREENSHOT
        elif content_type.startswith("video/"):
            evidence_type = EvidenceArtifactType.VIDEO_RECORDING
        elif "json" in content_type or content_type.startswith("text/"):
            evidence_type = EvidenceArtifactType.LOG_FILE
        elif "application/x-har" in content_type:
            evidence_type = EvidenceArtifactType.HAR_FILE
        else:
            evidence_type = EvidenceArtifactType.OTHER

        evidence = EvidenceArtifact(
            evidence_id=evidence_id,
            case_id="standalone",  # Default until linked
            user_id=uploaded_by,
            organization_id="default_org",  # Default
            original_filename=filename,
            stored_filename=filename,
            file_path=storage_path,
            evidence_type=evidence_type,
            mime_type=content_type,
            file_size=size_bytes,
            storage_backend=StorageBackend.LOCAL_FILESYSTEM,
            created_at=now,
            updated_at=now,
            description=description,
            metadata={},
            tags=tags or [],
            linked_case_ids=[],
        )

        self._standalone_evidence[evidence_id] = evidence
        return evidence

    async def get_standalone_evidence(self, evidence_id: str) -> Optional[Any]:
        """Get standalone evidence by ID (in-memory implementation)."""
        from faultmaven.modules.case.domain.owned_models.evidence import (
            EvidenceArtifact,
        )

        return self._standalone_evidence.get(evidence_id)

    async def list_standalone_evidence(self, filters: Any) -> tuple[List[Any], int]:
        """List standalone evidence with filters (in-memory implementation)."""
        from uuid import UUID

        from faultmaven.modules.case.domain.owned_models.evidence import (
            EvidenceListFilter,
        )

        all_evidence = list(self._standalone_evidence.values())
        filtered = all_evidence

        # Apply filters if EvidenceListFilter provided
        if filters and hasattr(filters, "case_id") and filters.case_id:
            # Convert UUID to string for comparison (linked_case_ids stores strings)
            case_id_str = (
                str(filters.case_id)
                if isinstance(filters.case_id, UUID)
                else filters.case_id
            )
            filtered = [
                e for e in filtered if case_id_str in getattr(e, "linked_case_ids", [])
            ]

        if filters and hasattr(filters, "uploaded_by") and filters.uploaded_by:
            # Convert UUID to string for comparison (user_id is stored as string)
            uploaded_by_str = (
                str(filters.uploaded_by)
                if isinstance(filters.uploaded_by, UUID)
                else filters.uploaded_by
            )
            filtered = [
                e for e in filtered if getattr(e, "user_id", None) == uploaded_by_str
            ]
        if filters and hasattr(filters, "tags") and filters.tags:
            # Match any tag
            filtered = [
                e
                for e in filtered
                if any(tag in getattr(e, "tags", []) for tag in filters.tags)
            ]
        if (
            filters
            and hasattr(filters, "filename_contains")
            and filters.filename_contains
        ):
            filtered = [
                e
                for e in filtered
                if filters.filename_contains.lower()
                in getattr(e, "original_filename", "").lower()
            ]

        total = len(filtered)

        # Apply pagination
        offset = getattr(filters, "offset", 0) if filters else 0
        limit = getattr(filters, "limit", 50) if filters else 50
        paginated = filtered[offset : offset + limit]

        return paginated, total

    async def delete_standalone_evidence(self, evidence_id: str) -> bool:
        """Delete standalone evidence record (in-memory implementation)."""
        if evidence_id in self._standalone_evidence:
            del self._standalone_evidence[evidence_id]
            return True
        return False

    async def link_standalone_evidence_to_case(
        self, evidence_id: str, case_id: str
    ) -> Optional[Any]:
        """Link standalone evidence to a case (in-memory implementation)."""
        from datetime import datetime, timezone

        from faultmaven.modules.case.domain.owned_models.evidence import (
            EvidenceArtifact,
        )

        evidence = self._standalone_evidence.get(evidence_id)
        if not evidence:
            return None

        # Update linked_case_ids
        linked_case_ids = getattr(evidence, "linked_case_ids", [])
        if case_id not in linked_case_ids:
            linked_case_ids.append(case_id)
            # Create updated evidence with new linked_case_ids
            updated_evidence = EvidenceArtifact(
                evidence_id=evidence.evidence_id,
                case_id=(
                    linked_case_ids[0] if linked_case_ids else "standalone"
                ),  # Primary case is first
                user_id=evidence.user_id,
                organization_id=evidence.organization_id,
                original_filename=evidence.original_filename,
                stored_filename=evidence.stored_filename,
                file_path=evidence.file_path,
                evidence_type=evidence.evidence_type,
                mime_type=evidence.mime_type,
                file_size=evidence.file_size,
                storage_backend=evidence.storage_backend,
                created_at=evidence.created_at,
                updated_at=datetime.now(timezone.utc),
                description=evidence.description,
                metadata=getattr(evidence, "metadata", {}),
                tags=getattr(evidence, "tags", []),
                linked_case_ids=linked_case_ids,
            )
            self._standalone_evidence[evidence_id] = updated_evidence
            return updated_evidence

        return evidence

    async def update_standalone_evidence(self, evidence: Any) -> Any:
        """Update standalone evidence record (in-memory implementation)."""
        from datetime import datetime, timezone

        from faultmaven.modules.case.domain.owned_models.evidence import (
            EvidenceArtifact,
        )

        evidence_id = getattr(evidence, "evidence_id", None)
        if not evidence_id or evidence_id not in self._standalone_evidence:
            raise ValueError(f"Evidence {evidence_id} not found")

        # Update timestamp
        if hasattr(evidence, "updated_at"):
            evidence.updated_at = datetime.now(timezone.utc)

        # Store updated evidence
        self._standalone_evidence[evidence_id] = evidence
        return evidence

    async def set_primary_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Set evidence as primary for a case (unsets others) (in-memory implementation)."""
        from datetime import datetime, timezone

        # Get the evidence
        evidence = self._standalone_evidence.get(evidence_id)
        if not evidence:
            return False

        # Verify it's linked to the case
        if case_id not in getattr(evidence, "linked_case_ids", []):
            return False

        # Unset primary for all other evidence linked to this case
        for ev_id, ev in self._standalone_evidence.items():
            if ev_id != evidence_id and case_id in getattr(ev, "linked_case_ids", []):
                ev.is_primary = False
                ev.updated_at = datetime.now(timezone.utc)

        # Set this evidence as primary
        evidence.is_primary = True
        evidence.updated_at = datetime.now(timezone.utc)

        return True

    async def get_primary_evidence(self, case_id: str) -> Optional[Any]:
        """Get primary evidence for a case (in-memory implementation)."""
        # Find evidence linked to this case that is marked as primary
        for evidence in self._standalone_evidence.values():
            if case_id in getattr(evidence, "linked_case_ids", []) and getattr(
                evidence, "is_primary", False
            ):
                return evidence
        return None

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
        """Clear all cases, reports, standalone evidence, and agent executions (testing utility)."""
        self._cases.clear()
        self._reports.clear()
        self._standalone_evidence.clear()
        self._agent_executions.clear()
        self._agent_tool_calls.clear()


# ============================================================
# PostgreSQL Implementation (Production)
# ============================================================


class PostgreSQLCaseRepository(CaseRepository):
    """
    PostgreSQL case repository for production use.

    Uses SQLAlchemy for database operations.
    Stores complex nested objects as JSONB columns.
    """

    def __init__(self, db_session):
        """
        Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session
        """
        self.db = db_session

    async def save(self, case: Case) -> Case:
        """
        Save case to PostgreSQL.

        Uses INSERT ON CONFLICT UPDATE (upsert) for atomic save.
        """
        from sqlalchemy import text

        # Update timestamp
        case.updated_at = datetime.now(case.updated_at.tzinfo)

        # Serialize complex fields to JSON
        case_data = {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "organization_id": case.organization_id,
            "title": case.title,
            "description": case.description,
            "status": case.status.value,
            "status_history": json.dumps(
                [t.model_dump(mode="json") for t in case.action_history]
            ),
            "closure_reason": case.closure_reason,
            # Progress (JSONB)
            "progress": json.dumps(case.progress.model_dump(mode="json")),
            # Turn tracking
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "turn_history": json.dumps(
                [t.model_dump(mode="json") for t in case.turn_history]
            ),
            # Path and strategy
            "path_selection": (
                json.dumps(case.path_selection.model_dump(mode="json"))
                if case.path_selection
                else None
            ),
            "investigation_strategy": case.investigation_strategy.value,
            # Problem context
            "inquiry": json.dumps(case.inquiry.model_dump(mode="json")),
            "problem_verification": (
                json.dumps(case.problem_verification.model_dump(mode="json"))
                if case.problem_verification
                else None
            ),
            # Investigation data (JSONB arrays)
            "uploaded_files": json.dumps(
                [f.model_dump(mode="json") for f in case.uploaded_files]
            ),
            "evidence": json.dumps([e.model_dump(mode="json") for e in case.evidence]),
            "hypotheses": json.dumps(
                {k: v.model_dump(mode="json") for k, v in case.hypotheses.items()}
            ),
            "solutions": json.dumps(
                [s.model_dump(mode="json") for s in case.solutions]
            ),
            # Conclusions
            "working_conclusion": (
                json.dumps(case.working_conclusion.model_dump(mode="json"))
                if case.working_conclusion
                else None
            ),
            "root_cause_conclusion": (
                json.dumps(case.root_cause_conclusion.model_dump(mode="json"))
                if case.root_cause_conclusion
                else None
            ),
            # Special states
            "escalation_state": (
                json.dumps(case.escalation_state.model_dump(mode="json"))
                if case.escalation_state
                else None
            ),
            # Documentation
            "documentation": json.dumps(case.documentation.model_dump(mode="json")),
            # Timestamps
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "last_activity_at": case.last_activity_at,
            "resolved_at": case.resolved_at,
            "closed_at": case.closed_at,
        }

        # Upsert query
        query = text("""
            INSERT INTO cases (
                case_id, user_id, organization_id, title, description, status,
                status_history, closure_reason, progress, current_turn,
                turns_without_progress, turn_history, path_selection,
                investigation_strategy, inquiry, problem_verification,
                uploaded_files, evidence, hypotheses, solutions, working_conclusion,
                root_cause_conclusion, escalation_state,
                documentation, created_at, updated_at, last_activity_at,
                resolved_at, closed_at
            ) VALUES (
                :case_id, :user_id, :organization_id, :title, :description, :status,
                :status_history, :closure_reason, :progress, :current_turn,
                :turns_without_progress, :turn_history, :path_selection,
                :investigation_strategy, :inquiry, :problem_verification,
                :uploaded_files, :evidence, :hypotheses, :solutions, :working_conclusion,
                :root_cause_conclusion, :escalation_state,
                :documentation, :created_at, :updated_at, :last_activity_at,
                :resolved_at, :closed_at
            )
            ON CONFLICT (case_id) DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                status = EXCLUDED.status,
                status_history = EXCLUDED.status_history,
                closure_reason = EXCLUDED.closure_reason,
                progress = EXCLUDED.progress,
                current_turn = EXCLUDED.current_turn,
                turns_without_progress = EXCLUDED.turns_without_progress,
                turn_history = EXCLUDED.turn_history,
                path_selection = EXCLUDED.path_selection,
                investigation_strategy = EXCLUDED.investigation_strategy,
                inquiry = EXCLUDED.inquiry,
                problem_verification = EXCLUDED.problem_verification,
                uploaded_files = EXCLUDED.uploaded_files,
                evidence = EXCLUDED.evidence,
                hypotheses = EXCLUDED.hypotheses,
                solutions = EXCLUDED.solutions,
                working_conclusion = EXCLUDED.working_conclusion,
                root_cause_conclusion = EXCLUDED.root_cause_conclusion,
                escalation_state = EXCLUDED.escalation_state,
                documentation = EXCLUDED.documentation,
                updated_at = EXCLUDED.updated_at,
                last_activity_at = EXCLUDED.last_activity_at,
                resolved_at = EXCLUDED.resolved_at,
                closed_at = EXCLUDED.closed_at
        """)

        await self.db.execute(query, case_data)
        await self.db.commit()

        return case

    async def get(self, case_id: str) -> Optional[Case]:
        """Retrieve case from PostgreSQL."""
        from sqlalchemy import text

        query = text("SELECT * FROM cases WHERE case_id = :case_id")
        result = await self.db.execute(query, {"case_id": case_id})
        row = result.first()

        if not row:
            return None

        # Reconstruct Case from database row
        return self._row_to_case(row)

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """List cases with filters."""
        from sqlalchemy import text

        # Build query with filters
        conditions = []
        params = {"limit": limit, "offset": offset}

        if user_id:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id

        if organization_id:
            conditions.append("organization_id = :organization_id")
            params["organization_id"] = organization_id

        if status:
            conditions.append("status = :status")
            params["status"] = status.value

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Count query
        count_query = text(f"SELECT COUNT(*) FROM cases WHERE {where_clause}")
        count_result = await self.db.execute(count_query, params)
        total_count = count_result.scalar()

        # Data query
        data_query = text(f"""
            SELECT * FROM cases
            WHERE {where_clause}
            ORDER BY last_activity_at DESC
            LIMIT :limit OFFSET :offset
        """)
        result = await self.db.execute(data_query, params)
        rows = result.fetchall()

        cases = [self._row_to_case(row) for row in rows]

        return cases, total_count

    async def delete(self, case_id: str) -> bool:
        """Delete case from PostgreSQL."""
        from sqlalchemy import text

        query = text("DELETE FROM cases WHERE case_id = :case_id")
        result = await self.db.execute(query, {"case_id": case_id})
        await self.db.commit()

        return result.rowcount > 0

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """Search cases using PostgreSQL full-text search."""
        from sqlalchemy import text

        # Build conditions
        conditions = ["(title ILIKE :query OR description ILIKE :query)"]
        params = {"query": f"%{query}%", "limit": limit}

        if user_id:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id

        if organization_id:
            conditions.append("organization_id = :organization_id")
            params["organization_id"] = organization_id

        where_clause = " AND ".join(conditions)

        # Count query
        count_query = text(f"SELECT COUNT(*) FROM cases WHERE {where_clause}")
        count_result = await self.db.execute(count_query, params)
        total_count = count_result.scalar()

        # Data query (order by relevance: title match > description match)
        data_query = text(f"""
            SELECT * FROM cases
            WHERE {where_clause}
            ORDER BY
                CASE WHEN title ILIKE :query THEN 1 ELSE 2 END,
                last_activity_at DESC
            LIMIT :limit
        """)
        result = await self.db.execute(data_query, params)
        rows = result.fetchall()

        cases = [self._row_to_case(row) for row in rows]

        return cases, total_count

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add message to case in PostgreSQL."""
        from sqlalchemy import text

        # PostgreSQL: messages stored as JSONB array, use array_append
        query = text("""
            UPDATE cases
            SET messages = messages || :message::jsonb,
                message_count = message_count + 1,
                last_activity_at = :timestamp
            WHERE case_id = :case_id
        """)

        result = await self.db.execute(
            query,
            {
                "case_id": case_id,
                "message": json.dumps(message_dict),
                "timestamp": datetime.now(timezone.utc),
            },
        )
        await self.db.commit()

        return result.rowcount > 0

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """Get messages from PostgreSQL with correct pagination."""
        from sqlalchemy import text

        # Get the messages JSONB array from the case
        query = text("SELECT messages FROM cases WHERE case_id = :case_id")
        result = await self.db.execute(query, {"case_id": case_id})
        row = result.fetchone()

        if not row or not row.messages:
            return []

        # Parse messages and apply pagination in Python
        # (PostgreSQL doesn't support LIMIT/OFFSET on jsonb_array_elements directly)
        all_messages = (
            json.loads(row.messages) if isinstance(row.messages, str) else row.messages
        )

        # Apply offset and limit
        return all_messages[offset : offset + limit]

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update last activity timestamp in PostgreSQL."""
        from sqlalchemy import text

        query = text("""
            UPDATE cases
            SET last_activity_at = :timestamp
            WHERE case_id = :case_id
        """)

        result = await self.db.execute(
            query, {"case_id": case_id, "timestamp": datetime.now(timezone.utc)}
        )
        await self.db.commit()

        return result.rowcount > 0

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics from PostgreSQL."""
        from sqlalchemy import text

        from faultmaven.utils.serialization import to_json_compatible

        query = text("""
            SELECT
                case_id,
                status,
                created_at,
                last_activity_at,
                resolved_at,
                message_count,
                current_turn,
                turns_without_progress,
                jsonb_array_length(evidence) as evidence_count,
                jsonb_object_keys(hypotheses)::text[] as hypothesis_keys,
                jsonb_array_length(solutions) as solution_count,
                investigation_strategy,
                (working_conclusion IS NOT NULL) as has_working_conclusion,
                (root_cause_conclusion IS NOT NULL) as has_root_cause,
                (escalation_state IS NOT NULL) as is_escalated
            FROM cases
            WHERE case_id = :case_id
        """)

        result = await self.db.execute(query, {"case_id": case_id})
        row = result.fetchone()

        if not row:
            return {}

        analytics = {
            "case_id": row.case_id,
            "status": row.status,
            "created_at": to_json_compatible(row.created_at),
            "last_activity_at": to_json_compatible(row.last_activity_at),
            "message_count": row.message_count,
            "current_turn": row.current_turn,
            "turns_without_progress": row.turns_without_progress,
            "evidence_count": row.evidence_count or 0,
            "hypothesis_count": len(row.hypothesis_keys) if row.hypothesis_keys else 0,
            "solution_count": row.solution_count or 0,
            "investigation_strategy": row.investigation_strategy,
            "has_working_conclusion": row.has_working_conclusion,
            "has_root_cause": row.has_root_cause,
            "is_escalated": row.is_escalated,
        }

        if row.resolved_at:
            analytics["resolved_at"] = to_json_compatible(row.resolved_at)
            duration = (row.resolved_at - row.created_at).total_seconds()
            analytics["resolution_time_seconds"] = duration

        return analytics

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired cases from PostgreSQL."""
        from datetime import timedelta, timezone

        from sqlalchemy import text

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        query = text("""
            DELETE FROM cases
            WHERE status = 'closed'
            AND closed_at < :cutoff_date
            AND case_id IN (
                SELECT case_id FROM cases
                WHERE status = 'closed' AND closed_at < :cutoff_date
                LIMIT :batch_size
            )
        """)

        result = await self.db.execute(
            query, {"cutoff_date": cutoff_date, "batch_size": batch_size}
        )
        await self.db.commit()

        return result.rowcount

    def _convert_legacy_inquiry_data(self, data: dict) -> dict:
        """Convert legacy LLM schema format to domain model format for backward compatibility.

        Old cases may have been saved with LLM schema format before conversion was added:
        - problem_confirmation.preliminary_guidance: Optional[str] (can be None)
        - preliminary_urgency.level: Literal["CRITICAL", "HIGH", ...] (uppercase)
        - preliminary_urgency.assessed_at_turn: missing field

        This method converts to domain model format:
        - problem_confirmation.preliminary_guidance: str (required, convert None to "")
        - preliminary_urgency.level: UrgencyLevel enum (lowercase)
        - preliminary_urgency.assessed_at_turn: int (default to 1)
        """
        # Handle problem_confirmation conversion
        if "problem_confirmation" in data and data["problem_confirmation"]:
            pc = data["problem_confirmation"]
            if "preliminary_guidance" in pc and pc["preliminary_guidance"] is None:
                pc["preliminary_guidance"] = ""

        # Handle preliminary_urgency conversion
        if "preliminary_urgency" in data and data["preliminary_urgency"]:
            pu = data["preliminary_urgency"]
            # Convert uppercase level to lowercase for UrgencyLevel enum
            if "level" in pu and isinstance(pu["level"], str):
                pu["level"] = pu["level"].lower()
            # Add missing assessed_at_turn field (default to 1 for old data)
            if "assessed_at_turn" not in pu:
                pu["assessed_at_turn"] = 1

        return data

    def _row_to_case(self, row) -> Case:
        """Convert database row to Case domain model."""
        # Parse JSON fields (required fields)
        progress = InvestigationProgress(**json.loads(row.progress))
        action_history = [CaseAction(**t) for t in json.loads(row.status_history)]
        turn_history = [TurnProgress(**t) for t in json.loads(row.turn_history)]
        uploaded_files = (
            [UploadedFile(**f) for f in json.loads(row.uploaded_files)]
            if row.uploaded_files
            else []
        )
        evidence = [Evidence(**e) for e in json.loads(row.evidence)]
        hypotheses = {k: Hypothesis(**v) for k, v in json.loads(row.hypotheses).items()}
        solutions = [Solution(**s) for s in json.loads(row.solutions)]

        # Fields with default_factory - handle NULL from database (old data or manual edits)
        # These should never be NULL, but database may have NULL from:
        # 1. Old migrations before field was added
        # 2. Manual database modifications
        # 3. Database schema defaults not matching Pydantic defaults
        # Parse inquiry with backward compatibility for legacy schema
        inquiry_data = json.loads(row.inquiry) if row.inquiry else {}
        inquiry_data = self._convert_legacy_inquiry_data(inquiry_data)
        inquiry = InquiryData(**inquiry_data) if inquiry_data else InquiryData()
        documentation = (
            DocumentationData(**json.loads(row.documentation))
            if row.documentation
            else DocumentationData()  # Use Pydantic default
        )

        # Optional fields (explicitly nullable in model)
        path_selection = (
            PathSelection(**json.loads(row.path_selection))
            if row.path_selection
            else None
        )
        problem_verification = (
            ProblemVerification(**json.loads(row.problem_verification))
            if row.problem_verification
            else None
        )
        working_conclusion = (
            WorkingConclusion(**json.loads(row.working_conclusion))
            if row.working_conclusion
            else None
        )
        root_cause_conclusion = (
            RootCauseConclusion(**json.loads(row.root_cause_conclusion))
            if row.root_cause_conclusion
            else None
        )
        escalation_state = (
            EscalationState(**json.loads(row.escalation_state))
            if row.escalation_state
            else None
        )

        # Parse messages field (list of dicts)
        messages = json.loads(row.messages) if row.messages else []

        # Backward compatibility: Fix description for old INVESTIGATING cases
        # Old cases may be in INVESTIGATING status with empty description
        description = row.description
        if (
            CaseStatus(row.status) == CaseStatus.INVESTIGATING
            and (not description or not description.strip())
            and inquiry.proposed_problem_statement
        ):
            description = inquiry.proposed_problem_statement
            # Log only in debug mode to avoid production log spam
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Auto-healed missing description for case {row.case_id} "
                    f"from proposed_problem_statement"
                )

        # Reconstruct Case
        return Case(
            case_id=row.case_id,
            user_id=row.user_id,
            organization_id=row.organization_id,
            title=row.title,
            description=description,
            status=CaseStatus(row.status),
            action_history=action_history,
            closure_reason=row.closure_reason,
            progress=progress,
            current_turn=row.current_turn,
            turns_without_progress=row.turns_without_progress,
            turn_history=turn_history,
            path_selection=path_selection,
            investigation_strategy=row.investigation_strategy,
            inquiry=inquiry,
            problem_verification=problem_verification,
            uploaded_files=uploaded_files,
            evidence=evidence,
            hypotheses=hypotheses,
            solutions=solutions,
            working_conclusion=working_conclusion,
            root_cause_conclusion=root_cause_conclusion,
            escalation_state=escalation_state,
            documentation=documentation,
            messages=messages,  # CRITICAL: Add messages field
            message_count=row.message_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_activity_at=row.last_activity_at,
            resolved_at=row.resolved_at,
            closed_at=row.closed_at,
        )

    # ============================================================
    # Standalone Evidence Operations (migrated from Evidence module)
    # TODO: Implement these methods properly
    # ============================================================

    async def create_standalone_evidence(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: str,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Any:
        """Create standalone evidence record (PostgreSQL stub - needs implementation)."""
        raise NotImplementedError(
            "create_standalone_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def get_standalone_evidence(self, evidence_id: str) -> Optional[Any]:
        """Get standalone evidence by ID (PostgreSQL stub)."""
        raise NotImplementedError(
            "get_standalone_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def list_standalone_evidence(self, filters: Any) -> tuple[List[Any], int]:
        """List standalone evidence with filters (PostgreSQL stub)."""
        raise NotImplementedError(
            "list_standalone_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def delete_standalone_evidence(self, evidence_id: str) -> bool:
        """Delete standalone evidence record (PostgreSQL stub)."""
        raise NotImplementedError(
            "delete_standalone_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def link_standalone_evidence_to_case(
        self, evidence_id: str, case_id: str
    ) -> Optional[Any]:
        """Link standalone evidence to a case (PostgreSQL stub)."""
        raise NotImplementedError(
            "link_standalone_evidence_to_case not yet implemented in PostgreSQLCaseRepository"
        )

    async def update_standalone_evidence(self, evidence: Any) -> Any:
        """Update standalone evidence record (PostgreSQL stub)."""
        raise NotImplementedError(
            "update_standalone_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def set_primary_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Set evidence as primary for a case (PostgreSQL stub)."""
        raise NotImplementedError(
            "set_primary_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    async def get_primary_evidence(self, case_id: str) -> Optional[Any]:
        """Get primary evidence for a case (PostgreSQL stub)."""
        raise NotImplementedError(
            "get_primary_evidence not yet implemented in PostgreSQLCaseRepository"
        )

    # ============================================================
    # Agent Execution Operations (migrated from Agent module)
    # TODO: Implement these methods properly
    # ============================================================

    async def create_agent_execution(self, execution: Any) -> Any:
        """Create new agent execution record (PostgreSQL stub)."""
        raise NotImplementedError(
            "create_agent_execution not yet implemented in PostgreSQLCaseRepository"
        )

    async def get_agent_execution(self, execution_id: str) -> Optional[Any]:
        """Get agent execution by ID (PostgreSQL stub)."""
        raise NotImplementedError(
            "get_agent_execution not yet implemented in PostgreSQLCaseRepository"
        )

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional[str] = None,
        agent_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a case (PostgreSQL stub)."""
        raise NotImplementedError(
            "list_agent_executions_by_case not yet implemented in PostgreSQLCaseRepository"
        )

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a session (PostgreSQL stub)."""
        raise NotImplementedError(
            "list_agent_executions_by_session not yet implemented in PostgreSQLCaseRepository"
        )

    async def update_agent_execution(self, execution: Any) -> Any:
        """Update agent execution (PostgreSQL stub)."""
        raise NotImplementedError(
            "update_agent_execution not yet implemented in PostgreSQLCaseRepository"
        )

    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution (PostgreSQL stub)."""
        raise NotImplementedError(
            "delete_agent_execution not yet implemented in PostgreSQLCaseRepository"
        )

    async def create_agent_tool_call(self, tool_call: Any) -> Any:
        """Create new agent tool call record (PostgreSQL stub)."""
        raise NotImplementedError(
            "create_agent_tool_call not yet implemented in PostgreSQLCaseRepository"
        )

    async def update_agent_tool_call(self, tool_call: Any) -> Any:
        """Update agent tool call (PostgreSQL stub)."""
        raise NotImplementedError(
            "update_agent_tool_call not yet implemented in PostgreSQLCaseRepository"
        )

    async def get_agent_tool_calls_for_execution(self, execution_id: str) -> List[Any]:
        """Get all tool calls for an execution (PostgreSQL stub)."""
        raise NotImplementedError(
            "get_agent_tool_calls_for_execution not yet implemented in PostgreSQLCaseRepository"
        )

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case (PostgreSQL stub)."""
        raise NotImplementedError(
            "count_agent_executions_by_case not yet implemented in PostgreSQLCaseRepository"
        )

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional[str] = None,
    ) -> Optional[Any]:
        """Get the most recent agent execution (PostgreSQL stub)."""
        raise NotImplementedError(
            "get_latest_agent_execution not yet implemented in PostgreSQLCaseRepository"
        )

    # ============================================================
    # Report Operations (TD-001: Already migrated, but not implemented here)
    # ============================================================
    # Note: PostgreSQLCaseRepository doesn't implement report methods yet.
    # These should be implemented in PostgreSQLHybridCaseRepository (production).
    # Stubs added for interface compliance.

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report (PostgreSQL stub - implemented in PostgreSQLHybridCaseRepository)."""
        raise NotImplementedError(
            "add_report not implemented in PostgreSQLCaseRepository - use PostgreSQLHybridCaseRepository"
        )

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Get report (PostgreSQL stub - implemented in PostgreSQLHybridCaseRepository)."""
        raise NotImplementedError(
            "get_report not implemented in PostgreSQLCaseRepository - use PostgreSQLHybridCaseRepository"
        )

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List["CaseReport"]:
        """Get reports (PostgreSQL stub - implemented in PostgreSQLHybridCaseRepository)."""
        raise NotImplementedError(
            "get_reports not implemented in PostgreSQLCaseRepository - use PostgreSQLHybridCaseRepository"
        )

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update report (PostgreSQL stub - implemented in PostgreSQLHybridCaseRepository)."""
        raise NotImplementedError(
            "update_report not implemented in PostgreSQLCaseRepository - use PostgreSQLHybridCaseRepository"
        )

    async def delete_report(self, report_id: str) -> bool:
        """Delete report (PostgreSQL stub - implemented in PostgreSQLHybridCaseRepository)."""
        raise NotImplementedError(
            "delete_report not implemented in PostgreSQLCaseRepository - use PostgreSQLHybridCaseRepository"
        )


# ============================================================
# Repository Exception
# ============================================================


class RepositoryException(Exception):
    """Base exception for repository errors."""

    pass
