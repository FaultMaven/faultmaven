"""Sessionless Case Repository Wrapper.

This wrapper implements the CaseRepository interface without holding a session instance.
It creates a new session for each operation using get_db_session() context manager.

This follows Architectural Design Principle 5 (Composition Root):
- Services never resolve their own dependencies
- No shared session state across requests
- Thread-safe and concurrent-request safe

Architecture:
    SessionlessCaseRepository (wrapper, holds no state)
    └── Uses get_db_session() → Creates PostgreSQLHybridCaseRepository per operation

Deployment Agnostic (Principle 1):
- Local Deployment (Self-Host): Works with SQLite database
- Cloud Deployment (Enterprise): Works with PostgreSQL database
- Database backend selected via DATABASE_URL configuration at startup
"""

from typing import List, Optional, TYPE_CHECKING, Dict, Any
from datetime import datetime

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.case.domain.models import Case, CaseStatus, Evidence, Hypothesis, Solution
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.domain.owned_models.report import CaseReport, ReportType


class SessionlessCaseRepository(CaseRepository):
    """
    Sessionless wrapper for PostgreSQLHybridCaseRepository.

    This wrapper creates a new database session for each operation,
    ensuring thread-safety and proper transaction isolation.

    Design:
    - Holds NO session instance (stateless)
    - Creates session per operation using context manager
    - Delegates to PostgreSQLHybridCaseRepository for actual logic
    """

    def __init__(self):
        """Initialize sessionless repository (no dependencies)."""
        pass

    async def save(self, case: Case) -> Case:
        """Save case with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.save(case)

    async def get(self, case_id: str) -> Optional[Case]:
        """Get case with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get(case_id)

    async def list_by_user(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        status_filter: Optional[CaseStatus] = None,
    ) -> tuple[List[Case], int]:
        """List cases with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.list_by_user(user_id, limit, offset, status_filter)

    async def delete(self, case_id: str) -> bool:
        """Delete case with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.delete(case_id)

    async def search_by_keyword(
        self, user_id: str, keyword: str, limit: int = 50
    ) -> List[Case]:
        """Search cases with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.search_by_keyword(user_id, keyword, limit)

    async def add_evidence(self, case_id: str, evidence: Evidence) -> None:
        """Add evidence with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            await repo.add_evidence(case_id, evidence)

    async def add_hypothesis(self, case_id: str, hypothesis: Hypothesis) -> None:
        """Add hypothesis with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            await repo.add_hypothesis(case_id, hypothesis)

    async def add_solution(self, case_id: str, solution: Solution) -> None:
        """Add solution with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            await repo.add_solution(case_id, solution)

    async def update_status(
        self, case_id: str, new_status: CaseStatus, reason: Optional[str] = None
    ) -> None:
        """Update status with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            await repo.update_status(case_id, new_status, reason)

    async def get_by_ids(self, case_ids: List[str]) -> List[Case]:
        """Bulk get cases with new session per operation."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_by_ids(case_ids)

    # ============================================================
    # CaseRepository Abstract Methods (from case_repository.py)
    # ============================================================

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """List cases with optional filters."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.list(user_id, organization_id, status, limit, offset)

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """Search cases by text query."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.search(query, user_id, organization_id, limit)

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add a message to a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.add_message(case_id, message_dict)

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
        """Get messages for a case with pagination."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_messages(case_id, limit, offset)

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update case last_activity_at timestamp."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.update_activity_timestamp(case_id)

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """Compute analytics for a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_analytics(case_id)

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired/old cases."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.cleanup_expired(max_age_days, batch_size)

    # ============================================================
    # Report Operations
    # ============================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Save report to persistence layer."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.add_report(report)

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Retrieve a report by ID."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_report(report_id)

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List["CaseReport"]:
        """Get reports for a case with optional filtering."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_reports(case_id, report_type, include_history, only_current)

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update an existing report."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.update_report(report)

    async def delete_report(self, report_id: str) -> bool:
        """Delete a report by ID."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.delete_report(report_id)

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
        """Create standalone evidence record."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.create_standalone_evidence(
                filename, content_type, size_bytes, storage_path, uploaded_by, description, tags
            )

    async def get_standalone_evidence(self, evidence_id: str) -> Optional[Any]:
        """Get standalone evidence by ID."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_standalone_evidence(evidence_id)

    async def list_standalone_evidence(self, filters: Any) -> tuple[List[Any], int]:
        """List standalone evidence with filters."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.list_standalone_evidence(filters)

    async def delete_standalone_evidence(self, evidence_id: str) -> bool:
        """Delete standalone evidence record."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.delete_standalone_evidence(evidence_id)

    async def link_standalone_evidence_to_case(
        self, evidence_id: str, case_id: str
    ) -> Optional[Any]:
        """Link standalone evidence to a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.link_standalone_evidence_to_case(evidence_id, case_id)

    async def update_standalone_evidence(self, evidence: Any) -> Any:
        """Update standalone evidence record."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.update_standalone_evidence(evidence)

    async def set_primary_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Set evidence as primary for a case (unsets others)."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.set_primary_evidence(case_id, evidence_id)

    async def get_primary_evidence(self, case_id: str) -> Optional[Any]:
        """Get primary evidence for a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_primary_evidence(case_id)

    # ============================================================
    # Agent Execution Operations (migrated from Agent module)
    # ============================================================

    async def create_agent_execution(self, execution: Any) -> Any:
        """Create new agent execution record."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.create_agent_execution(execution)

    async def get_agent_execution(self, execution_id: str) -> Optional[Any]:
        """Get agent execution by ID with tool calls loaded."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_agent_execution(execution_id)

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional[Any] = None,
        agent_type: Optional[Any] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a case with optional filters."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.list_agent_executions_by_case(
                case_id, status, agent_type, limit, offset
            )

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional[Any] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        """List agent executions for a session with optional filters."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.list_agent_executions_by_session(
                session_id, status, limit, offset
            )

    async def update_agent_execution(self, execution: Any) -> Any:
        """Update agent execution status and results."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.update_agent_execution(execution)

    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution by ID (cascades to tool calls)."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.delete_agent_execution(execution_id)

    async def create_agent_tool_call(self, tool_call: Any) -> Any:
        """Create new agent tool call record."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.create_agent_tool_call(tool_call)

    async def update_agent_tool_call(self, tool_call: Any) -> Any:
        """Update agent tool call status and results."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.update_agent_tool_call(tool_call)

    async def get_agent_tool_calls_for_execution(self, execution_id: str) -> List[Any]:
        """Get all tool calls for an execution."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_agent_tool_calls_for_execution(execution_id)

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.count_agent_executions_by_case(case_id)

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional[Any] = None,
    ) -> Optional[Any]:
        """Get the most recent agent execution for a case."""
        async with get_db_session() as session:
            repo = PostgreSQLHybridCaseRepository(session)
            return await repo.get_latest_agent_execution(case_id, agent_type)
