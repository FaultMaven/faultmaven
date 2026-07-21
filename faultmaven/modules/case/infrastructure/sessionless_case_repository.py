"""Sessionless Case Repository Wrapper.

This wrapper implements the CaseRepository interface without holding a session instance.
It creates a new session for each operation using get_db_session() context manager.

This follows Architectural Design Principle 5 (Composition Root):
- Services never resolve their own dependencies
- No shared session state across requests
- Thread-safe and concurrent-request safe

Architecture:
    SessionlessCaseRepository (wrapper, holds no state)
    └── Uses get_db_session() → Creates dialect-specific repository per operation
        ├── SQLite: SQLiteCaseRepository (local deployment)
        └── PostgreSQL: PostgreSQLHybridCaseRepository (cloud deployment)

Deployment Agnostic (Principle 1):
- Local Deployment (Self-Host): Works with SQLite database via SQLiteCaseRepository
- Cloud Deployment (Enterprise): Works with PostgreSQL database via PostgreSQLHybridCaseRepository
- Database backend selected via DATABASE_URL configuration at startup
- Dialect detection happens at runtime to select the correct repository
"""

import builtins
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.modules.case.contracts import (
    Case,
    CaseCheckpoint,
    CaseEntity,
    CaseState,
    EntityType,
    Evidence,
    Hypothesis,
    Solution,
    UploadedFile,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import CaseReport, ReportType

logger = logging.getLogger(__name__)


def get_repository_for_session(session):
    """
    Factory function to create the appropriate repository based on database dialect.

    This implements the deployment-agnostic architecture:
    - SQLite → SQLiteCaseRepository (no PostgreSQL-specific features)
    - PostgreSQL → PostgreSQLHybridCaseRepository (optimized for PostgreSQL)

    Args:
        session: SQLAlchemy AsyncSession

    Returns:
        CaseRepository implementation appropriate for the database dialect
    """
    # Detect dialect from session's engine bind
    dialect_name = "sqlite"  # Default fallback
    try:
        if session.bind:
            dialect_name = session.bind.dialect.name
        elif hasattr(session, "get_bind"):
            bind = session.get_bind()
            if bind:
                dialect_name = bind.dialect.name
    except Exception as e:
        logger.warning(f"Failed to detect database dialect, defaulting to SQLite: {e}")

    if dialect_name == "postgresql":
        from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
            PostgreSQLHybridCaseRepository,
        )

        return PostgreSQLHybridCaseRepository(session)
    else:
        # SQLite or any other dialect - use SQLite-compatible repository
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        return SQLiteCaseRepository(session)


