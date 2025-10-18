"""Report storage interfaces.

This module defines the interface contracts for report persistence,
following FaultMaven's interface-based dependency injection pattern.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from .report import CaseReport, ReportType


class IReportStore(ABC):
    """
    Interface for report data persistence operations.

    This interface defines the contract for storing and retrieving case reports,
    supporting versioning, content management, and case closure integration.

    Architecture:
    - Metadata stored in Redis for fast queries
    - Content stored in ChromaDB for runbooks (similarity search)
    - Content stored in ChromaDB for other reports (efficient text storage)
    """

    @abstractmethod
    async def save_report(self, report: CaseReport) -> bool:
        """
        Save report with full content to storage.

        Logic:
        1. If report type is runbook → index in ChromaDB runbook collection
        2. Store metadata in Redis hash
        3. Mark previous version (same type) as not current
        4. Update case report indexes
        5. Return success status

        Args:
            report: CaseReport object with content

        Returns:
            True if report was saved successfully

        Raises:
            ServiceException: If storage operation fails
        """
        pass

    @abstractmethod
    async def get_report(self, report_id: str) -> Optional[CaseReport]:
        """
        Retrieve report by ID with full content.

        Args:
            report_id: Report identifier (UUID)

        Returns:
            CaseReport object if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_case_reports(
        self,
        case_id: str,
        include_history: bool = False,
        report_type: Optional[ReportType] = None
    ) -> List[CaseReport]:
        """
        Get reports for a case.

        Args:
            case_id: Case identifier
            include_history: If False, return only is_current=True reports
                           If True, return all versions
            report_type: Optional filter by report type

        Returns:
            List of CaseReport objects sorted by type and version (descending)
        """
        pass

    @abstractmethod
    async def get_latest_reports_for_closure(
        self,
        case_id: str
    ) -> List[CaseReport]:
        """
        Get current version of all report types for case closure.

        This is used when closing a case to link reports to closure.

        Args:
            case_id: Case identifier

        Returns:
            List of CaseReport objects with is_current=True
        """
        pass

    @abstractmethod
    async def mark_reports_linked_to_closure(
        self,
        case_id: str,
        report_ids: List[str]
    ) -> bool:
        """
        Mark reports as linked to case closure.

        Sets linked_to_closure=True on specified reports.

        Args:
            case_id: Case identifier
            report_ids: List of report IDs to mark

        Returns:
            True if update successful
        """
        pass

    @abstractmethod
    async def delete_case_reports(self, case_id: str) -> bool:
        """
        Delete all reports for a case.

        This cascades from case deletion and removes:
        - Report metadata from Redis
        - Report content from ChromaDB
        - All indexes

        Args:
            case_id: Case identifier

        Returns:
            True if deletion successful
        """
        pass

    @abstractmethod
    async def get_report_count(self, case_id: str) -> int:
        """
        Get total number of reports for a case (all versions).

        Args:
            case_id: Case identifier

        Returns:
            Total report count
        """
        pass
