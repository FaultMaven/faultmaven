"""Case Repository for milestone-based investigation persistence.

This module provides the repository pattern for Case domain model persistence.
It abstracts database operations and provides clean interfaces for the service layer.
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

# ============================================================
# Repository Interface
# ============================================================


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
        - Inquiry: Stored as InquiryData

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

    def clear(self):
        """Clear all cases (testing utility)."""
        self._cases.clear()


# NOTE: Legacy PostgreSQLCaseRepository was removed (2026-03-18).
# Active implementations: SQLiteCaseRepository, PostgreSQLHybridCaseRepository


class RepositoryException(Exception):
    """Base exception for repository errors."""

    pass
