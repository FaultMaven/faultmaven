"""SQLite Case Repository - Local Deployment Implementation.

This module implements the CaseRepository interface using SQLite-compatible SQL.
It mirrors the functionality of PostgreSQLHybridCaseRepository but avoids PostgreSQL-specific features:

PostgreSQL Features NOT Supported in SQLite:
- ::jsonb type casts → Use plain parameter binding
- jsonb_build_object() → Use json_object()
- FILTER (WHERE ...) → Use CASE WHEN ... expressions
- to_tsvector/ts_rank → Use LIKE pattern matching
- Array operators (= ALL, != ALL) → Use IN clauses with explicit values
- INTERVAL syntax → Use datetime() functions

Architecture:
    This repository follows the same hybrid schema as PostgreSQLHybridCaseRepository:
    - cases (main table)
    - evidence (1:N normalized table)
    - hypotheses (1:N normalized table)
    - solutions (1:N normalized table)
    - case_messages (1:N normalized table)
    - uploaded_files (1:N normalized table)
    - case_status_transitions (1:N normalized table)
"""

import builtins
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.contracts import (
    Case,
    CaseReport,
    CaseStatus,
    CaseStatusTransition,
    DocumentationData,
    CaseCheckpoint,
    EscalationState,
    Evidence,
    Hypothesis,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    PathSelection,
    ProblemVerification,
    ReportStatus,
    ReportType,
    RootCauseConclusion,
    RunbookMetadata,
    Solution,
    UploadedFile,
    WorkingConclusion,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.serialization import to_json_compatible

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SQLiteCaseRepository(CaseRepository):
    """
    SQLite repository using hybrid normalized schema.

    This implementation is SQLite-compatible, avoiding PostgreSQL-specific features.
    It provides the same functionality as PostgreSQLHybridCaseRepository for local deployments.

    Design Philosophy:
    - Normalize what you query (evidence, hypotheses, solutions, messages)
    - Embed what you don't (inquiry, conclusions, progress)
    - Use SQLite-compatible SQL syntax throughout
    """

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with SQLAlchemy async session."""
        self.db = db_session

    # ========================================================================
    # Core CRUD Operations
    # ========================================================================

    async def save(self, case: Case) -> Case:
        """Save case using hybrid schema with transactions."""
        try:
            case.updated_at = datetime.now(UTC)

            async with self.db.begin():
                await self._upsert_case_record(case)
                await self._upsert_evidence(case.case_id, case.evidence)
                await self._upsert_hypotheses(case.case_id, case.hypotheses)
                await self._upsert_solutions(case.case_id, case.solutions)
                await self._upsert_uploaded_files(case.case_id, case.uploaded_files)
                await self._upsert_messages(
                    case.case_id, case.messages
                )  # Save messages!

                if case.status_history:
                    await self._append_status_transitions(
                        case.case_id, case.status_history
                    )

                await self.db.commit()

            return case

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to save case {case.case_id}: {e}") from e

    async def get(self, case_id: str) -> Case | None:
        """Retrieve case by ID using separate queries for normalized tables."""
        try:
            # Main case query (no JSON aggregation - SQLite doesn't support it well)
            query = text("""
                SELECT *
                FROM cases
                WHERE case_id = :case_id
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            row = result.fetchone()

            if not row:
                return None

            # Load related data separately (SQLite-compatible approach)
            hypotheses_data = await self._load_hypotheses(case_id)
            solutions_data = await self._load_solutions(case_id)
            uploaded_files_data = await self._load_uploaded_files(case_id)
            messages_data = await self._load_messages(case_id)

            # Reconstruct Case domain object
            case = self._row_to_case(
                row, hypotheses_data, solutions_data, uploaded_files_data, messages_data
            )

            # Load evidence directly
            if case:
                await self._load_evidence_for_case(case)

            return case

        except Exception as e:
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    async def _load_hypotheses(self, case_id: str) -> list[dict]:
        """Load hypotheses for a case."""
        query = text("""
            SELECT hypothesis_id, statement, status, likelihood, initial_likelihood,
                   generated_at_turn, last_updated_turn, last_progress_at_turn,
                   iterations_without_progress,
                   category, generation_mode, rationale, retirement_reason,
                   evidence_links, tested_at, concluded_at, proposed_at, updated_at, metadata
            FROM hypotheses
            WHERE case_id = :case_id
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()

        hypotheses = []
        for row in rows:
            hypotheses.append(
                {
                    "hypothesis_id": row[0],
                    "statement": row[1],
                    "status": row[2],
                    "likelihood": row[3],
                    "initial_likelihood": row[4],
                    "generated_at_turn": row[5] or 0,
                    "last_updated_turn": row[6],
                    "last_progress_at_turn": row[7],
                    "iterations_without_progress": row[8],
                    "category": row[9],
                    "generation_mode": row[10],
                    "rationale": row[11],
                    "retirement_reason": row[12],
                    "evidence_links": json.loads(row[13]) if row[13] else {},
                    "tested_at": row[14],
                    "concluded_at": row[15],
                    "proposed_at": row[16],
                    "updated_at": row[17],
                    "metadata": json.loads(row[18]) if row[18] else {},
                }
            )
        return hypotheses

    async def _load_solutions(self, case_id: str) -> list[dict]:
        """Load solutions for a case."""
        query = text("""
            SELECT solution_id, solution_type, title, immediate_action, longterm_fix,
                   implementation_steps, commands, risks,
                   proposed_at, updated_at, metadata
            FROM solutions
            WHERE case_id = :case_id
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()

        solutions = []
        for row in rows:
            solutions.append(
                {
                    "solution_id": row[0],
                    "solution_type": row[1] or "other",
                    "title": row[2] or "Untitled solution",
                    "immediate_action": row[3],
                    "longterm_fix": row[4],
                    "implementation_steps": json.loads(row[5]) if row[5] else [],
                    "commands": json.loads(row[6]) if row[6] else [],
                    "risks": json.loads(row[7]) if row[7] else [],
                    "proposed_at": row[8],
                    "updated_at": row[9],
                    "metadata": json.loads(row[10]) if row[10] else {},
                }
            )
        return solutions

    async def _load_uploaded_files(self, case_id: str) -> list[dict]:
        """Load uploaded files for a case.

        Schema per design spec (case-schema.md §4.6):
        - size_bytes, data_type, content_ref, uploaded_at_turn, source_type, preprocessing_summary
        """
        query = text("""
            SELECT file_id, filename, size_bytes, data_type, uploaded_at_turn,
                   uploaded_at, source_type, content_ref, preprocessing_summary, metadata
            FROM uploaded_files
            WHERE case_id = :case_id
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()

        if not rows:
            return []

        columns = list(result.keys())
        files = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            files.append(
                {
                    "file_id": row_dict.get("file_id"),
                    "filename": row_dict.get("filename"),
                    "size_bytes": row_dict.get("size_bytes", 0),
                    "data_type": row_dict.get("data_type", "other"),
                    "uploaded_at_turn": row_dict.get("uploaded_at_turn", 0),
                    "uploaded_at": row_dict.get("uploaded_at"),
                    "source_type": row_dict.get("source_type", "file_upload"),
                    "content_ref": row_dict.get("content_ref", ""),
                    "preprocessing_summary": row_dict.get("preprocessing_summary", ""),
                }
            )
        return files

    async def _load_messages(self, case_id: str) -> list[dict]:
        """Load messages for a case from case_messages table.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        query = text("""
            SELECT message_id, turn_number, role, content, created_at, token_count, metadata
            FROM case_messages
            WHERE case_id = :case_id
            ORDER BY created_at ASC
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()

        if not rows:
            return []

        columns = list(result.keys())
        messages = []
        for row in rows:
            row_dict = dict(zip(columns, row))

            msg_timestamp = row_dict.get("created_at")
            if msg_timestamp:
                if isinstance(msg_timestamp, str):
                    msg_timestamp = msg_timestamp.replace(" ", "T")
                elif hasattr(msg_timestamp, "isoformat"):
                    msg_timestamp = msg_timestamp.isoformat()

            metadata = row_dict.get("metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata) if metadata else {}
            elif metadata is None:
                metadata = {}

            messages.append(
                {
                    "message_id": row_dict.get("message_id"),
                    "turn_number": row_dict.get("turn_number", 0),
                    "role": row_dict.get("role"),
                    "content": row_dict.get("content"),
                    "created_at": msg_timestamp,
                    "token_count": row_dict.get("token_count"),
                    "metadata": metadata,
                }
            )
        return messages

    async def _load_evidence_for_case(self, case: Case) -> None:
        """Load evidence for case directly from evidence_artifacts table."""
        try:
            query = text("""
                SELECT
                    evidence_id, case_id, user_id, organization_id,
                    original_filename, stored_filename, file_path,
                    evidence_type, mime_type, file_size, storage_backend,
                    created_at, updated_at, metadata, description,
                    is_primary
                FROM evidence_artifacts
                WHERE case_id = :case_id
                ORDER BY created_at DESC
                LIMIT 1000
            """)
            result = await self.db.execute(query, {"case_id": case.case_id})
            rows = result.fetchall()

            case.evidence = [
                Evidence(
                    evidence_id=str(row[0]),
                    data_type=row[7] if row[7] else "other",
                    summary=row[14] or "",
                    preprocessed_content=None,
                    storage_ref=row[6],
                    file_size=row[9],
                    filename=row[4],
                    timestamp=row[11],
                )
                for row in rows
            ]
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                f"Failed to load evidence for case {case.case_id}: {e}"
            )

    async def list(
        self,
        user_id: str | None = None,
        organization_id: str | None = None,
        status: CaseStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Case], int]:
        """List cases with optional filters and pagination."""
        try:
            where_clauses = []
            params = {"limit": limit, "offset": offset}

            if user_id:
                where_clauses.append("user_id = :user_id")
                params["user_id"] = user_id

            if organization_id:
                where_clauses.append("organization_id = :organization_id")
                params["organization_id"] = organization_id

            if status:
                where_clauses.append("status = :status")
                params["status"] = status.value

            where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            # Count query
            count_query = text(f"SELECT COUNT(*) FROM cases {where_sql}")
            count_result = await self.db.execute(count_query, params)
            total_count = count_result.scalar()

            # List query
            list_query = text(f"""
                SELECT case_id
                FROM cases
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT :limit OFFSET :offset
            """)

            result = await self.db.execute(list_query, params)
            case_ids = [row[0] for row in result.fetchall()]

            # Fetch full cases
            cases = []
            for cid in case_ids:
                case = await self.get(cid)
                if case:
                    cases.append(case)

            return cases, total_count

        except Exception as e:
            raise RepositoryException(f"Failed to list cases: {e}") from e

            return cases, total_count

        except Exception as e:
            raise RepositoryException(f"Failed to list cases: {e}") from e

    async def count_user_cases_on_date(self, user_id: str, date: Any) -> int:
        """
        Count cases created by a user on a specific date using SQLite date function.
        """
        try:
            # Ensure date string YYYY-MM-DD
            if hasattr(date, "strftime"):
                date_str = date.strftime("%Y-%m-%d")
            else:
                date_str = str(date)

            query = text("""
                SELECT COUNT(*)
                FROM cases
                WHERE user_id = :user_id
                AND date(created_at) = :date_str
                """)
            result = await self.db.execute(
                query, {"user_id": user_id, "date_str": date_str}
            )
            return result.scalar() or 0

        except Exception as e:
            raise RepositoryException(f"Failed to count user cases: {e}") from e

    async def delete(self, case_id: str) -> bool:
        """Delete case by ID."""
        try:
            query = text("DELETE FROM cases WHERE case_id = :case_id")
            result = await self.db.execute(query, {"case_id": case_id})
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to delete case {case_id}: {e}") from e

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        organization_id: str | None = None,
        limit: int = 20,
    ) -> tuple[builtins.list[Case], int]:
        """Search cases using SQLite LIKE pattern matching (no full-text search)."""
        try:
            where_clauses = [
                "(title LIKE :search_pattern OR title LIKE :search_pattern2)"
            ]
            params = {
                "search_pattern": f"%{query}%",
                "search_pattern2": f"%{query.lower()}%",
                "limit": limit,
            }

            if user_id:
                where_clauses.append("user_id = :user_id")
                params["user_id"] = user_id

            if organization_id:
                where_clauses.append("organization_id = :organization_id")
                params["organization_id"] = organization_id

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Search query using LIKE (SQLite-compatible)
            search_query = text(f"""
                SELECT case_id
                FROM cases
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT :limit
            """)

            result = await self.db.execute(search_query, params)
            case_ids = [row[0] for row in result.fetchall()]

            # Fetch full cases
            cases = []
            for cid in case_ids:
                case = await self.get(cid)
                if case:
                    cases.append(case)

            return cases, len(cases)

        except Exception as e:
            raise RepositoryException(f"Failed to search cases: {e}") from e

    # ========================================================================
    # Message Operations
    # ========================================================================

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add message to case_messages table.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        try:
            message_id = message_dict.get("message_id", f"msg_{uuid4().hex[:16]}")
            # Accept both 'timestamp' (legacy) and 'created_at' (design spec)
            created_at = (
                message_dict.get("created_at")
                or message_dict.get("timestamp")
                or datetime.now(UTC)
            )

            # SQLite-compatible: no ::jsonb type cast
            query = text("""
                INSERT INTO case_messages (message_id, case_id, turn_number, role, content, created_at, token_count, metadata)
                VALUES (:message_id, :case_id, :turn_number, :role, :content, :created_at, :token_count, :metadata)
            """)

            await self.db.execute(
                query,
                {
                    "message_id": message_id,
                    "case_id": case_id,
                    "turn_number": message_dict.get("turn_number", 0),
                    "role": message_dict.get("role", "user"),
                    "content": message_dict.get("content", ""),
                    "created_at": created_at,
                    "token_count": message_dict.get("token_count"),
                    "metadata": json.dumps(message_dict.get("metadata", {})),
                },
            )
            await self.db.commit()
            return True

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to add message to case {case_id}: {e}"
            ) from e

    # ========================================================================
    # Report Operations
    # ========================================================================

    async def add_report(self, report: CaseReport) -> CaseReport:
        """Add report using hybrid design (SQLite-compatible)."""
        try:
            # SQLite: no ::jsonb type cast
            query = text("""
                INSERT INTO case_reports (
                    report_id, case_id, type, status, title, summary,
                    author_id, runbook, recommendations, created_at,
                    updated_at, metadata
                ) VALUES (
                    :report_id, :case_id, :type, :status, :title, :summary,
                    :author_id, :runbook, :recommendations, :created_at,
                    :updated_at, :metadata
                )
            """)

            await self.db.execute(
                query,
                {
                    "report_id": report.report_id,
                    "case_id": report.case_id,
                    "type": report.type.value,
                    "status": report.status.value,
                    "title": report.title,
                    "summary": report.summary,
                    "author_id": report.author_id,
                    "runbook": (
                        json.dumps(to_json_compatible(report.runbook.model_dump()))
                        if report.runbook
                        else None
                    ),
                    "recommendations": json.dumps(
                        to_json_compatible(
                            [r.model_dump() for r in (report.recommendations or [])]
                        )
                    ),
                    "created_at": report.created_at,
                    "updated_at": report.updated_at,
                    "metadata": json.dumps(to_json_compatible(report.metadata)),
                },
            )
            await self.db.commit()
            return report

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to add report to case {report.case_id}: {e}"
            ) from e

    async def get_report(self, report_id: str) -> Optional[CaseReport]:
        """Get report by ID (SQLite-compatible)."""
        try:
            query = text("""
                SELECT report_id, case_id, type, status, title, summary,
                       author_id, runbook, recommendations, created_at,
                       updated_at, metadata
                FROM case_reports
                WHERE report_id = :report_id
            """)

            result = await self.db.execute(query, {"report_id": report_id})
            row = result.fetchone()

            if not row:
                return None

            return self._row_to_case_report(row)

        except Exception as e:
            raise RepositoryException(f"Failed to get report {report_id}: {e}") from e

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional[ReportType] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List[CaseReport]:
        """Get reports for case (SQLite-compatible)."""
        try:
            where_clauses = ["case_id = :case_id"]
            params = {"case_id": case_id}

            if report_type:
                where_clauses.append("type = :type")
                params["type"] = report_type.value

            if only_current:
                # Assuming 'current' logic means latest or specific status if defined
                # For now, just ordered by date
                pass

            where_sql = "WHERE " + " AND ".join(where_clauses)

            query = text(f"""
                SELECT report_id, case_id, type, status, title, summary,
                       author_id, runbook, recommendations, created_at,
                       updated_at, metadata
                FROM case_reports
                {where_sql}
                ORDER BY created_at DESC
            """)

            result = await self.db.execute(query, params)
            rows = result.fetchall()

            return [self._row_to_case_report(row) for row in rows]

        except Exception as e:
            raise RepositoryException(
                f"Failed to get reports for case {case_id}: {e}"
            ) from e

    async def update_report(self, report: CaseReport) -> CaseReport:
        """Update report (SQLite-compatible)."""
        try:
            # SQLite: no ::jsonb type cast
            query = text("""
                UPDATE case_reports
                SET type = :type,
                    status = :status,
                    title = :title,
                    summary = :summary,
                    author_id = :author_id,
                    runbook = :runbook,
                    recommendations = :recommendations,
                    updated_at = :updated_at,
                    metadata = :metadata
                WHERE report_id = :report_id
            """)

            await self.db.execute(
                query,
                {
                    "report_id": report.report_id,
                    "type": report.type.value,
                    "status": report.status.value,
                    "title": report.title,
                    "summary": report.summary,
                    "author_id": report.author_id,
                    "runbook": (
                        json.dumps(to_json_compatible(report.runbook.model_dump()))
                        if report.runbook
                        else None
                    ),
                    "recommendations": json.dumps(
                        to_json_compatible(
                            [r.model_dump() for r in (report.recommendations or [])]
                        )
                    ),
                    "updated_at": datetime.now(UTC),
                    "metadata": json.dumps(to_json_compatible(report.metadata)),
                },
            )
            await self.db.commit()
            return report

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update report {report.report_id}: {e}"
            ) from e

    async def delete_report(self, report_id: str) -> bool:
        """Delete report by ID."""
        try:
            query = text("DELETE FROM case_reports WHERE report_id = :report_id")
            result = await self.db.execute(query, {"report_id": report_id})
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to delete report {report_id}: {e}"
            ) from e

    def _row_to_case_report(self, row: Any) -> CaseReport:
        """Convert DB row to CaseReport domain model."""
        row_dict = dict(zip(row._fields, row))

        # Parse JSON fields
        runbook_data = row_dict.get("runbook")
        if isinstance(runbook_data, str):
            runbook_data = json.loads(runbook_data) if runbook_data else None

        recommendations_data = row_dict.get("recommendations")
        if isinstance(recommendations_data, str):
            recommendations_data = (
                json.loads(recommendations_data) if recommendations_data else []
            )

        metadata = row_dict.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}

        # Reconstruct objects
        runbook = None
        if runbook_data:
            # Import needed here or assume types available
            pass  # Logic to reconstruct RunbookMetadata from dict

        # For MVP, returning partial hydration or mapping as needed by domain
        # Assuming direct mapping for now
        return CaseReport(
            report_id=row_dict["report_id"],
            case_id=row_dict["case_id"],
            type=ReportType(row_dict["type"]),
            status=ReportStatus(row_dict["status"]),
            title=row_dict["title"],
            summary=row_dict["summary"],
            author_id=row_dict["author_id"],
            runbook=None,  # Placeholder/Need actual hydration logic
            recommendations=[],  # Placeholder
            created_at=row_dict["created_at"],
            updated_at=row_dict["updated_at"],
            metadata=metadata,
        )

    # ========================================================================
    # Checkpoint Operations (TASK-028)
    # ========================================================================

    async def create_checkpoint(self, checkpoint: CaseCheckpoint) -> CaseCheckpoint:
        """Create a new case checkpoint (SQLite-compatible)."""
        try:
            query = text("""
                INSERT INTO case_checkpoints (
                    checkpoint_id, case_id, turn_number, case_snapshot,
                    snapshot_hash, trigger, created_at, metadata
                ) VALUES (
                    :checkpoint_id, :case_id, :turn_number, :case_snapshot,
                    :snapshot_hash, :trigger, :created_at, :metadata
                )
            """)

            await self.db.execute(
                query,
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "case_id": checkpoint.case_id,
                    "turn_number": checkpoint.turn_number,
                    "case_snapshot": json.dumps(
                        to_json_compatible(checkpoint.case_snapshot)
                    ),
                    "snapshot_hash": checkpoint.snapshot_hash,
                    "trigger": checkpoint.trigger,
                    "created_at": checkpoint.created_at,
                    "metadata": json.dumps(to_json_compatible(checkpoint.metadata)),
                },
            )
            await self.db.commit()
            return checkpoint

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to create checkpoint for case {checkpoint.case_id}: {e}"
            ) from e

    async def get_checkpoint(self, checkpoint_id: str) -> Optional[CaseCheckpoint]:
        """Get a checkpoint by ID (SQLite-compatible)."""
        try:
            query = text("""
                SELECT checkpoint_id, case_id, turn_number, case_snapshot,
                       snapshot_hash, trigger, created_at, metadata
                FROM case_checkpoints
                WHERE checkpoint_id = :checkpoint_id
            """)

            result = await self.db.execute(query, {"checkpoint_id": checkpoint_id})
            row = result.fetchone()

            if not row:
                return None

            return self._row_to_case_checkpoint(row)

        except Exception as e:
            raise RepositoryException(
                f"Failed to get checkpoint {checkpoint_id}: {e}"
            ) from e

    async def get_checkpoints(self, case_id: str) -> List[CaseCheckpoint]:
        """Get all checkpoints for a case (SQLite-compatible)."""
        try:
            query = text("""
                SELECT checkpoint_id, case_id, turn_number, case_snapshot,
                       snapshot_hash, trigger, created_at, metadata
                FROM case_checkpoints
                WHERE case_id = :case_id
                ORDER BY turn_number ASC
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            rows = result.fetchall()

            return [self._row_to_case_checkpoint(row) for row in rows]

        except Exception as e:
            raise RepositoryException(
                f"Failed to get checkpoints for case {case_id}: {e}"
            ) from e

    def _row_to_case_checkpoint(self, row: Any) -> CaseCheckpoint:
        """Convert DB row to CaseCheckpoint domain model."""
        # Handle dict-like access for Row objects
        if hasattr(row, "_mapping"):
            row_dict = dict(row._mapping)
        else:
            # Fallback for older SQLAlchemy versions or raw tuples
            # Try to map by position if we know the query order, or check keys
            try:
                # If specific known columns
                keys = [
                    "checkpoint_id",
                    "case_id",
                    "turn_number",
                    "case_snapshot",
                    "snapshot_hash",
                    "trigger",
                    "created_at",
                    "metadata",
                ]
                row_dict = dict(zip(keys, row))
            except Exception:
                # If row has keys/keys()
                if hasattr(row, "keys"):
                    row_dict = dict(zip(row.keys(), row))
                else:
                    raise Exception("Cannot map row to dictionary")

        # Parse JSON fields
        snapshot_data = row_dict.get("case_snapshot")
        if isinstance(snapshot_data, str):
            snapshot_data = json.loads(snapshot_data) if snapshot_data else {}

        metadata = row_dict.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}

        # Handle timestamp
        created_at = row_dict.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace(" ", "T"))

        return CaseCheckpoint(
            checkpoint_id=row_dict["checkpoint_id"],
            case_id=row_dict["case_id"],
            turn_number=row_dict["turn_number"],
            case_snapshot=snapshot_data,
            snapshot_hash=row_dict["snapshot_hash"],
            trigger=row_dict["trigger"],
            created_at=created_at,
            metadata=metadata,
        )

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> builtins.list[dict]:
        """Get messages for case with pagination.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        try:
            query = text("""
                SELECT message_id, turn_number, role, content, created_at, token_count, metadata
                FROM case_messages
                WHERE case_id = :case_id
                ORDER BY created_at ASC
                LIMIT :limit OFFSET :offset
            """)

            result = await self.db.execute(
                query, {"case_id": case_id, "limit": limit, "offset": offset}
            )

            messages = []
            for row in result.fetchall():
                metadata = row[6]
                if isinstance(metadata, str):
                    metadata = json.loads(metadata) if metadata else {}

                # SQLite returns timestamps as strings, parse them if needed
                created_at = row[4]
                if created_at:
                    if isinstance(created_at, str):
                        # Parse ISO format timestamp string
                        from datetime import datetime

                        created_at = datetime.fromisoformat(
                            created_at.replace(" ", "T")
                        )
                        created_at = created_at.isoformat()
                    else:
                        created_at = created_at.isoformat()

                messages.append(
                    {
                        "message_id": row[0],
                        "turn_number": row[1],
                        "role": row[2],
                        "content": row[3],
                        "created_at": created_at,
                        "token_count": row[5],
                        "metadata": metadata,
                    }
                )

            return messages

        except Exception as e:
            raise RepositoryException(
                f"Failed to get messages for case {case_id}: {e}"
            ) from e

    # ========================================================================
    # Utility Operations
    # ========================================================================

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """Update last_activity_at timestamp."""
        try:
            # SQLite: use datetime('now') instead of NOW()
            query = text("""
                UPDATE cases
                SET last_activity_at = datetime('now')
                WHERE case_id = :case_id
            """)
            result = await self.db.execute(query, {"case_id": case_id})
            await self.db.commit()
            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update activity timestamp for case {case_id}: {e}"
            ) from e

    async def get_analytics(self, case_id: str) -> dict[str, Any]:
        """Compute analytics for case from normalized tables.

        Handles schema variations by computing file size separately using
        schema-compatible _load_uploaded_files method.
        """
        try:
            # SQLite-compatible: Use separate COUNT queries instead of FILTER
            # Note: file size computed separately for schema compatibility
            query = text("""
                SELECT
                    (SELECT COUNT(*) FROM hypotheses WHERE case_id = :case_id) as hypothesis_count,
                    (SELECT COUNT(*) FROM hypotheses WHERE case_id = :case_id AND status = 'validated') as validated_hypotheses,
                    (SELECT COUNT(*) FROM solutions WHERE case_id = :case_id) as solution_count,
                    (SELECT COUNT(*) FROM solutions WHERE case_id = :case_id AND status = 'implemented') as implemented_solutions,
                    (SELECT COUNT(*) FROM case_messages WHERE case_id = :case_id) as message_count,
                    (SELECT COUNT(*) FROM uploaded_files WHERE case_id = :case_id) as file_count
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            row = result.fetchone()

            if not row:
                return {}

            analytics = {
                "evidence_count": 0,
                "hypothesis_count": row[0] or 0,
                "validated_hypotheses": row[1] or 0,
                "solution_count": row[2] or 0,
                "implemented_solutions": row[3] or 0,
                "message_count": row[4] or 0,
                "file_count": row[5] or 0,
                "total_file_size": 0,
            }

            # Compute total file size using schema-compatible method
            try:
                files = await self._load_uploaded_files(case_id)
                analytics["total_file_size"] = sum(
                    f.get("size_bytes", 0) or 0 for f in files
                )
            except Exception:
                pass  # File size will remain 0

            # Load evidence count
            try:
                count_query = text(
                    "SELECT COUNT(*) FROM evidence_artifacts WHERE case_id = :case_id"
                )
                count_result = await self.db.execute(count_query, {"case_id": case_id})
                count_row = count_result.fetchone()
                if count_row:
                    analytics["evidence_count"] = count_row[0]
            except Exception:
                pass

            return analytics

        except Exception as e:
            raise RepositoryException(
                f"Failed to get analytics for case {case_id}: {e}"
            ) from e

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Clean up expired/old cases."""
        try:
            # SQLite: use datetime() function instead of INTERVAL
            query = text("""
                DELETE FROM cases
                WHERE case_id IN (
                    SELECT case_id
                    FROM cases
                    WHERE status = 'closed'
                    AND closed_at < datetime('now', '-' || :max_age_days || ' days')
                    LIMIT :batch_size
                )
            """)

            result = await self.db.execute(
                query, {"max_age_days": max_age_days, "batch_size": batch_size}
            )
            await self.db.commit()
            return result.rowcount

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to cleanup expired cases: {e}") from e

    # ========================================================================
    # Private Helper Methods
    # ========================================================================

    async def _upsert_case_record(self, case: Case) -> None:
        """Upsert main cases table (SQLite-compatible - no type casts)."""
        query = text("""
            INSERT INTO cases (
                case_id, user_id, organization_id, title,
                status, created_at, updated_at,
                inquiry, problem_verification, working_conclusion,
                root_cause_conclusion, path_selection,
                escalation_state, documentation, progress, metadata
            ) VALUES (
                :case_id, :user_id, :organization_id, :title,
                :status, :created_at, :updated_at,
                :inquiry, :problem_verification, :working_conclusion,
                :root_cause_conclusion, :path_selection,
                :escalation_state, :documentation, :progress, :metadata
            )
            ON CONFLICT (case_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                organization_id = EXCLUDED.organization_id,
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                inquiry = EXCLUDED.inquiry,
                problem_verification = EXCLUDED.problem_verification,
                working_conclusion = EXCLUDED.working_conclusion,
                root_cause_conclusion = EXCLUDED.root_cause_conclusion,
                path_selection = EXCLUDED.path_selection,
                escalation_state = EXCLUDED.escalation_state,
                documentation = EXCLUDED.documentation,
                progress = EXCLUDED.progress,
                metadata = EXCLUDED.metadata
        """)

        await self.db.execute(
            query,
            {
                "case_id": case.case_id,
                "user_id": case.user_id,
                "organization_id": case.organization_id,
                "title": case.title,
                "status": case.status.value,
                "created_at": case.created_at,
                "updated_at": case.updated_at,
                "inquiry": json.dumps(to_json_compatible(case.inquiry.model_dump())),
                "problem_verification": (
                    json.dumps(
                        to_json_compatible(case.problem_verification.model_dump())
                    )
                    if case.problem_verification
                    else None
                ),
                "working_conclusion": (
                    json.dumps(to_json_compatible(case.working_conclusion.model_dump()))
                    if case.working_conclusion
                    else None
                ),
                "root_cause_conclusion": (
                    json.dumps(
                        to_json_compatible(case.root_cause_conclusion.model_dump())
                    )
                    if case.root_cause_conclusion
                    else None
                ),
                "path_selection": (
                    json.dumps(to_json_compatible(case.path_selection.model_dump()))
                    if case.path_selection
                    else None
                ),
                "escalation_state": (
                    json.dumps(to_json_compatible(case.escalation_state.model_dump()))
                    if case.escalation_state
                    else None
                ),
                "documentation": json.dumps(
                    to_json_compatible(case.documentation.model_dump())
                ),
                "progress": json.dumps(to_json_compatible(case.progress.model_dump())),
                "metadata": json.dumps(
                    {
                        "current_turn": case.current_turn,
                        "turns_without_progress": case.turns_without_progress,
                        "message_count": case.message_count,
                        "closure_reason": case.closure_reason,
                        "closed_at": (
                            to_json_compatible(case.closed_at)
                            if case.closed_at
                            else None
                        ),
                        "resolved_at": (
                            to_json_compatible(case.resolved_at)
                            if hasattr(case, "resolved_at") and case.resolved_at
                            else None
                        ),
                    }
                ),
            },
        )

    async def _upsert_evidence(
        self, case_id: str, evidence_list: builtins.list[Evidence]
    ) -> None:
        """Upsert evidence records (SQLite-compatible)."""
        # Delete existing evidence not in current list using IN clause
        current_ids = [e.evidence_id for e in evidence_list]
        if current_ids:
            # SQLite: Use explicit IN clause instead of != ALL(array)
            placeholders = ", ".join([f":id_{i}" for i in range(len(current_ids))])
            delete_query = text(f"""
                DELETE FROM evidence
                WHERE case_id = :case_id
                AND evidence_id NOT IN ({placeholders})
            """)
            params = {"case_id": case_id}
            for i, eid in enumerate(current_ids):
                params[f"id_{i}"] = eid
            await self.db.execute(delete_query, params)

        # Upsert each evidence record (no ::jsonb type cast)
        for evidence in evidence_list:
            query = text("""
                INSERT INTO evidence (
                    evidence_id, case_id, category, summary, preprocessed_content,
                    content_ref, file_size, filename, upload_timestamp, metadata
                ) VALUES (
                    :evidence_id, :case_id, :category, :summary, :preprocessed_content,
                    :content_ref, :file_size, :filename, :upload_timestamp, :metadata
                )
                ON CONFLICT (evidence_id) DO UPDATE SET
                    category = EXCLUDED.category,
                    summary = EXCLUDED.summary,
                    preprocessed_content = EXCLUDED.preprocessed_content,
                    content_ref = EXCLUDED.content_ref,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "evidence_id": evidence.evidence_id,
                    "case_id": case_id,
                    "category": evidence.category.value,
                    "summary": evidence.summary,
                    "preprocessed_content": evidence.preprocessed_content or "",
                    "content_ref": evidence.content_ref,
                    "file_size": evidence.content_size_bytes,
                    "filename": "",  # Evidence doesn't have filename - would come from source
                    "upload_timestamp": evidence.collected_at.isoformat(),
                    "metadata": json.dumps({}),
                },
            )

    async def _upsert_hypotheses(
        self, case_id: str, hypotheses_dict: dict[str, Hypothesis]
    ) -> None:
        """Upsert hypotheses records (SQLite-compatible)."""
        current_ids = list(hypotheses_dict.keys())
        if current_ids:
            placeholders = ", ".join([f":id_{i}" for i in range(len(current_ids))])
            delete_query = text(f"""
                DELETE FROM hypotheses
                WHERE case_id = :case_id
                AND hypothesis_id NOT IN ({placeholders})
            """)
            params = {"case_id": case_id}
            for i, hid in enumerate(current_ids):
                params[f"id_{i}"] = hid
            await self.db.execute(delete_query, params)

        for hypothesis_id, hypothesis in hypotheses_dict.items():
            query = text("""
                INSERT INTO hypotheses (
                    hypothesis_id, case_id, statement, status, likelihood, initial_likelihood,
                    generated_at_turn, last_updated_turn, last_progress_at_turn,
                    iterations_without_progress,
                    category, generation_mode, rationale, retirement_reason, evidence_links,
                    tested_at, concluded_at, proposed_at, updated_at, metadata
                ) VALUES (
                    :hypothesis_id, :case_id, :statement, :status, :likelihood, :initial_likelihood,
                    :generated_at_turn, :last_updated_turn, :last_progress_at_turn,
                    :iterations_without_progress,
                    :category, :generation_mode, :rationale, :retirement_reason, :evidence_links,
                    :tested_at, :concluded_at, :proposed_at, :updated_at, :metadata
                )
                ON CONFLICT (hypothesis_id) DO UPDATE SET
                    statement = EXCLUDED.statement,
                    status = EXCLUDED.status,
                    likelihood = EXCLUDED.likelihood,
                    generated_at_turn = EXCLUDED.generated_at_turn,
                    last_updated_turn = EXCLUDED.last_updated_turn,
                    last_progress_at_turn = EXCLUDED.last_progress_at_turn,
                    iterations_without_progress = EXCLUDED.iterations_without_progress,
                    retirement_reason = EXCLUDED.retirement_reason,
                    evidence_links = EXCLUDED.evidence_links,
                    concluded_at = EXCLUDED.concluded_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "hypothesis_id": hypothesis_id,
                    "case_id": case_id,
                    "statement": hypothesis.statement,
                    "status": hypothesis.status.value,
                    "likelihood": hypothesis.likelihood,
                    "initial_likelihood": hypothesis.initial_likelihood,
                    "generated_at_turn": hypothesis.generated_at_turn,
                    "last_updated_turn": hypothesis.last_updated_turn,
                    "last_progress_at_turn": hypothesis.last_progress_at_turn,
                    "iterations_without_progress": hypothesis.iterations_without_progress,
                    "category": hypothesis.category.value,
                    "generation_mode": hypothesis.generation_mode.value,
                    "rationale": hypothesis.rationale,
                    "retirement_reason": hypothesis.retirement_reason,
                    "evidence_links": json.dumps(
                        {
                            eid: link.model_dump(mode="json")
                            for eid, link in hypothesis.evidence_links.items()
                        }
                    ),
                    "tested_at": hypothesis.tested_at,
                    "concluded_at": hypothesis.concluded_at,
                    "proposed_at": getattr(hypothesis, "proposed_at", None)
                    or datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "metadata": json.dumps({}),
                },
            )

    async def _upsert_solutions(
        self, case_id: str, solutions_list: builtins.list[Solution]
    ) -> None:
        """Upsert solutions records (SQLite-compatible)."""
        current_ids = [
            s.solution_id for s in solutions_list if hasattr(s, "solution_id")
        ]
        if current_ids:
            placeholders = ", ".join([f":id_{i}" for i in range(len(current_ids))])
            delete_query = text(f"""
                DELETE FROM solutions
                WHERE case_id = :case_id
                AND solution_id NOT IN ({placeholders})
            """)
            params = {"case_id": case_id}
            for i, sid in enumerate(current_ids):
                params[f"id_{i}"] = sid
            await self.db.execute(delete_query, params)

        for solution in solutions_list:
            solution_id = (
                solution.solution_id
                if hasattr(solution, "solution_id")
                else f"sol_{uuid4().hex[:12]}"
            )

            query = text("""
                INSERT INTO solutions (
                    solution_id, case_id, solution_type, title, immediate_action,
                    longterm_fix, implementation_steps, commands, risks,
                    description, status,
                    proposed_at, updated_at, metadata
                ) VALUES (
                    :solution_id, :case_id, :solution_type, :title, :immediate_action,
                    :longterm_fix, :implementation_steps, :commands, :risks,
                    :description, :status,
                    :proposed_at, :updated_at, :metadata
                )
                ON CONFLICT (solution_id) DO UPDATE SET
                    solution_type = EXCLUDED.solution_type,
                    title = EXCLUDED.title,
                    immediate_action = EXCLUDED.immediate_action,
                    longterm_fix = EXCLUDED.longterm_fix,
                    implementation_steps = EXCLUDED.implementation_steps,
                    commands = EXCLUDED.commands,
                    risks = EXCLUDED.risks,
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "solution_id": solution_id,
                    "case_id": case_id,
                    "solution_type": (
                        solution.solution_type.value
                        if hasattr(solution, "solution_type") and solution.solution_type
                        else "other"
                    ),
                    "title": (
                        solution.title
                        if hasattr(solution, "title") and solution.title
                        else "Untitled solution"
                    ),
                    "immediate_action": getattr(solution, "immediate_action", None),
                    "longterm_fix": getattr(solution, "longterm_fix", None),
                    "implementation_steps": json.dumps(
                        solution.implementation_steps
                        if hasattr(solution, "implementation_steps")
                        else []
                    ),
                    "commands": json.dumps(
                        solution.commands if hasattr(solution, "commands") else []
                    ),
                    "risks": json.dumps(
                        solution.risks if hasattr(solution, "risks") else []
                    ),
                    "description": (
                        solution.immediate_action
                        if hasattr(solution, "immediate_action")
                        and solution.immediate_action
                        else (
                            solution.title
                            if hasattr(solution, "title")
                            else str(solution)
                        )
                    ),
                    "status": "proposed",
                    "proposed_at": getattr(solution, "proposed_at", None)
                    or datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "metadata": json.dumps({}),
                },
            )

    async def _upsert_uploaded_files(
        self, case_id: str, files_list: builtins.list[UploadedFile]
    ) -> None:
        """Upsert uploaded_files records (SQLite-compatible)."""
        current_ids = [f.file_id for f in files_list]
        if current_ids:
            placeholders = ", ".join([f":id_{i}" for i in range(len(current_ids))])
            delete_query = text(f"""
                DELETE FROM uploaded_files
                WHERE case_id = :case_id
                AND file_id NOT IN ({placeholders})
            """)
            params = {"case_id": case_id}
            for i, fid in enumerate(current_ids):
                params[f"id_{i}"] = fid
            await self.db.execute(delete_query, params)

        for file in files_list:
            query = text("""
                INSERT INTO uploaded_files (
                    file_id, case_id, filename, size_bytes, data_type,
                    uploaded_at_turn, uploaded_at, source_type,
                    content_ref, preprocessing_summary, metadata
                ) VALUES (
                    :file_id, :case_id, :filename, :size_bytes, :data_type,
                    :uploaded_at_turn, :uploaded_at, :source_type,
                    :content_ref, :preprocessing_summary, :metadata
                )
                ON CONFLICT (file_id) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    size_bytes = EXCLUDED.size_bytes,
                    data_type = EXCLUDED.data_type,
                    uploaded_at_turn = EXCLUDED.uploaded_at_turn,
                    source_type = EXCLUDED.source_type,
                    content_ref = EXCLUDED.content_ref,
                    preprocessing_summary = EXCLUDED.preprocessing_summary,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "file_id": file.file_id,
                    "case_id": case_id,
                    "filename": file.filename,
                    "size_bytes": file.size_bytes,
                    "data_type": file.data_type,
                    "uploaded_at_turn": file.uploaded_at_turn,
                    "uploaded_at": file.uploaded_at,
                    "source_type": file.source_type,
                    "content_ref": file.content_ref,
                    "preprocessing_summary": file.preprocessing_summary,
                    "metadata": json.dumps({}),
                },
            )

    async def _upsert_messages(
        self, case_id: str, messages_list: builtins.list[dict]
    ) -> None:
        """Upsert case messages (SQLite-compatible).

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        # Get IDs of messages that should exist
        current_ids = [
            msg.get("message_id") for msg in messages_list if msg.get("message_id")
        ]

        if current_ids:
            # Delete messages not in current list
            placeholders = ", ".join([f":id_{i}" for i in range(len(current_ids))])
            delete_query = text(f"""
                DELETE FROM case_messages
                WHERE case_id = :case_id
                AND message_id NOT IN ({placeholders})
            """)
            params = {"case_id": case_id}
            for i, mid in enumerate(current_ids):
                params[f"id_{i}"] = mid
            await self.db.execute(delete_query, params)

        # Upsert each message
        for idx, msg in enumerate(messages_list):
            # Skip if no message_id (shouldn't happen, but be safe)
            if not msg.get("message_id"):
                continue

            query = text("""
                INSERT INTO case_messages (
                    message_id, case_id, turn_number, role, content, created_at, token_count, metadata
                ) VALUES (
                    :message_id, :case_id, :turn_number, :role, :content, :created_at, :token_count, :metadata
                )
                ON CONFLICT (message_id) DO UPDATE SET
                    turn_number = EXCLUDED.turn_number,
                    role = EXCLUDED.role,
                    content = EXCLUDED.content,
                    created_at = EXCLUDED.created_at,
                    token_count = EXCLUDED.token_count,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "message_id": msg.get("message_id"),
                    "case_id": case_id,
                    "turn_number": msg.get("turn_number", idx),
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "created_at": msg.get("created_at") or datetime.now(UTC),
                    "token_count": msg.get("token_count"),
                    "metadata": json.dumps(msg.get("metadata", {})),
                },
            )

    async def _append_status_transitions(
        self, case_id: str, transitions: builtins.list[CaseStatusTransition]
    ) -> None:
        """Append status transitions (SQLite-compatible)."""
        for transition in transitions:
            query = text("""
                INSERT INTO case_status_transitions (
                    case_id, from_status, to_status, reason, transitioned_at, metadata
                ) VALUES (
                    :case_id, :from_status, :to_status, :reason, :transitioned_at, :metadata
                )
                ON CONFLICT DO NOTHING
            """)

            await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "from_status": (
                        transition.from_status.value if transition.from_status else None
                    ),
                    "to_status": transition.to_status.value,
                    "reason": (
                        transition.reason if hasattr(transition, "reason") else None
                    ),
                    "transitioned_at": transition.triggered_at,
                    "metadata": json.dumps({}),
                },
            )

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

    def _row_to_case(
        self,
        row,
        hypotheses_data: builtins.list[dict],
        solutions_data: builtins.list[dict],
        uploaded_files_data: builtins.list[dict],
        messages_data: builtins.list[dict] | None = None,
    ) -> Case:
        """Reconstruct Case domain object from database row."""
        # Parse JSON columns with backward compatibility for legacy schema
        inquiry_data = json.loads(row.inquiry) if row.inquiry else {}
        inquiry_data = self._convert_legacy_inquiry_data(inquiry_data)
        inquiry = InquiryData(**inquiry_data) if inquiry_data else InquiryData()
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
        path_selection = (
            PathSelection(**json.loads(row.path_selection))
            if row.path_selection
            else None
        )
        escalation_state = (
            EscalationState(**json.loads(row.escalation_state))
            if row.escalation_state
            else None
        )
        documentation = (
            DocumentationData(**json.loads(row.documentation))
            if row.documentation
            else DocumentationData()
        )
        progress = (
            InvestigationProgress(**json.loads(row.progress))
            if row.progress
            else InvestigationProgress()
        )

        # Convert loaded data to domain objects
        hypotheses_dict = (
            {h["hypothesis_id"]: Hypothesis(**h) for h in hypotheses_data}
            if hypotheses_data
            else {}
        )

        solutions_list = (
            [Solution(**s) for s in solutions_data] if solutions_data else []
        )

        uploaded_files = (
            [UploadedFile(**f) for f in uploaded_files_data]
            if uploaded_files_data
            else []
        )

        # Parse metadata to extract stored fields
        metadata = json.loads(row.metadata) if row.metadata else {}

        # Build case_data dict conditionally to allow Pydantic to apply field defaults
        # Only include optional fields if they have actual values from database
        case_data = {
            "case_id": row.case_id,
            "user_id": row.user_id,
            "organization_id": row.organization_id,  # Required field, must be NOT NULL in DB
            "title": row.title,
            "status": CaseStatus(row.status),
            "status_history": [],
            "closure_reason": metadata.get("closure_reason"),
            "progress": progress,
            "current_turn": metadata.get("current_turn", 0),
            "turns_without_progress": metadata.get("turns_without_progress", 0),
            "message_count": metadata.get("message_count", 0),
            "turn_history": [],
            "path_selection": path_selection,
            "inquiry": inquiry,
            "problem_verification": problem_verification,
            "uploaded_files": uploaded_files,
            "evidence": [],  # Loaded separately
            "hypotheses": hypotheses_dict,
            "solutions": solutions_list,
            "messages": messages_data if messages_data else [],
            "working_conclusion": working_conclusion,
            "root_cause_conclusion": root_cause_conclusion,
            "escalation_state": escalation_state,
            "documentation": documentation,
            # metadata field removed as it is not part of Case model
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

        # Backward compatibility: Fix description for old INVESTIGATING cases
        # Old cases may be in INVESTIGATING status with empty description
        description = row.description if hasattr(row, "description") else ""
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

        # Only add optional fields if they exist and have values in database
        # This allows Pydantic to apply its own defaults for missing fields

        if description:
            case_data["description"] = description

        if hasattr(row, "investigation_strategy") and row.investigation_strategy:
            case_data["investigation_strategy"] = InvestigationStrategy(
                row.investigation_strategy
            )

        if hasattr(row, "last_activity_at") and row.last_activity_at:
            case_data["last_activity_at"] = row.last_activity_at

        # Extract dates from metadata if not on row
        resolved_at_val = metadata.get("resolved_at") or (
            row.resolved_at if hasattr(row, "resolved_at") else None
        )
        if resolved_at_val:
            case_data["resolved_at"] = resolved_at_val

        closed_at_val = metadata.get("closed_at") or (
            row.closed_at if hasattr(row, "closed_at") else None
        )
        if closed_at_val:
            case_data["closed_at"] = closed_at_val

        return Case(**case_data)

    # ========================================================================
    # Report Operations (SQLite-compatible)
    # ========================================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report to reports table (SQLite-compatible)."""

        if report.is_current:
            unmark_query = text("""
                UPDATE reports
                SET is_current = 0, updated_at = datetime('now')
                WHERE case_id = :case_id
                  AND report_type = :report_type
                  AND is_current = 1
            """)
            await self.db.execute(
                unmark_query,
                {"case_id": report.case_id, "report_type": report.report_type.value},
            )

        metadata_json = (
            json.dumps(report.metadata.model_dump()) if report.metadata else "{}"
        )

        # SQLite-compatible: no type casts
        insert_query = text("""
            INSERT INTO reports (
                report_id, case_id, report_type, version, is_current,
                linked_to_closure, title, content, format,
                generation_status, generation_time_ms, metadata,
                generated_at, updated_at
            ) VALUES (
                :report_id, :case_id, :report_type, :version, :is_current,
                :linked_to_closure, :title, :content, :format,
                :generation_status, :generation_time_ms, :metadata,
                :generated_at, :updated_at
            )
            ON CONFLICT (report_id) DO UPDATE SET
                version = EXCLUDED.version,
                is_current = EXCLUDED.is_current,
                linked_to_closure = EXCLUDED.linked_to_closure,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                format = EXCLUDED.format,
                generation_status = EXCLUDED.generation_status,
                generation_time_ms = EXCLUDED.generation_time_ms,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
        """)

        now = datetime.now(UTC)
        generated_at = (
            datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))
            if isinstance(report.generated_at, str)
            else now
        )
        updated_at = (
            datetime.fromisoformat(report.updated_at.replace("Z", "+00:00"))
            if report.updated_at and isinstance(report.updated_at, str)
            else generated_at
        )

        await self.db.execute(
            insert_query,
            {
                "report_id": report.report_id,
                "case_id": report.case_id,
                "report_type": report.report_type.value,
                "version": report.version,
                "is_current": 1 if report.is_current else 0,
                "linked_to_closure": 1 if report.linked_to_closure else 0,
                "title": report.title,
                "content": report.content,
                "format": report.format,
                "generation_status": report.generation_status.value,
                "generation_time_ms": report.generation_time_ms,
                "metadata": metadata_json,
                "generated_at": generated_at.isoformat(),
                "updated_at": updated_at.isoformat(),
            },
        )

        await self.db.commit()
        return report

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Get report by ID from SQLite."""

        query = text("""
            SELECT
                report_id, case_id, report_type, version, is_current,
                linked_to_closure, title, content, format,
                generation_status, generation_time_ms, metadata,
                generated_at, updated_at
            FROM reports
            WHERE report_id = :report_id
        """)

        result = await self.db.execute(query, {"report_id": report_id})
        row = result.fetchone()

        if not row:
            return None

        return self._row_to_report(row)

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> builtins.list["CaseReport"]:
        """Get reports for a case with optional filtering."""
        conditions = ["case_id = :case_id"]
        params = {"case_id": case_id}

        if report_type:
            conditions.append("report_type = :report_type")
            params["report_type"] = report_type.value

        if only_current or not include_history:
            conditions.append("is_current = 1")

        where_clause = " AND ".join(conditions)

        query = text(f"""
            SELECT
                report_id, case_id, report_type, version, is_current,
                linked_to_closure, title, content, format,
                generation_status, generation_time_ms, metadata,
                generated_at, updated_at
            FROM reports
            WHERE {where_clause}
            ORDER BY report_type, version DESC
        """)

        result = await self.db.execute(query, params)
        rows = result.fetchall()

        return [self._row_to_report(row) for row in rows]

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update report in SQLite."""
        if report.is_current:
            unmark_query = text("""
                UPDATE reports
                SET is_current = 0, updated_at = datetime('now')
                WHERE case_id = :case_id
                  AND report_type = :report_type
                  AND report_id != :report_id
                  AND is_current = 1
            """)
            await self.db.execute(
                unmark_query,
                {
                    "case_id": report.case_id,
                    "report_type": report.report_type.value,
                    "report_id": report.report_id,
                },
            )

        metadata_json = (
            json.dumps(report.metadata.model_dump()) if report.metadata else "{}"
        )
        now = datetime.now(UTC)
        updated_at = (
            datetime.fromisoformat(report.updated_at.replace("Z", "+00:00"))
            if report.updated_at and isinstance(report.updated_at, str)
            else now
        )

        update_query = text("""
            UPDATE reports
            SET version = :version,
                is_current = :is_current,
                linked_to_closure = :linked_to_closure,
                title = :title,
                content = :content,
                format = :format,
                generation_status = :generation_status,
                generation_time_ms = :generation_time_ms,
                metadata = :metadata,
                updated_at = :updated_at
            WHERE report_id = :report_id
        """)

        result = await self.db.execute(
            update_query,
            {
                "report_id": report.report_id,
                "version": report.version,
                "is_current": 1 if report.is_current else 0,
                "linked_to_closure": 1 if report.linked_to_closure else 0,
                "title": report.title,
                "content": report.content,
                "format": report.format,
                "generation_status": report.generation_status.value,
                "generation_time_ms": report.generation_time_ms,
                "metadata": metadata_json,
                "updated_at": updated_at.isoformat(),
            },
        )

        await self.db.commit()

        if result.rowcount == 0:
            raise RepositoryException(f"Report {report.report_id} not found")

        return report

    async def delete_report(self, report_id: str) -> bool:
        """Delete report from SQLite."""
        delete_query = text("DELETE FROM reports WHERE report_id = :report_id")
        result = await self.db.execute(delete_query, {"report_id": report_id})
        await self.db.commit()
        return result.rowcount > 0

    def _row_to_report(self, row) -> "CaseReport":
        """Convert database row to CaseReport domain object."""
        from faultmaven.utils.serialization import to_json_compatible

        metadata = None
        if row.metadata and row.metadata != "{}":
            try:
                metadata_dict = (
                    json.loads(row.metadata)
                    if isinstance(row.metadata, str)
                    else row.metadata
                )
                if metadata_dict:
                    metadata = RunbookMetadata(**metadata_dict)
            except Exception:
                pass

        # Handle timestamps (SQLite stores as strings)
        if row.generated_at:
            if isinstance(row.generated_at, str):
                generated_at = row.generated_at
            else:
                gen_dt = row.generated_at
                if gen_dt.tzinfo is None:
                    gen_dt = gen_dt.replace(tzinfo=UTC)
                generated_at = to_json_compatible(gen_dt)
        else:
            generated_at = to_json_compatible(datetime.now(UTC))

        if row.updated_at:
            if isinstance(row.updated_at, str):
                updated_at = row.updated_at
            else:
                upd_dt = row.updated_at
                if upd_dt.tzinfo is None:
                    upd_dt = upd_dt.replace(tzinfo=UTC)
                updated_at = to_json_compatible(upd_dt)
        else:
            updated_at = generated_at

        return CaseReport(
            report_id=row.report_id,
            case_id=row.case_id,
            report_type=ReportType(row.report_type),
            version=row.version,
            is_current=bool(row.is_current),
            linked_to_closure=bool(row.linked_to_closure),
            title=row.title,
            content=row.content,
            format=row.format,
            generation_status=ReportStatus(row.generation_status),
            generation_time_ms=row.generation_time_ms,
            generated_at=generated_at,
            updated_at=updated_at,
            metadata=metadata,
        )

    # ============================================================
    # Stub Methods (Not implemented - same as PostgreSQL version)
    # ============================================================

    async def share_case(
        self,
        case_id: str,
        target_user_id: str,
        role: str,
        sharer_user_id: str | None = None,
    ) -> bool:
        """Share a case with another user (stub - not implemented for SQLite)."""
        raise NotImplementedError(
            "share_case not implemented for SQLite (requires stored procedures)"
        )

    async def unshare_case(
        self, case_id: str, user_id: str, unsharer_user_id: str | None = None
    ) -> bool:
        """Unshare a case (stub - not implemented for SQLite)."""
        raise NotImplementedError("unshare_case not implemented for SQLite")

    async def get_case_participants(
        self, case_id: str
    ) -> builtins.list[dict[str, Any]]:
        """Get case participants (stub)."""
        raise NotImplementedError("get_case_participants not implemented for SQLite")

    async def create_standalone_evidence(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: str,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> Any:
        raise NotImplementedError(
            "create_standalone_evidence not implemented for SQLite"
        )

    async def get_standalone_evidence(self, evidence_id: str) -> Any | None:
        raise NotImplementedError("get_standalone_evidence not implemented for SQLite")

    async def list_standalone_evidence(
        self, filters: Any
    ) -> tuple[builtins.list[Any], int]:
        raise NotImplementedError("list_standalone_evidence not implemented for SQLite")

    async def delete_standalone_evidence(self, evidence_id: str) -> bool:
        raise NotImplementedError(
            "delete_standalone_evidence not implemented for SQLite"
        )

    async def link_standalone_evidence_to_case(
        self, evidence_id: str, case_id: str
    ) -> Any | None:
        raise NotImplementedError(
            "link_standalone_evidence_to_case not implemented for SQLite"
        )

    async def create_agent_execution(self, execution: Any) -> Any:
        raise NotImplementedError("create_agent_execution not implemented for SQLite")

    async def get_agent_execution(self, execution_id: str) -> Any | None:
        raise NotImplementedError("get_agent_execution not implemented for SQLite")

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: str | None = None,
        agent_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
        raise NotImplementedError(
            "list_agent_executions_by_case not implemented for SQLite"
        )

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
        raise NotImplementedError(
            "list_agent_executions_by_session not implemented for SQLite"
        )

    async def update_agent_execution(self, execution: Any) -> Any:
        raise NotImplementedError("update_agent_execution not implemented for SQLite")

    async def delete_agent_execution(self, execution_id: str) -> bool:
        raise NotImplementedError("delete_agent_execution not implemented for SQLite")

    async def create_agent_tool_call(self, tool_call: Any) -> Any:
        raise NotImplementedError("create_agent_tool_call not implemented for SQLite")

    async def update_agent_tool_call(self, tool_call: Any) -> Any:
        raise NotImplementedError("update_agent_tool_call not implemented for SQLite")

    async def get_agent_tool_calls_for_execution(
        self, execution_id: str
    ) -> builtins.list[Any]:
        raise NotImplementedError(
            "get_agent_tool_calls_for_execution not implemented for SQLite"
        )

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        raise NotImplementedError(
            "count_agent_executions_by_case not implemented for SQLite"
        )

    async def get_latest_agent_execution(
        self, case_id: str, agent_type: str | None = None
    ) -> Any | None:
        raise NotImplementedError(
            "get_latest_agent_execution not implemented for SQLite"
        )


class RepositoryException(Exception):
    """Exception raised for repository errors."""

    pass
