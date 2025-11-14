"""PostgreSQL Hybrid Case Repository - Production Implementation.

This module implements the CaseRepository interface using the hybrid normalized schema:
- 10 normalized tables for high-cardinality data (evidence, hypotheses, solutions, messages)
- JSONB columns in cases table for low-cardinality flexible data
- References: docs/architecture/case-storage-design.md
- Migration: migrations/001_initial_hybrid_schema.sql

Architecture:
    cases (main table)
    ├── evidence (1:N normalized table)
    ├── hypotheses (1:N normalized table)
    ├── solutions (1:N normalized table)
    ├── case_messages (1:N normalized table)
    ├── uploaded_files (1:N normalized table)
    ├── case_status_transitions (1:N normalized table)
    ├── case_tags (M:N normalized table)
    └── agent_tool_calls (1:N normalized table)
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.models.case import (
    Case,
    CaseStatus,
    InvestigationProgress,
    TurnProgress,
    UploadedFile,
    Evidence,
    Hypothesis,
    Solution,
    ConsultingData,
    ProblemVerification,
    WorkingConclusion,
    RootCauseConclusion,
    DegradedMode,
    EscalationState,
    DocumentationData,
    PathSelection,
    CaseStatusTransition,
)


class PostgreSQLHybridCaseRepository(CaseRepository):
    """
    PostgreSQL repository using hybrid normalized schema.

    Design Philosophy:
    - Normalize what you query (evidence, hypotheses, solutions, messages)
    - Embed what you don't (consulting, conclusions, progress)

    Performance Characteristics:
    - Case load: ~10ms (single query + JOINs)
    - Evidence filtering: ~5ms (indexed queries on normalized table)
    - Search: ~15ms (full-text search on preprocessed_content)
    - Hypothesis tracking: ~3ms (status index lookup)
    """

    def __init__(self, db_session: AsyncSession):
        """
        Initialize repository with SQLAlchemy async session.

        Args:
            db_session: SQLAlchemy AsyncSession for database operations
        """
        self.db = db_session

    # ========================================================================
    # Core CRUD Operations
    # ========================================================================

    async def save(self, case: Case) -> Case:
        """
        Save case using hybrid schema with transactions.

        Strategy:
        1. Upsert cases table (main record + JSONB)
        2. Upsert normalized tables (evidence, hypotheses, solutions)
        3. Append-only tables (messages, status_transitions)

        Args:
            case: Case domain object

        Returns:
            Saved case with updated timestamps

        Raises:
            RepositoryException: If save fails
        """
        try:
            # Update timestamp
            case.updated_at = datetime.now(timezone.utc)

            # Start transaction
            async with self.db.begin():
                # 1. Upsert main cases table
                await self._upsert_case_record(case)

                # 2. Upsert evidence (normalized table)
                await self._upsert_evidence(case.case_id, case.evidence)

                # 3. Upsert hypotheses (normalized table)
                await self._upsert_hypotheses(case.case_id, case.hypotheses)

                # 4. Upsert solutions (normalized table)
                await self._upsert_solutions(case.case_id, case.solutions)

                # 5. Upsert uploaded_files (normalized table)
                await self._upsert_uploaded_files(case.case_id, case.uploaded_files)

                # 6. Append status transitions (append-only)
                if case.status_history:
                    await self._append_status_transitions(case.case_id, case.status_history)

                await self.db.commit()

            return case

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to save case {case.case_id}: {e}") from e

    async def get(self, case_id: str) -> Optional[Case]:
        """
        Retrieve case by ID using JOINs for normalized tables.

        Performance: ~10ms (single query with LEFT JOINs)

        Args:
            case_id: Case identifier

        Returns:
            Case if found, None otherwise
        """
        try:
            # Main query with LEFT JOINs for normalized tables
            query = text("""
                SELECT
                    c.*,

                    -- Evidence (aggregated as JSON)
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'evidence_id', e.evidence_id,
                            'category', e.category,
                            'summary', e.summary,
                            'preprocessed_content', e.preprocessed_content,
                            'content_ref', e.content_ref,
                            'file_size', e.file_size,
                            'filename', e.filename,
                            'upload_timestamp', e.upload_timestamp,
                            'metadata', e.metadata
                        )) FILTER (WHERE e.evidence_id IS NOT NULL),
                        '[]'::json
                    ) as evidence_data,

                    -- Hypotheses (aggregated as JSON)
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'hypothesis_id', h.hypothesis_id,
                            'description', h.description,
                            'status', h.status,
                            'confidence_score', h.confidence_score,
                            'supporting_evidence_ids', h.supporting_evidence_ids,
                            'validation_result', h.validation_result,
                            'validation_timestamp', h.validation_timestamp,
                            'proposed_at', h.proposed_at,
                            'updated_at', h.updated_at,
                            'metadata', h.metadata
                        )) FILTER (WHERE h.hypothesis_id IS NOT NULL),
                        '[]'::json
                    ) as hypotheses_data,

                    -- Solutions (aggregated as JSON)
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'solution_id', s.solution_id,
                            'description', s.description,
                            'status', s.status,
                            'implementation_steps', s.implementation_steps,
                            'risk_level', s.risk_level,
                            'estimated_effort', s.estimated_effort,
                            'verification_result', s.verification_result,
                            'verification_timestamp', s.verification_timestamp,
                            'proposed_at', s.proposed_at,
                            'implemented_at', s.implemented_at,
                            'updated_at', s.updated_at,
                            'metadata', s.metadata
                        )) FILTER (WHERE s.solution_id IS NOT NULL),
                        '[]'::json
                    ) as solutions_data,

                    -- Uploaded Files (aggregated as JSON - matches UploadedFile Pydantic model)
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'file_id', f.file_id,
                            'filename', f.filename,
                            'size_bytes', f.size_bytes,
                            'data_type', f.data_type,
                            'uploaded_at_turn', f.uploaded_at_turn,
                            'uploaded_at', f.uploaded_at,
                            'source_type', f.source_type,
                            'content_ref', f.content_ref,
                            'preprocessing_summary', f.preprocessing_summary
                        )) FILTER (WHERE f.file_id IS NOT NULL),
                        '[]'::json
                    ) as uploaded_files_data

                FROM cases c
                LEFT JOIN evidence e ON c.case_id = e.case_id
                LEFT JOIN hypotheses h ON c.case_id = h.case_id
                LEFT JOIN solutions s ON c.case_id = s.case_id
                LEFT JOIN uploaded_files f ON c.case_id = f.case_id
                WHERE c.case_id = :case_id
                GROUP BY c.case_id
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            row = result.fetchone()

            if not row:
                return None

            # Reconstruct Case domain object
            return await self._row_to_case(row)

        except Exception as e:
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        status: Optional[CaseStatus] = None,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters and pagination.

        Performance: ~20ms for 50 cases (indexed queries)

        Args:
            user_id: Filter by user
            organization_id: Filter by organization
            status: Filter by status
            limit: Maximum results
            offset: Pagination offset

        Returns:
            Tuple of (cases, total_count)
        """
        try:
            # Build WHERE clause dynamically
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

            # List query (simplified - just get case IDs, then fetch full cases)
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
            for case_id in case_ids:
                case = await self.get(case_id)
                if case:
                    cases.append(case)

            return cases, total_count

        except Exception as e:
            raise RepositoryException(f"Failed to list cases: {e}") from e

    async def delete(self, case_id: str) -> bool:
        """
        Delete case by ID (cascades to normalized tables via FK constraints).

        Args:
            case_id: Case identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            query = text("DELETE FROM cases WHERE case_id = :case_id")
            result = await self.db.execute(query, {"case_id": case_id})
            await self.db.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to delete case {case_id}: {e}") from e

    async def share_case(
        self,
        case_id: str,
        target_user_id: str,
        role: str,  # ParticipantRole: owner, collaborator, viewer
        sharer_user_id: Optional[str] = None
    ) -> bool:
        """
        Share a case with another user.

        Uses the SQL function created in migration 002.

        Args:
            case_id: Case identifier
            target_user_id: User to share with
            role: Role to assign (owner, collaborator, viewer)
            sharer_user_id: User performing the share action

        Returns:
            True if case was shared successfully
        """
        try:
            # Use the upsert_case_participant function from migration 002
            query = text("""
                SELECT upsert_case_participant(
                    :case_id,
                    :user_id,
                    :role::participant_role,
                    :added_by
                )
            """)

            await self.db.execute(query, {
                "case_id": case_id,
                "user_id": target_user_id,
                "role": role,
                "added_by": sharer_user_id or target_user_id
            })
            await self.db.commit()

            self.logger.info(
                f"Shared case {case_id} with user {target_user_id} as {role}"
            )
            return True

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to share case {case_id}: {e}") from e

    async def unshare_case(
        self,
        case_id: str,
        user_id: str,
        unsharer_user_id: Optional[str] = None
    ) -> bool:
        """
        Unshare a case from a user.

        Args:
            case_id: Case identifier
            user_id: User to unshare from
            unsharer_user_id: User performing the unshare action

        Returns:
            True if case was unshared successfully
        """
        try:
            # Use the remove_case_participant function from migration 002
            query = text("""
                SELECT remove_case_participant(
                    :case_id,
                    :user_id,
                    :removed_by
                )
            """)

            await self.db.execute(query, {
                "case_id": case_id,
                "user_id": user_id,
                "removed_by": unsharer_user_id or user_id
            })
            await self.db.commit()

            self.logger.info(
                f"Unshared case {case_id} from user {user_id}"
            )
            return True

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to unshare case {case_id}: {e}") from e

    async def get_case_participants(self, case_id: str) -> List[Dict[str, Any]]:
        """
        Get all participants for a case.

        Args:
            case_id: Case identifier

        Returns:
            List of participants with their roles
        """
        try:
            query = text("""
                SELECT user_id, role, added_at, added_by, last_accessed_at
                FROM case_participants
                WHERE case_id = :case_id
                ORDER BY added_at DESC
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            rows = result.fetchall()

            return [
                {
                    "user_id": row.user_id,
                    "role": row.role,
                    "added_at": row.added_at,
                    "added_by": row.added_by,
                    "last_accessed_at": row.last_accessed_at
                }
                for row in rows
            ]

        except Exception as e:
            raise RepositoryException(f"Failed to get participants for case {case_id}: {e}") from e

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20
    ) -> tuple[List[Case], int]:
        """
        Search cases using PostgreSQL full-text search.

        Searches:
        - cases.title
        - cases.consulting->>'initial_description'
        - evidence.preprocessed_content (via JOIN)

        Performance: ~15ms (GIN indexes on tsvector columns)

        Args:
            query: Search query
            user_id: Filter by user
            organization_id: Filter by organization
            limit: Maximum results

        Returns:
            Tuple of (cases, total_count)
        """
        try:
            # Build WHERE clause
            where_clauses = ["(to_tsvector('english', c.title || ' ' || COALESCE(c.consulting->>'initial_description', '')) @@ plainto_tsquery('english', :query) OR e.preprocessed_content_fts @@ plainto_tsquery('english', :query))"]
            params = {"query": query, "limit": limit}

            if user_id:
                where_clauses.append("c.user_id = :user_id")
                params["user_id"] = user_id

            if organization_id:
                where_clauses.append("c.organization_id = :organization_id")
                params["organization_id"] = organization_id

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Search query with relevance ranking
            search_query = text(f"""
                SELECT DISTINCT c.case_id,
                    ts_rank(to_tsvector('english', c.title), plainto_tsquery('english', :query)) as rank
                FROM cases c
                LEFT JOIN evidence e ON c.case_id = e.case_id
                {where_sql}
                ORDER BY rank DESC, c.updated_at DESC
                LIMIT :limit
            """)

            result = await self.db.execute(search_query, params)
            case_ids = [row[0] for row in result.fetchall()]

            # Fetch full cases
            cases = []
            for case_id in case_ids:
                case = await self.get(case_id)
                if case:
                    cases.append(case)

            return cases, len(cases)

        except Exception as e:
            raise RepositoryException(f"Failed to search cases: {e}") from e

    # ========================================================================
    # Message Operations (Normalized Table)
    # ========================================================================

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """
        Add message to case_messages table.

        Args:
            case_id: Case identifier
            message_dict: Message data (role, content, metadata)

        Returns:
            True if added successfully
        """
        try:
            message_id = message_dict.get('message_id', f"msg_{uuid4().hex[:16]}")

            query = text("""
                INSERT INTO case_messages (message_id, case_id, role, content, metadata)
                VALUES (:message_id, :case_id, :role, :content, :metadata::jsonb)
            """)

            await self.db.execute(query, {
                "message_id": message_id,
                "case_id": case_id,
                "role": message_dict.get('role', 'user'),
                "content": message_dict.get('content', ''),
                "metadata": json.dumps(message_dict.get('metadata', {}))
            })
            await self.db.commit()

            return True

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to add message to case {case_id}: {e}") from e

    async def get_messages(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[dict]:
        """
        Get messages for case with pagination.

        Args:
            case_id: Case identifier
            limit: Maximum messages
            offset: Pagination offset

        Returns:
            List of message dictionaries
        """
        try:
            query = text("""
                SELECT message_id, role, content, created_at, metadata
                FROM case_messages
                WHERE case_id = :case_id
                ORDER BY created_at ASC
                LIMIT :limit OFFSET :offset
            """)

            result = await self.db.execute(query, {
                "case_id": case_id,
                "limit": limit,
                "offset": offset
            })

            messages = []
            for row in result.fetchall():
                messages.append({
                    'message_id': row[0],
                    'role': row[1],
                    'content': row[2],
                    'created_at': row[3].isoformat() if row[3] else None,
                    'metadata': row[4] if row[4] else {}
                })

            return messages

        except Exception as e:
            raise RepositoryException(f"Failed to get messages for case {case_id}: {e}") from e

    # ========================================================================
    # Utility Operations
    # ========================================================================

    async def update_activity_timestamp(self, case_id: str) -> bool:
        """
        Update last_activity_at timestamp (efficient partial update).

        Args:
            case_id: Case identifier

        Returns:
            True if updated
        """
        try:
            query = text("""
                UPDATE cases
                SET last_activity_at = NOW()
                WHERE case_id = :case_id
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            await self.db.commit()

            return result.rowcount > 0

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to update activity timestamp for case {case_id}: {e}") from e

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """
        Compute analytics for case from normalized tables.

        Returns:
            Dictionary with analytics data
        """
        try:
            query = text("""
                SELECT
                    COUNT(DISTINCT e.evidence_id) as evidence_count,
                    COUNT(DISTINCT h.hypothesis_id) as hypothesis_count,
                    COUNT(DISTINCT h.hypothesis_id) FILTER (WHERE h.status = 'validated') as validated_hypotheses,
                    COUNT(DISTINCT s.solution_id) as solution_count,
                    COUNT(DISTINCT s.solution_id) FILTER (WHERE s.status = 'implemented') as implemented_solutions,
                    COUNT(DISTINCT m.message_id) as message_count,
                    COUNT(DISTINCT f.file_id) as file_count,
                    SUM(f.size_bytes) as total_file_size
                FROM cases c
                LEFT JOIN evidence e ON c.case_id = e.case_id
                LEFT JOIN hypotheses h ON c.case_id = h.case_id
                LEFT JOIN solutions s ON c.case_id = s.case_id
                LEFT JOIN case_messages m ON c.case_id = m.case_id
                LEFT JOIN uploaded_files f ON c.case_id = f.case_id
                WHERE c.case_id = :case_id
                GROUP BY c.case_id
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            row = result.fetchone()

            if not row:
                return {}

            return {
                'evidence_count': row[0] or 0,
                'hypothesis_count': row[1] or 0,
                'validated_hypotheses': row[2] or 0,
                'solution_count': row[3] or 0,
                'implemented_solutions': row[4] or 0,
                'message_count': row[5] or 0,
                'file_count': row[6] or 0,
                'total_file_size': row[7] or 0
            }

        except Exception as e:
            raise RepositoryException(f"Failed to get analytics for case {case_id}: {e}") from e

    async def cleanup_expired(self, max_age_days: int = 90, batch_size: int = 100) -> int:
        """
        Clean up expired/old cases.

        Args:
            max_age_days: Maximum age in days for closed cases
            batch_size: Maximum cases to process

        Returns:
            Number of cases deleted
        """
        try:
            query = text("""
                DELETE FROM cases
                WHERE case_id IN (
                    SELECT case_id
                    FROM cases
                    WHERE status = 'closed'
                    AND closed_at < NOW() - INTERVAL ':max_age_days days'
                    LIMIT :batch_size
                )
            """)

            result = await self.db.execute(query, {
                "max_age_days": max_age_days,
                "batch_size": batch_size
            })
            await self.db.commit()

            return result.rowcount

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to cleanup expired cases: {e}") from e

    # ========================================================================
    # Private Helper Methods
    # ========================================================================

    async def _upsert_case_record(self, case: Case) -> None:
        """Upsert main cases table (JSONB columns for flexible data)."""
        query = text("""
            INSERT INTO cases (
                case_id, user_id, title, status, created_at, updated_at,
                consulting, problem_verification, working_conclusion,
                root_cause_conclusion, path_selection, degraded_mode,
                escalation_state, documentation, progress, metadata
            ) VALUES (
                :case_id, :user_id, :title, :status, :created_at, :updated_at,
                :consulting::jsonb, :problem_verification::jsonb, :working_conclusion::jsonb,
                :root_cause_conclusion::jsonb, :path_selection::jsonb, :degraded_mode::jsonb,
                :escalation_state::jsonb, :documentation::jsonb, :progress::jsonb, :metadata::jsonb
            )
            ON CONFLICT (case_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                consulting = EXCLUDED.consulting,
                problem_verification = EXCLUDED.problem_verification,
                working_conclusion = EXCLUDED.working_conclusion,
                root_cause_conclusion = EXCLUDED.root_cause_conclusion,
                path_selection = EXCLUDED.path_selection,
                degraded_mode = EXCLUDED.degraded_mode,
                escalation_state = EXCLUDED.escalation_state,
                documentation = EXCLUDED.documentation,
                progress = EXCLUDED.progress,
                metadata = EXCLUDED.metadata
        """)

        await self.db.execute(query, {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "title": case.title,
            "status": case.status.value,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "consulting": json.dumps(case.consulting.model_dump()),
            "problem_verification": json.dumps(case.problem_verification.model_dump()) if case.problem_verification else None,
            "working_conclusion": json.dumps(case.working_conclusion.model_dump()) if case.working_conclusion else None,
            "root_cause_conclusion": json.dumps(case.root_cause_conclusion.model_dump()) if case.root_cause_conclusion else None,
            "path_selection": json.dumps(case.path_selection.model_dump()) if case.path_selection else None,
            "degraded_mode": json.dumps(case.degraded_mode.model_dump()) if case.degraded_mode else None,
            "escalation_state": json.dumps(case.escalation_state.model_dump()) if case.escalation_state else None,
            "documentation": json.dumps(case.documentation.model_dump()),
            "progress": json.dumps(case.progress.model_dump()),
            "metadata": json.dumps({})  # Reserved for future use
        })

    async def _upsert_evidence(self, case_id: str, evidence_list: List[Evidence]) -> None:
        """Upsert evidence records (normalized table)."""
        # Delete existing evidence not in current list
        current_ids = [e.evidence_id for e in evidence_list]
        if current_ids:
            delete_query = text("""
                DELETE FROM evidence
                WHERE case_id = :case_id
                AND evidence_id != ALL(:current_ids)
            """)
            await self.db.execute(delete_query, {"case_id": case_id, "current_ids": current_ids})

        # Upsert each evidence record
        for evidence in evidence_list:
            query = text("""
                INSERT INTO evidence (
                    evidence_id, case_id, category, summary, preprocessed_content,
                    content_ref, file_size, filename, upload_timestamp, metadata
                ) VALUES (
                    :evidence_id, :case_id, :category, :summary, :preprocessed_content,
                    :content_ref, :file_size, :filename, :upload_timestamp, :metadata::jsonb
                )
                ON CONFLICT (evidence_id) DO UPDATE SET
                    category = EXCLUDED.category,
                    summary = EXCLUDED.summary,
                    preprocessed_content = EXCLUDED.preprocessed_content,
                    content_ref = EXCLUDED.content_ref,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(query, {
                "evidence_id": evidence.evidence_id,
                "case_id": case_id,
                "category": evidence.data_type,  # Maps to evidence_category enum
                "summary": evidence.summary,
                "preprocessed_content": evidence.preprocessed_content or "",
                "content_ref": evidence.storage_ref,
                "file_size": evidence.file_size,
                "filename": evidence.filename,
                "upload_timestamp": evidence.timestamp,
                "metadata": json.dumps({})  # Reserved
            })

    async def _upsert_hypotheses(self, case_id: str, hypotheses_dict: Dict[str, Hypothesis]) -> None:
        """Upsert hypotheses records (normalized table)."""
        # Delete existing hypotheses not in current dict
        current_ids = list(hypotheses_dict.keys())
        if current_ids:
            delete_query = text("""
                DELETE FROM hypotheses
                WHERE case_id = :case_id
                AND hypothesis_id != ALL(:current_ids)
            """)
            await self.db.execute(delete_query, {"case_id": case_id, "current_ids": current_ids})

        # Upsert each hypothesis
        for hypothesis_id, hypothesis in hypotheses_dict.items():
            query = text("""
                INSERT INTO hypotheses (
                    hypothesis_id, case_id, description, status, confidence_score,
                    supporting_evidence_ids, validation_result, validation_timestamp,
                    proposed_at, updated_at, metadata
                ) VALUES (
                    :hypothesis_id, :case_id, :description, :status, :confidence_score,
                    :supporting_evidence_ids, :validation_result, :validation_timestamp,
                    :proposed_at, :updated_at, :metadata::jsonb
                )
                ON CONFLICT (hypothesis_id) DO UPDATE SET
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    confidence_score = EXCLUDED.confidence_score,
                    supporting_evidence_ids = EXCLUDED.supporting_evidence_ids,
                    validation_result = EXCLUDED.validation_result,
                    validation_timestamp = EXCLUDED.validation_timestamp,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(query, {
                "hypothesis_id": hypothesis_id,
                "case_id": case_id,
                "description": hypothesis.hypothesis,
                "status": "proposed",  # Default status
                "confidence_score": hypothesis.confidence if hasattr(hypothesis, 'confidence') else None,
                "supporting_evidence_ids": hypothesis.evidence if hasattr(hypothesis, 'evidence') else [],
                "validation_result": hypothesis.validation_result if hasattr(hypothesis, 'validation_result') else None,
                "validation_timestamp": hypothesis.validated_at if hasattr(hypothesis, 'validated_at') else None,
                "proposed_at": hypothesis.proposed_at if hasattr(hypothesis, 'proposed_at') else datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "metadata": json.dumps({})
            })

    async def _upsert_solutions(self, case_id: str, solutions_list: List[Solution]) -> None:
        """Upsert solutions records (normalized table)."""
        # Delete existing solutions not in current list
        current_ids = [s.solution_id for s in solutions_list if hasattr(s, 'solution_id')]
        if current_ids:
            delete_query = text("""
                DELETE FROM solutions
                WHERE case_id = :case_id
                AND solution_id != ALL(:current_ids)
            """)
            await self.db.execute(delete_query, {"case_id": case_id, "current_ids": current_ids})

        # Upsert each solution
        for solution in solutions_list:
            solution_id = solution.solution_id if hasattr(solution, 'solution_id') else f"sol_{uuid4().hex[:12]}"

            query = text("""
                INSERT INTO solutions (
                    solution_id, case_id, description, status, implementation_steps,
                    risk_level, estimated_effort, verification_result, verification_timestamp,
                    proposed_at, implemented_at, updated_at, metadata
                ) VALUES (
                    :solution_id, :case_id, :description, :status, :implementation_steps,
                    :risk_level, :estimated_effort, :verification_result, :verification_timestamp,
                    :proposed_at, :implemented_at, :updated_at, :metadata::jsonb
                )
                ON CONFLICT (solution_id) DO UPDATE SET
                    description = EXCLUDED.description,
                    status = EXCLUDED.status,
                    implementation_steps = EXCLUDED.implementation_steps,
                    risk_level = EXCLUDED.risk_level,
                    estimated_effort = EXCLUDED.estimated_effort,
                    verification_result = EXCLUDED.verification_result,
                    verification_timestamp = EXCLUDED.verification_timestamp,
                    implemented_at = EXCLUDED.implemented_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(query, {
                "solution_id": solution_id,
                "case_id": case_id,
                "description": solution.description if hasattr(solution, 'description') else str(solution),
                "status": "proposed",  # Default status
                "implementation_steps": solution.steps if hasattr(solution, 'steps') else [],
                "risk_level": solution.risk_level if hasattr(solution, 'risk_level') else None,
                "estimated_effort": solution.effort if hasattr(solution, 'effort') else None,
                "verification_result": None,
                "verification_timestamp": None,
                "proposed_at": datetime.now(timezone.utc),
                "implemented_at": None,
                "updated_at": datetime.now(timezone.utc),
                "metadata": json.dumps({})
            })

    async def _upsert_uploaded_files(self, case_id: str, files_list: List[UploadedFile]) -> None:
        """Upsert uploaded_files records (normalized table) - matches UploadedFile Pydantic model."""
        # Delete existing files not in current list
        current_ids = [f.file_id for f in files_list]
        if current_ids:
            delete_query = text("""
                DELETE FROM uploaded_files
                WHERE case_id = :case_id
                AND file_id != ALL(:current_ids)
            """)
            await self.db.execute(delete_query, {"case_id": case_id, "current_ids": current_ids})

        # Upsert each file (field names match Pydantic model exactly)
        for file in files_list:
            query = text("""
                INSERT INTO uploaded_files (
                    file_id, case_id, filename, size_bytes, data_type,
                    uploaded_at_turn, uploaded_at, source_type,
                    content_ref, preprocessing_summary, metadata
                ) VALUES (
                    :file_id, :case_id, :filename, :size_bytes, :data_type,
                    :uploaded_at_turn, :uploaded_at, :source_type,
                    :content_ref, :preprocessing_summary, :metadata::jsonb
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

            await self.db.execute(query, {
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
                "metadata": json.dumps({})
            })

    async def _append_status_transitions(self, case_id: str, transitions: List[CaseStatusTransition]) -> None:
        """Append status transitions (append-only audit trail)."""
        for transition in transitions:
            query = text("""
                INSERT INTO case_status_transitions (
                    case_id, from_status, to_status, reason, transitioned_at, metadata
                ) VALUES (
                    :case_id, :from_status, :to_status, :reason, :transitioned_at, :metadata::jsonb
                )
                ON CONFLICT DO NOTHING
            """)

            await self.db.execute(query, {
                "case_id": case_id,
                "from_status": transition.from_status.value if transition.from_status else None,
                "to_status": transition.to_status.value,
                "reason": transition.reason if hasattr(transition, 'reason') else None,
                "transitioned_at": transition.timestamp,
                "metadata": json.dumps({})
            })

    async def _row_to_case(self, row) -> Case:
        """
        Reconstruct Case domain object from database row.

        Args:
            row: Database row from case query with JOINs

        Returns:
            Case domain object
        """
        # Parse JSONB columns
        consulting = ConsultingData(**json.loads(row.consulting)) if row.consulting else ConsultingData()
        problem_verification = ProblemVerification(**json.loads(row.problem_verification)) if row.problem_verification else None
        working_conclusion = WorkingConclusion(**json.loads(row.working_conclusion)) if row.working_conclusion else None
        root_cause_conclusion = RootCauseConclusion(**json.loads(row.root_cause_conclusion)) if row.root_cause_conclusion else None
        path_selection = PathSelection(**json.loads(row.path_selection)) if row.path_selection else None
        degraded_mode = DegradedMode(**json.loads(row.degraded_mode)) if row.degraded_mode else None
        escalation_state = EscalationState(**json.loads(row.escalation_state)) if row.escalation_state else None
        documentation = DocumentationData(**json.loads(row.documentation)) if row.documentation else DocumentationData()
        progress = InvestigationProgress(**json.loads(row.progress)) if row.progress else InvestigationProgress()

        # Parse normalized table data (aggregated as JSON)
        evidence_list = [Evidence(**e) for e in json.loads(row.evidence_data)] if row.evidence_data != '[]' else []
        hypotheses_dict = {h['hypothesis_id']: Hypothesis(**h) for h in json.loads(row.hypotheses_data)} if row.hypotheses_data != '[]' else {}
        solutions_list = [Solution(**s) for s in json.loads(row.solutions_data)] if row.solutions_data != '[]' else []
        uploaded_files = [UploadedFile(**f) for f in json.loads(row.uploaded_files_data)] if row.uploaded_files_data != '[]' else []

        # Reconstruct Case
        return Case(
            case_id=row.case_id,
            user_id=row.user_id,
            organization_id=row.organization_id if hasattr(row, 'organization_id') else None,
            title=row.title,
            description=None,  # Not stored in hybrid schema
            status=CaseStatus(row.status),
            status_history=[],  # Load separately if needed
            closure_reason=None,

            # Progress
            progress=progress,
            current_turn=0,  # Not stored in hybrid schema
            turns_without_progress=0,
            turn_history=[],

            # Path and strategy
            path_selection=path_selection,
            investigation_strategy=None,  # Not stored

            # Problem context
            consulting=consulting,
            problem_verification=problem_verification,

            # Investigation data (from normalized tables)
            uploaded_files=uploaded_files,
            evidence=evidence_list,
            hypotheses=hypotheses_dict,
            solutions=solutions_list,

            # Conclusions
            working_conclusion=working_conclusion,
            root_cause_conclusion=root_cause_conclusion,

            # Special states
            degraded_mode=degraded_mode,
            escalation_state=escalation_state,

            # Documentation
            documentation=documentation,

            # Timestamps
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_activity_at=row.last_activity_at if hasattr(row, 'last_activity_at') else row.updated_at,
            resolved_at=row.resolved_at if hasattr(row, 'resolved_at') else None,
            closed_at=row.closed_at if hasattr(row, 'closed_at') else None,
        )


class RepositoryException(Exception):
    """Exception raised for repository errors."""
    pass