class SessionlessCaseRepository(CaseRepository):
    """
    Sessionless wrapper that selects dialect-specific repository at runtime.

    This wrapper creates a new database session for each operation,
    ensuring thread-safety and proper transaction isolation.

    Design:
    - Holds NO session instance (stateless)
    - Creates session per operation using context manager
    - Detects database dialect and delegates to appropriate repository:
      - SQLite → SQLiteCaseRepository
      - PostgreSQL → PostgreSQLHybridCaseRepository
    """

    def __init__(self):
        """Initialize sessionless repository (no dependencies)."""
        pass

    async def save(self, case: Case) -> Case:
        """Save case with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.save(case)

    async def get(self, case_id: str) -> Case | None:
        """Get case with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get(case_id)

    async def list_by_user(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        status_filter: CaseState | None = None,
    ) -> tuple[list[Case], int]:
        """List cases with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list_by_user(user_id, limit, offset, status_filter)

    async def delete(self, case_id: str) -> bool:
        """Delete case with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.delete(case_id)

    async def find_uploaded_file_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> UploadedFile | None:
        """Find oldest UploadedFile in a case whose content_hash matches.

        Post-010: dedup is a file-level concern (uploads no longer
        create an Evidence row at intake).
        """
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.find_uploaded_file_by_content_hash(case_id, content_hash)

    async def list_evidence_by_time_window(
        self,
        case_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[Evidence]:
        """Return evidence whose coverage overlaps ``[start, end]``.

        Phase 3 — see ``CaseRepository.list_evidence_by_time_window``.
        Delegates to the dialect-specific repository under a fresh
        session, consistent with the other methods on this wrapper.
        """
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list_evidence_by_time_window(case_id, start, end)

    async def upsert_case_entities(
        self,
        case_id: str,
        evidence_id: str,
        entities: list[CaseEntity],
    ) -> None:
        """Phase 4 — see ``CaseRepository.upsert_case_entities``."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            await repo.upsert_case_entities(case_id, evidence_id, entities)

    async def find_entity(
        self,
        case_id: str,
        entity_value: str,
        entity_type: Optional[EntityType] = None,
    ) -> list[CaseEntity]:
        """Phase 4 — see ``CaseRepository.find_entity``."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.find_entity(case_id, entity_value, entity_type)

    async def list_top_entities(
        self,
        case_id: str,
        entity_type: EntityType,
        limit: int = 10,
    ) -> list[CaseEntity]:
        """Phase 4 — see ``CaseRepository.list_top_entities``."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list_top_entities(case_id, entity_type, limit)

    async def search_by_keyword(
        self, user_id: str, keyword: str, limit: int = 50
    ) -> list[Case]:
        """Search cases with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.search_by_keyword(user_id, keyword, limit)

    async def add_evidence(self, case_id: str, evidence: Evidence) -> None:
        """Add evidence with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            await repo.add_evidence(case_id, evidence)

    async def add_hypothesis(self, case_id: str, hypothesis: Hypothesis) -> None:
        """Add hypothesis with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            await repo.add_hypothesis(case_id, hypothesis)

    async def add_solution(self, case_id: str, solution: Solution) -> None:
        """Add solution with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            await repo.add_solution(case_id, solution)

    async def get_by_ids(self, case_ids: list[str]) -> list[Case]:
        """Bulk get cases with new session per operation."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_by_ids(case_ids)

    # ============================================================
    # CaseRepository Abstract Methods (from case_repository.py)
    # ============================================================

    async def list(
        self,
        user_id: str | None = None,
        organization_id: str | None = None,
        state: CaseState | None = None,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        shared_case_ids: list[str] | None = None,
        restrict_case_ids: list[str] | None = None,
        include_empty: bool = True,
    ) -> tuple[list[Case], int]:
        """List cases (filtered by user_id/state).

        organization_id is forwarded but does NOT scope reads in CE — tenant
        isolation is cloud RLS (ADR-006); see the underlying repositories.
        ``shared_case_ids`` widens owner-only scope to ``owned ∪ shared`` (§D4);
        ``restrict_case_ids`` narrows to one team's shares (filter-by-team).
        """
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list(
                user_id,
                organization_id,
                state,
                limit,
                offset,
                source,
                shared_case_ids=shared_case_ids,
                restrict_case_ids=restrict_case_ids,
                include_empty=include_empty,
            )

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        organization_id: str | None = None,
        limit: int = 20,
        shared_case_ids: builtins.list[str] | None = None,
        restrict_case_ids: builtins.list[str] | None = None,
    ) -> tuple[builtins.list[Case], int]:
        """Search cases by text query (scoped by user_id).

        organization_id is forwarded but does NOT scope reads in CE — tenant
        isolation is cloud RLS (ADR-006); see the underlying repositories.
        ``shared_case_ids`` widens owner-only scope to ``owned ∪ shared`` (§D4);
        ``restrict_case_ids`` narrows to one team's shares (filter-by-team).
        """
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.search(
                query,
                user_id,
                organization_id,
                limit,
                shared_case_ids=shared_case_ids,
                restrict_case_ids=restrict_case_ids,
            )

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add a message to a case."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.add_message(case_id, message_dict)

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> builtins.list[dict]:
        """Get messages for a case with pagination."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_messages(case_id, limit, offset)

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update case last_activity_at timestamp."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_activity_timestamp(case_id)

    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Scoped UPDATE of cosmetic metadata fields (no version bump)."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_metadata_fields(
                case_id, title=title, description=description
            )

    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """Scoped update of the `vectorized` flag on one evidence row."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_evidence_vectorized(
                case_id, evidence_id, vectorized
            )

    async def delete_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Explicit scoped delete of a single evidence row."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.delete_evidence(case_id, evidence_id)

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Explicit scoped delete of a single uploaded_file row."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.delete_uploaded_file(case_id, file_id)

    async def get_analytics(self, case_id: str) -> dict[str, Any]:
        """Compute analytics for a case."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_analytics(case_id)

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired/old cases."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.cleanup_expired(max_age_days, batch_size)

    async def count_user_cases_on_date(self, user_id: str, date: Any) -> int:
        """Count cases created by user on a specific date."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.count_user_cases_on_date(user_id, date)

    # ============================================================
    # Report Operations
    # ============================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Save report to persistence layer."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.add_report(report)

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Retrieve a report by ID."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_report(report_id)

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> builtins.list["CaseReport"]:
        """Get reports for a case with optional filtering."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_reports(
                case_id, report_type, include_history, only_current
            )

    async def count_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
    ) -> int:
        """Count persisted reports for a case (all versions)."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.count_reports(case_id, report_type)

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update an existing report."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_report(report)

    async def delete_report(self, report_id: str) -> bool:
        """Delete a report by ID."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.delete_report(report_id)

    # Standalone evidence operations were removed in storage redesign 2026-04
    # phase 2. Evidence is case-tied only, accessed via `case.evidence`.

    # ============================================================
    # Agent Execution Operations (migrated from Agent module)
    # ============================================================

    async def create_agent_execution(self, execution: Any) -> Any:
        """Create new agent execution record."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.create_agent_execution(execution)

    async def get_agent_execution(self, execution_id: str) -> Any | None:
        """Get agent execution by ID with tool calls loaded."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_agent_execution(execution_id)

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Any | None = None,
        agent_type: Any | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
        """List agent executions for a case with optional filters."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list_agent_executions_by_case(
                case_id, status, agent_type, limit, offset
            )

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Any | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
        """List agent executions for a session with optional filters."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.list_agent_executions_by_session(
                session_id, status, limit, offset
            )

    async def update_agent_execution(self, execution: Any) -> Any:
        """Update agent execution status and results."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_agent_execution(execution)

    async def delete_agent_execution(self, execution_id: str) -> bool:
        """Delete agent execution by ID (cascades to tool calls)."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.delete_agent_execution(execution_id)

    async def create_agent_tool_call(self, tool_call: Any) -> Any:
        """Create new agent tool call record."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.create_agent_tool_call(tool_call)

    async def update_agent_tool_call(self, tool_call: Any) -> Any:
        """Update agent tool call status and results."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.update_agent_tool_call(tool_call)

    async def get_agent_tool_calls_for_execution(
        self, execution_id: str
    ) -> builtins.list[Any]:
        """Get all tool calls for an execution."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_agent_tool_calls_for_execution(execution_id)

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        """Count agent executions for a case."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.count_agent_executions_by_case(case_id)

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Any | None = None,
    ) -> Any | None:
        """Get the most recent agent execution for a case."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_latest_agent_execution(case_id, agent_type)

    # ============================================================
    # Checkpoint Operations
    # ============================================================

    async def create_checkpoint(self, checkpoint: "CaseCheckpoint") -> "CaseCheckpoint":
        """Create a new case checkpoint."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.create_checkpoint(checkpoint)

    async def get_checkpoint(self, checkpoint_id: str) -> Optional["CaseCheckpoint"]:
        """Get a checkpoint by ID."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_checkpoint(checkpoint_id)

    async def get_checkpoints(self, case_id: str) -> builtins.list["CaseCheckpoint"]:
        """Get all checkpoints for a case."""
        async with get_db_session() as session:
            repo = get_repository_for_session(session)
            return await repo.get_checkpoints(case_id)
