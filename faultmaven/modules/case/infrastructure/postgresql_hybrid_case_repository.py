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
    ├── case_actions (1:N normalized table)
    ├── case_tags (M:N normalized table)
    └── agent_tool_calls (1:N normalized table)
"""

import builtins
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.domain.models import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseEntity,
    CaseState,
    DocumentationData,
    EntityType,
    EscalationState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisEvidenceLink,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    NeedPriority,
    NeedPurpose,
    NeedState,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint

# Case-owned models (per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.report import CaseReport, ReportType
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    _agent_execution_mappers as agent_mappers,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

# TYPE_CHECKING imports not needed - models imported directly above

logger = logging.getLogger(__name__)


def _serialize_tags(tags: Optional[List[str]]) -> Optional[List[str]]:
    """Serialize Evidence.tags for the PG ``tags`` column.

    PostgreSQL stores tags as ``TEXT[]`` (see ``TagsArray`` in
    infrastructure/persistence/models.py). asyncpg / psycopg bind a
    Python list directly to that array, so the serializer is just an
    ``[] → None`` normalization. Pydantic's ``_no_commas_in_tags``
    validator already rejects values containing commas — same rule
    SQLite needs for its TEXT round-trip.
    """
    if not tags:
        return None
    return list(tags)


def _deserialize_tags(value: Any) -> List[str]:
    """Inverse of ``_serialize_tags``.

    On PG, asyncpg returns the column as ``list[str]``. Older rows
    written before the schema rewrite may surface as a comma-separated
    TEXT (the SQLite shape) — accept both for robustness.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(t) for t in value if t]
    if isinstance(value, str):
        return [t for t in value.split(",") if t]
    return []


_STANCE_TO_RELATIONSHIP: Dict[EvidenceStance, str] = {
    EvidenceStance.SUPPORTS: "supports",
    EvidenceStance.REFUTES: "refutes",
    # The hypothesis_evidence CHECK allows ('supports', 'refutes', 'related').
    # Domain NEUTRAL maps to 'related' (closest neutral-not-irrelevant slot).
    EvidenceStance.NEUTRAL: "related",
}

_RELATIONSHIP_TO_STANCE: Dict[str, EvidenceStance] = {
    "supports": EvidenceStance.SUPPORTS,
    "refutes": EvidenceStance.REFUTES,
    "related": EvidenceStance.NEUTRAL,
}


def _pg_row_to_case_entity(row: Any) -> CaseEntity:
    """Build a domain ``CaseEntity`` from a SELECT row.

    Same column order as the SQLite helper in
    ``sqlite_case_repository._row_to_case_entity``.
    """
    entity_type_str = row[1]
    try:
        entity_type = EntityType(entity_type_str)
    except ValueError:
        entity_type = next(iter(EntityType))
    return CaseEntity(
        case_id=str(row[0]),
        entity_type=entity_type,
        entity_value=str(row[2]),
        evidence_id=str(row[3]),
        mention_count=int(row[4]) if row[4] is not None else 1,
        in_error_context=bool(row[5]),
        first_seen_ts=row[6],
    )


class PostgreSQLHybridCaseRepository(CaseRepository):
    """
    PostgreSQL repository using hybrid normalized schema.

    Design Philosophy:
    - Normalize what you query (evidence, hypotheses, solutions, messages)
    - Embed what you don't (inquiry, conclusions, progress)

    Performance Characteristics:
    - Case load: ~10ms (single query + JOINs)
    - Evidence filtering: ~5ms (indexed queries on normalized table)
    - Search: ~15ms (tsvector search on cases.title + inquiry text)
    - Hypothesis tracking: ~3ms (state index lookup)
    """

    def __init__(
        self,
        db_session: AsyncSession,
    ):
        """
        Initialize repository with SQLAlchemy async session.

        Args:
            db_session: SQLAlchemy AsyncSession for database operations

        Note: Evidence is now owned by Case module per module-organization-design.md.
              Evidence operations are handled directly by this repository.
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
        3. Append-only tables (messages, case_actions)

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

            # P3 chokepoint: refresh denormalized disposition_eligibility
            # from current case content. Same site as the SQLite + in-memory
            # repositories so the column stays in sync regardless of
            # backend, without per-mutation-site write burden.
            from faultmaven.core.investigation.terminal_transitions import (
                derive_disposition_eligibility,
            )

            case.disposition_eligibility = derive_disposition_eligibility(case)

            organization_id = case.organization_id

            await self._upsert_case_record(case)
            # Post-010: evidence.source_file_id is a real FK to
            # uploaded_files.file_id, so files must exist before any
            # evidence row that references them gets inserted.
            await self._upsert_uploaded_files(
                case.case_id, case.uploaded_files, organization_id
            )
            await self._upsert_evidence(case.case_id, case.evidence, organization_id)
            # Needs and the fulfillment junction must run AFTER evidence
            # so the junction FK to evidence.evidence_id is satisfied.
            await self._upsert_evidence_needs(
                case.case_id,
                case.evidence_needs,
                organization_id,
                case.current_turn,
            )
            await self._upsert_hypotheses(
                case.case_id, case.hypotheses, organization_id
            )
            await self._upsert_solutions(case.case_id, case.solutions, organization_id)
            await self._upsert_messages(case.case_id, case.messages, organization_id)
            if case.action_history:
                await self._append_case_actions(
                    case.case_id, case.action_history, organization_id
                )

            await self.db.commit()
            return case

        except StaleCaseException:
            # OCC mismatch — propagate unwrapped so callers can retry or
            # surface 409 without unwrapping a generic RepositoryException.
            await self.db.rollback()
            raise
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to save case {case.case_id}: {e}") from e

    async def get(self, case_id: str) -> Optional[Case]:
        """
        Retrieve case by ID using JOINs for normalized tables.

        Hypotheses, solutions, uploaded_files and case_messages are
        aggregated to JSON in the same query as the parent ``cases`` row.
        Evidence is loaded separately (via ``_load_evidence_for_case``)
        because the row → ``Evidence`` reconstruction is non-trivial and
        easier to keep in one place than to inline into a JSON aggregate.

        Hypotheses' evidence linkage lives in the ``hypothesis_evidence``
        junction table (the ``hypotheses.evidence_links`` JSON blob is
        gone). The links are loaded after the parent fetch.
        """
        try:
            query = text("""
                SELECT
                    c.*,

                    -- Hypotheses (aggregated as JSON; evidence_links
                    -- column is gone — junction-table data is hydrated
                    -- separately by _load_hypothesis_evidence_links).
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'hypothesis_id', h.hypothesis_id,
                            'statement', h.statement,
                            'state', h.state,
                            'likelihood', h.likelihood,
                            'initial_likelihood', h.initial_likelihood,
                            'generated_at_turn', h.generated_at_turn,
                            'last_updated_turn', h.last_updated_turn,
                            'last_progress_at_turn', h.last_progress_at_turn,
                            'iterations_without_progress', h.iterations_without_progress,
                            'category', h.category,
                            'generation_mode', h.generation_mode,
                            'rationale', h.rationale,
                            'retirement_reason', h.retirement_reason,
                            'refutation_reason', h.refutation_reason,
                            'tested_at', h.tested_at,
                            'concluded_at', h.concluded_at,
                            'proposed_at', h.proposed_at,
                            'updated_at', h.updated_at,
                            'metadata', h.metadata
                        )) FILTER (WHERE h.hypothesis_id IS NOT NULL),
                        '[]'::json
                    ) as hypotheses_data,

                    -- Solutions (aggregated as JSON). Keys mirror Pydantic
                    -- Solution field names so Solution(**s) reconstruction
                    -- in _row_to_case is direct (no name translation).
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'solution_id', s.solution_id,
                            'solution_type', s.solution_type,
                            'title', s.title,
                            'immediate_action', s.immediate_action,
                            'longterm_fix', s.longterm_fix,
                            'implementation_steps', s.implementation_steps,
                            'commands', s.commands,
                            'risks', s.risks,
                            'proposed_at', s.proposed_at,
                            'proposed_by', s.proposed_by,
                            'applied_at', s.applied_at,
                            'applied_by', s.applied_by,
                            'verified_at', s.verified_at,
                            'verification_method', s.verification_method,
                            'verification_evidence_id', s.verification_evidence_id,
                            'effectiveness', s.effectiveness
                        )) FILTER (WHERE s.solution_id IS NOT NULL),
                        '[]'::json
                    ) as solutions_data,

                    -- Uploaded files — preprocessing artifacts (summary,
                    -- structural_index, data_type, coverage_*) ride on
                    -- this row and must be hydrated here so the agent
                    -- sees them on subsequent turns.
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'file_id', f.file_id,
                            'filename', f.filename,
                            'size_bytes', f.size_bytes,
                            'content_type', f.content_type,
                            'content_hash', f.content_hash,
                            'storage_ref', f.storage_ref,
                            'upload_source', f.upload_source,
                            'uploaded_at_turn', f.uploaded_at_turn,
                            'uploaded_at', f.uploaded_at,
                            'uploaded_by', f.uploaded_by,
                            'summary', f.summary,
                            'structural_index', f.structural_index,
                            'data_type', f.data_type,
                            'coverage_start_ts', f.coverage_start_ts,
                            'coverage_end_ts', f.coverage_end_ts
                        )) FILTER (WHERE f.file_id IS NOT NULL),
                        '[]'::json
                    ) as uploaded_files_data,

                    -- Case messages (case_messages table; sorted by created_at).
                    COALESCE(
                        json_agg(DISTINCT jsonb_build_object(
                            'message_id', m.message_id,
                            'turn_number', m.turn_number,
                            'role', m.role,
                            'content', m.content,
                            'created_at', m.created_at,
                            'token_count', m.token_count,
                            'metadata', m.metadata
                        )) FILTER (WHERE m.message_id IS NOT NULL),
                        '[]'::json
                    ) as messages_data

                FROM cases c
                LEFT JOIN hypotheses h ON c.case_id = h.case_id
                LEFT JOIN solutions s ON c.case_id = s.case_id
                LEFT JOIN uploaded_files f ON c.case_id = f.case_id
                LEFT JOIN case_messages m ON c.case_id = m.case_id
                WHERE c.case_id = :case_id
                GROUP BY c.case_id
            """)

            result = await self.db.execute(query, {"case_id": case_id})
            row = result.fetchone()

            if not row:
                return None

            # Hydrate junction-table links for every hypothesis on the row.
            hypotheses_payload = (
                row.hypotheses_data
                if isinstance(row.hypotheses_data, list)
                else json.loads(row.hypotheses_data)
            )
            hypothesis_ids = [
                h["hypothesis_id"] for h in hypotheses_payload if h.get("hypothesis_id")
            ]
            links_by_hyp = await self._load_hypothesis_evidence_links(hypothesis_ids)

            case = await self._row_to_case(row, links_by_hyp)

            # Load evidence separately — the Pydantic reconstruction needs
            # column-by-column conversion that doesn't fit cleanly in a
            # JSONB aggregate.
            if case:
                await self._load_evidence_for_case(case)
                await self._load_evidence_needs_for_case(case)

            return case

        except Exception as e:
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    async def _load_evidence_for_case(self, case: Case) -> None:
        """Load investigation evidence from the evidence table.

        Post-010 columns (in this fixed order, consumed positionally by
        ``_row_to_evidence``): ``evidence_id``, ``category``,
        ``source_type``, ``summary``, ``extract``, ``is_primary``,
        ``reliability_score``, ``tags``, ``collected_at_turn``,
        ``source_file_id``, ``vectorized``, ``coverage_start_ts``,
        ``coverage_end_ts``, ``metadata``, ``created_at``,
        ``primary_purpose``, ``analysis``, ``processing_mode``,
        ``advances_milestones``, ``collected_by``.

        File-level metadata (filename, content_hash, content_type, size,
        storage_ref) lives on ``uploaded_files``, reachable via
        ``source_file_id``.
        """
        try:
            query = text("""
                SELECT
                    evidence_id, category, source_type,
                    summary, extract,
                    is_primary, reliability_score, tags,
                    collected_at_turn, source_file_id, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at,
                    primary_purpose, analysis, processing_mode,
                    advances_milestones, collected_by
                FROM evidence
                WHERE case_id = :case_id
                ORDER BY created_at DESC
                LIMIT 1000
                """)
            result = await self.db.execute(query, {"case_id": case.case_id})
            rows = result.fetchall()

            evidence_list = [self._row_to_evidence(row) for row in rows if row]
            case.evidence = [ev for ev in evidence_list if ev is not None]
        except Exception as e:
            logger.warning(
                "Failed to load evidence for case %s: %s",
                case.case_id,
                e,
            )

    def _row_to_evidence(self, row: Any) -> Optional[Evidence]:
        """Reconstruct a domain ``Evidence`` from a SELECT row.

        Column order: ``evidence_id, category, source_type, summary,
        extract, is_primary, reliability_score, tags, collected_at_turn,
        source_file_id, vectorized, coverage_start_ts, coverage_end_ts,
        metadata, created_at, primary_purpose, analysis, processing_mode,
        advances_milestones, collected_by``.

        Returns ``None`` and logs a warning when reconstruction fails so
        one bad row doesn't blank an entire result set.
        """
        try:
            # Strict category validation — every row is born with a valid
            # 4-category classification.
            category = EvidenceCategory(row[1])

            source_type = EvidenceSourceType(row[2]) if row[2] else None

            metadata_raw = row[13]
            parsed_metadata: Optional[Dict[str, Any]] = None
            if metadata_raw:
                # Postgres JSONB returns a dict directly; legacy TEXT
                # rows arrive as JSON-serialized strings.
                if isinstance(metadata_raw, dict):
                    parsed_metadata = metadata_raw or None
                else:
                    try:
                        parsed = json.loads(metadata_raw)
                        if isinstance(parsed, dict) and parsed:
                            parsed_metadata = parsed
                    except (json.JSONDecodeError, TypeError):
                        parsed_metadata = None

            collected_at = row[14]
            if isinstance(collected_at, str):
                try:
                    collected_at = datetime.fromisoformat(
                        collected_at.replace(" ", "T")
                    )
                except ValueError:
                    collected_at = datetime.now(timezone.utc)
            elif collected_at is None:
                collected_at = datetime.now(timezone.utc)

            # Postgres ARRAY(String) returns a list; the SQLite repo uses
            # comma-encoded TEXT and _deserialize_tags. Handle both shapes.
            advances_raw = row[18] if len(row) > 18 else None
            if isinstance(advances_raw, list):
                advances_milestones = list(advances_raw)
            else:
                advances_milestones = _deserialize_tags(advances_raw)

            tags_raw = row[7]
            if isinstance(tags_raw, list):
                tags = list(tags_raw)
            else:
                tags = _deserialize_tags(tags_raw)

            return Evidence(
                evidence_id=str(row[0]),
                category=category,
                primary_purpose=row[15],
                summary=row[3] if row[3] else "Evidence",
                extract=row[4],
                analysis=row[16],
                processing_mode=row[17],
                source_type=source_type,
                source_file_id=row[9],
                is_primary=bool(row[5]),
                reliability_score=(float(row[6]) if row[6] is not None else None),
                tags=tags,
                advances_milestones=advances_milestones,
                collected_by=row[19] or "system",
                collected_at=collected_at,
                collected_at_turn=row[8] if row[8] else 0,
                vectorized=bool(row[10]),
                metadata=parsed_metadata,
                coverage_start_ts=row[11],
                coverage_end_ts=row[12],
            )
        except Exception as ev_err:  # noqa: BLE001
            logger.warning("Failed to load evidence %s: %s", row[0], ev_err)
            return None

    async def _load_evidence_needs_for_case(self, case: Case) -> None:
        """Load evidence-need rows + their fulfillment junctions.

        Mirrors the SQLite repo's ``_load_evidence_needs_for_case`` —
        same column shape, JSON parsing for ``motivating_hypothesis_ids``,
        and one-shot junction query for fulfillment.
        """
        try:
            need_query = text("""
                SELECT
                    need_id, purpose, request_text, rationale,
                    priority, state,
                    motivating_hypothesis_ids,
                    superseded_reason,
                    created_at_turn, created_at, updated_at
                FROM evidence_needs
                WHERE case_id = :case_id
                ORDER BY created_at ASC, need_id ASC
            """)
            need_rows = (
                await self.db.execute(need_query, {"case_id": case.case_id})
            ).fetchall()

            if not need_rows:
                case.evidence_needs = []
                return

            need_ids = [row[0] for row in need_rows]
            params: Dict[str, Any] = {}
            placeholders = self._bind_ids(params, need_ids)
            junction_query = text(f"""
                SELECT need_id, evidence_id
                FROM evidence_need_fulfillment
                WHERE need_id IN ({placeholders})
            """)
            junction_rows = (await self.db.execute(junction_query, params)).fetchall()

            fulfillments_by_need: Dict[str, builtins.list[str]] = {}
            for nid, eid in junction_rows:
                fulfillments_by_need.setdefault(nid, []).append(eid)

            needs: builtins.list[EvidenceNeed] = []
            for row in need_rows:
                need = self._row_to_evidence_need(
                    row,
                    case_id=case.case_id,
                    fulfilling_evidence_ids=fulfillments_by_need.get(row[0], []),
                )
                if need is not None:
                    needs.append(need)
            case.evidence_needs = needs
        except Exception as e:
            logger.warning(
                "Failed to load evidence_needs for case %s: %s", case.case_id, e
            )

    def _row_to_evidence_need(
        self,
        row: Any,
        *,
        case_id: str,
        fulfilling_evidence_ids: builtins.list[str],
    ) -> Optional[EvidenceNeed]:
        """Reconstruct an ``EvidenceNeed`` from a SELECT row.

        Column order: ``need_id, purpose, request_text, rationale,
        priority, state, motivating_hypothesis_ids (JSONB),
        superseded_reason, created_at_turn, created_at, updated_at``.
        On PG, JSONB is returned as a Python list directly (asyncpg);
        on dialect-compatibility paths a JSON string is also tolerated.
        """
        try:
            motivating_raw = row[6]
            if isinstance(motivating_raw, list):
                motivating = list(motivating_raw)
            elif motivating_raw is None:
                motivating = []
            else:
                try:
                    motivating = json.loads(motivating_raw)
                except (json.JSONDecodeError, TypeError):
                    motivating = []

            return EvidenceNeed(
                need_id=str(row[0]),
                case_id=case_id,
                purpose=NeedPurpose(row[1]),
                request_text=row[2],
                rationale=row[3],
                priority=NeedPriority(row[4]),
                state=NeedState(row[5]),
                motivating_hypothesis_ids=motivating,
                fulfilling_evidence_ids=fulfilling_evidence_ids,
                superseded_reason=row[7],
                created_at_turn=row[8],
                created_at=row[9] if row[9] else datetime.now(timezone.utc),
                updated_at=row[10] if row[10] else datetime.now(timezone.utc),
            )
        except Exception as need_err:  # noqa: BLE001
            logger.warning("Failed to load evidence_need %s: %s", row[0], need_err)
            return None

    async def _load_hypothesis_evidence_links(
        self, hypothesis_ids: builtins.list[str]
    ) -> Dict[str, builtins.list[HypothesisEvidenceLink]]:
        """Load junction-table rows and return them as
        ``{hypothesis_id: [HypothesisEvidenceLink, ...]}``.

        Empty input returns ``{}``. Hypotheses with no links are absent
        from the result (callers default to an empty list per hypothesis).
        The junction table doesn't carry the LLM's free-text rationale —
        that lives on ``case_messages`` / agent reasoning logs — so we
        persist an empty marker on the reconstructed link.
        """
        if not hypothesis_ids:
            return {}
        params: Dict[str, Any] = {}
        placeholders = self._bind_ids(params, hypothesis_ids)
        query = text(f"""
            SELECT hypothesis_id, evidence_id, relationship_type, confidence,
                   linked_at_turn, created_at
            FROM hypothesis_evidence
            WHERE hypothesis_id IN ({placeholders})
        """)
        result = await self.db.execute(query, params)
        rows = result.fetchall()

        by_hyp: Dict[str, builtins.list[HypothesisEvidenceLink]] = {}
        for row in rows:
            hyp_id = row[0]
            relationship = row[2] or "related"
            stance = _RELATIONSHIP_TO_STANCE.get(relationship, EvidenceStance.NEUTRAL)
            confidence = float(row[3]) if row[3] is not None else 0.0
            analyzed_at = row[5]
            if isinstance(analyzed_at, str):
                try:
                    analyzed_at = datetime.fromisoformat(analyzed_at.replace(" ", "T"))
                except ValueError:
                    analyzed_at = datetime.now(timezone.utc)
            elif analyzed_at is None:
                analyzed_at = datetime.now(timezone.utc)
            link = HypothesisEvidenceLink(
                hypothesis_id=str(hyp_id),
                evidence_id=str(row[1]),
                stance=stance,
                # Junction has no reasoning column; required-by-Pydantic
                # field is satisfied with empty marker.
                reasoning="",
                stance_confidence=max(0.0, min(1.0, confidence)),
                analyzed_at=analyzed_at,
            )
            by_hyp.setdefault(str(hyp_id), []).append(link)
        return by_hyp

    def _bind_ids(self, params: Dict[str, Any], ids: builtins.list[str]) -> str:
        """Expand a list of identifiers into named bind parameters.

        Returns the SQL placeholder clause (``:cid_0, :cid_1, ...``) and
        mutates ``params`` with the values. Used to splice into an
        ``IN (...)`` filter without resorting to f-string interpolation
        of values.
        """
        names = []
        for i, cid in enumerate(ids):
            key = f"cid_{i}"
            params[key] = cid
            names.append(f":{key}")
        return ", ".join(names)

    async def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters and pagination.

        Performance: ~20ms for 50 cases (indexed queries)

        Args:
            user_id: Filter by user
            organization_id: Filter by organization
            state: Filter by state
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

            if state:
                where_clauses.append("state = :state")
                params["state"] = state.value

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

    async def count_user_cases_on_date(self, user_id: str, date: Any) -> int:
        """
        Count cases created by a user on a specific date using PostgreSQL date casting.
        """
        try:
            # Ensure date object or string YYYY-MM-DD
            # PostgreSQL driver (asyncpg) handles date objects correctly
            date_val = date

            query = text("""
                SELECT COUNT(*)
                FROM cases
                WHERE user_id = :user_id
                AND created_at::date = :date
                """)
            result = await self.db.execute(
                query, {"user_id": user_id, "date": date_val}
            )
            return result.scalar() or 0

        except Exception as e:
            raise RepositoryException(f"Failed to count user cases: {e}") from e

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

    async def find_uploaded_file_by_content_hash(
        self, case_id: str, content_hash: str
    ) -> Optional[UploadedFile]:
        """Find oldest UploadedFile in a case whose ``content_hash`` matches.

        Dedup is a file-level concern: file uploads create only an
        UploadedFile row at intake (no Evidence row), so deduplication
        keys off ``uploaded_files.content_hash``. The hydrated
        UploadedFile carries the preprocessing artifacts (summary,
        structural_index, data_type, coverage timestamps).
        """
        if not content_hash:
            return None
        try:
            query = text("""
                SELECT
                    file_id, organization_id, case_id, uploaded_by,
                    filename, size_bytes, content_type, content_hash,
                    storage_ref, upload_source, uploaded_at_turn,
                    metadata, uploaded_at,
                    summary, structural_index, data_type,
                    coverage_start_ts, coverage_end_ts
                FROM uploaded_files
                WHERE case_id = :case_id
                  AND content_hash = :content_hash
                ORDER BY uploaded_at ASC
                LIMIT 1
            """)
            result = await self.db.execute(
                query, {"case_id": case_id, "content_hash": content_hash}
            )
            row = result.fetchone()
            if row is None:
                return None
            return UploadedFile(
                file_id=row[0],
                uploaded_by=row[3],
                filename=row[4],
                size_bytes=row[5],
                content_type=row[6],
                content_hash=row[7],
                storage_ref=row[8],
                upload_source=row[9],
                uploaded_at_turn=row[10],
                uploaded_at=row[12],
                summary=row[13],
                structural_index=row[14],
                data_type=row[15],
                coverage_start_ts=row[16],
                coverage_end_ts=row[17],
            )
        except Exception as e:
            raise RepositoryException(
                f"Failed to find uploaded_file by content_hash for case {case_id}: {e}"
            ) from e

    async def list_evidence_by_time_window(
        self,
        case_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Return evidence whose coverage overlaps ``[start, end]``.

        Overlap: ``coverage_start_ts <= end AND coverage_end_ts >= start``.
        NULL coverage timestamps exclude the row from results —
        timeless evidence isn't time-windowable. Uses the
        ``idx_evidence_coverage`` index for the case_id + range filter.
        """
        try:
            where_clauses = [
                "case_id = :case_id",
                "coverage_start_ts IS NOT NULL",
                "coverage_end_ts IS NOT NULL",
            ]
            params: Dict[str, Any] = {"case_id": case_id}

            if end is not None:
                where_clauses.append("coverage_start_ts <= :end_ts")
                params["end_ts"] = end
            if start is not None:
                where_clauses.append("coverage_end_ts >= :start_ts")
                params["start_ts"] = start

            query = text(f"""
                SELECT
                    evidence_id, category, source_type,
                    summary, extract,
                    is_primary, reliability_score, tags,
                    collected_at_turn, source_file_id, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at,
                    primary_purpose, analysis, processing_mode,
                    advances_milestones, collected_by
                FROM evidence
                WHERE {' AND '.join(where_clauses)}
                ORDER BY coverage_start_ts ASC
                LIMIT 1000
                """)
            result = await self.db.execute(query, params)
            rows = result.fetchall()

            evidence_list: List[Evidence] = []
            for row in rows:
                ev = self._row_to_evidence(row)
                if ev is not None:
                    evidence_list.append(ev)

            return evidence_list
        except Exception as e:
            raise RepositoryException(
                f"Failed to list evidence by time window for case {case_id}: {e}"
            ) from e

    async def upsert_case_entities(
        self,
        case_id: str,
        evidence_id: str,
        entities: List[CaseEntity],
    ) -> None:
        """Delete this evidence's case_entities rows, then insert fresh.

        ``organization_id`` is NOT NULL on case_entities; we derive it
        from the parent case row in the INSERT so callers don't have to
        thread it through.
        """
        try:
            delete_q = text("""
                DELETE FROM case_entities
                WHERE case_id = :case_id AND evidence_id = :evidence_id
                """)
            await self.db.execute(
                delete_q, {"case_id": case_id, "evidence_id": evidence_id}
            )

            if not entities:
                return

            insert_q = text("""
                INSERT INTO case_entities (
                    case_id, organization_id, entity_type, entity_value, evidence_id,
                    mention_count, in_error_context, first_seen_ts
                ) VALUES (
                    :case_id,
                    (SELECT organization_id FROM cases WHERE case_id = :case_id),
                    :entity_type, :entity_value, :evidence_id,
                    :mention_count, :in_error_context, :first_seen_ts
                )
                """)
            for entity in entities:
                await self.db.execute(
                    insert_q,
                    {
                        "case_id": case_id,
                        "entity_type": entity.entity_type.value,
                        "entity_value": entity.entity_value,
                        "evidence_id": evidence_id,
                        "mention_count": entity.mention_count,
                        "in_error_context": entity.in_error_context,
                        "first_seen_ts": entity.first_seen_ts,
                    },
                )
        except Exception as e:
            raise RepositoryException(
                f"Failed to upsert case_entities for evidence {evidence_id}: {e}"
            ) from e

    async def find_entity(
        self,
        case_id: str,
        entity_value: str,
        entity_type: Optional[EntityType] = None,
    ) -> List[CaseEntity]:
        """Phase 4 exact-value lookup — see CaseRepository.find_entity."""
        try:
            where_clauses = ["case_id = :case_id", "entity_value = :entity_value"]
            params: Dict[str, Any] = {
                "case_id": case_id,
                "entity_value": entity_value,
            }
            if entity_type is not None:
                where_clauses.append("entity_type = :entity_type")
                params["entity_type"] = entity_type.value

            query = text(f"""
                SELECT
                    case_id, entity_type, entity_value, evidence_id,
                    mention_count, in_error_context, first_seen_ts
                FROM case_entities
                WHERE {' AND '.join(where_clauses)}
                ORDER BY mention_count DESC
                """)
            result = await self.db.execute(query, params)
            rows = result.fetchall()
            return [_pg_row_to_case_entity(row) for row in rows]
        except Exception as e:
            raise RepositoryException(
                f"Failed to find entity in case {case_id}: {e}"
            ) from e

    async def list_top_entities(
        self,
        case_id: str,
        entity_type: EntityType,
        limit: int = 10,
    ) -> List[CaseEntity]:
        """Phase 4 aggregation — see CaseRepository.list_top_entities.

        PG version uses ``array_agg(evidence_id ORDER BY mention_count DESC)``
        to grab a representative evidence_id per distinct entity_value
        in a single query, avoiding the per-value follow-up scan the
        SQLite version needs.
        """
        try:
            query = text("""
                SELECT
                    entity_value,
                    SUM(mention_count) AS total_mentions,
                    BOOL_OR(in_error_context) AS any_error,
                    MIN(first_seen_ts) AS earliest_ts,
                    (array_agg(evidence_id ORDER BY mention_count DESC))[1]
                        AS representative_evidence_id
                FROM case_entities
                WHERE case_id = :case_id AND entity_type = :entity_type
                GROUP BY entity_value
                ORDER BY total_mentions DESC
                LIMIT :limit
                """)
            result = await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "entity_type": entity_type.value,
                    "limit": limit,
                },
            )
            rows = result.fetchall()
            return [
                CaseEntity(
                    case_id=case_id,
                    entity_type=entity_type,
                    entity_value=row[0],
                    evidence_id=row[4] or "",
                    mention_count=int(row[1]),
                    in_error_context=bool(row[2]),
                    first_seen_ts=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            raise RepositoryException(
                f"Failed to list top entities for case {case_id}: {e}"
            ) from e

    async def share_case(
        self,
        case_id: str,
        target_user_id: str,
        role: str,  # ParticipantRole: owner, collaborator, viewer
        sharer_user_id: Optional[str] = None,
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

            await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "user_id": target_user_id,
                    "role": role,
                    "added_by": sharer_user_id or target_user_id,
                },
            )
            await self.db.commit()

            self.logger.info(
                f"Shared case {case_id} with user {target_user_id} as {role}"
            )
            return True

        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(f"Failed to share case {case_id}: {e}") from e

    async def unshare_case(
        self, case_id: str, user_id: str, unsharer_user_id: Optional[str] = None
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

            await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "user_id": user_id,
                    "removed_by": unsharer_user_id or user_id,
                },
            )
            await self.db.commit()

            self.logger.info(f"Unshared case {case_id} from user {user_id}")
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
                    "last_accessed_at": row.last_accessed_at,
                }
                for row in rows
            ]

        except Exception as e:
            raise RepositoryException(
                f"Failed to get participants for case {case_id}: {e}"
            ) from e

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        limit: int = 20,
    ) -> tuple[List[Case], int]:
        """
        Search cases using PostgreSQL full-text search.

        Searches:
        - cases.title
        - cases.inquiry->>'proposed_problem_statement'

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
            # Note: Evidence search removed per Principle 3 (Database Boundaries)
            # Evidence full-text search should be done via IEvidenceQuery if needed
            where_clauses = [
                "(to_tsvector('english', c.title || ' ' || COALESCE(c.inquiry->>'proposed_problem_statement', '')) @@ plainto_tsquery('english', :query) OR c.case_id ILIKE :case_id_pattern)"
            ]
            params = {"query": query, "case_id_pattern": f"%{query}%", "limit": limit}

            if user_id:
                where_clauses.append("c.user_id = :user_id")
                params["user_id"] = user_id

            if organization_id:
                where_clauses.append("c.organization_id = :organization_id")
                params["organization_id"] = organization_id

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Search query with relevance ranking
            # Evidence JOIN removed per Principle 3 (Database Boundaries)
            search_query = text(f"""
                SELECT DISTINCT c.case_id,
                    ts_rank(to_tsvector('english', c.title), plainto_tsquery('english', :query)) as rank
                FROM cases c
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
        """Add message to case_messages table.

        Returns False (not raise) if the parent case doesn't exist —
        organization_id is NOT NULL on case_messages and is derived
        via subquery from the parent case row, so a missing case
        would otherwise surface as an IntegrityError. Pre-checking
        keeps the contract: True on success, False on missing case,
        raise only on real persistence errors.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        try:
            probe = await self.db.execute(
                text("SELECT 1 FROM cases WHERE case_id = :case_id"),
                {"case_id": case_id},
            )
            if probe.fetchone() is None:
                return False

            message_id = message_dict.get("message_id", f"msg_{uuid4().hex[:16]}")
            created_at = (
                message_dict.get("created_at")
                or message_dict.get("timestamp")
                or datetime.now(timezone.utc)
            )

            query = text("""
                INSERT INTO case_messages (
                    message_id, case_id, organization_id, turn_number, role, content,
                    created_at, token_count, metadata
                ) VALUES (
                    :message_id, :case_id,
                    (SELECT organization_id FROM cases WHERE case_id = :case_id),
                    :turn_number, :role, :content,
                    :created_at, :token_count, :metadata::jsonb
                )
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

    async def get_messages(
        self, case_id: str, limit: int = 50, offset: int = 0
    ) -> List[dict]:
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
                metadata_raw = row[6]
                if isinstance(metadata_raw, dict):
                    metadata = metadata_raw
                elif isinstance(metadata_raw, str):
                    metadata = json.loads(metadata_raw) if metadata_raw else {}
                else:
                    metadata = {}

                created_at = row[4].isoformat() if row[4] else None
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
            raise RepositoryException(
                f"Failed to update activity timestamp for case {case_id}: {e}"
            ) from e

    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Scoped UPDATE of cosmetic metadata fields — does NOT bump version.

        See ``ICaseRepository.update_metadata_fields`` for rationale.
        """
        if title is None and description is None:
            return False

        sets: list[str] = []
        params: dict[str, Any] = {"case_id": case_id}
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if description is not None:
            sets.append("description = :description")
            params["description"] = description
        sets.append("updated_at = NOW()")

        try:
            query = text(f"UPDATE cases SET {', '.join(sets)} WHERE case_id = :case_id")
            result = await self.db.execute(query, params)
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update metadata fields for case {case_id}: {e}"
            ) from e

    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """Scoped UPDATE of the `vectorized` column on one evidence row.

        Safe alternative to aggregate save(case) from background tasks — does
        not touch case_messages or other sibling tables.
        """
        try:
            query = text("""
                UPDATE evidence
                SET vectorized = :vectorized
                WHERE case_id = :case_id AND evidence_id = :evidence_id
            """)
            result = await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "evidence_id": evidence_id,
                    "vectorized": vectorized,
                },
            )
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update vectorized flag for evidence "
                f"{evidence_id} on case {case_id}: {e}"
            ) from e

    async def delete_evidence(self, case_id: str, evidence_id: str) -> bool:
        """Scoped DELETE of a single evidence row."""
        try:
            query = text("""
                DELETE FROM evidence
                WHERE case_id = :case_id AND evidence_id = :evidence_id
            """)
            result = await self.db.execute(
                query, {"case_id": case_id, "evidence_id": evidence_id}
            )
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to delete evidence {evidence_id} on case {case_id}: {e}"
            ) from e

    async def delete_uploaded_file(self, case_id: str, file_id: str) -> bool:
        """Scoped DELETE of a single uploaded_file row."""
        try:
            query = text("""
                DELETE FROM uploaded_files
                WHERE case_id = :case_id AND file_id = :file_id
            """)
            result = await self.db.execute(
                query, {"case_id": case_id, "file_id": file_id}
            )
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to delete uploaded_file {file_id} on case {case_id}: {e}"
            ) from e

    async def get_analytics(self, case_id: str) -> Dict[str, Any]:
        """
        Compute analytics for case from normalized tables.

        Note: Evidence count is loaded via IEvidenceQuery to respect database
        boundaries (Principle 3: Database Boundaries - no cross-module JOINs).

        Returns:
            Dictionary with analytics data
        """
        try:
            # Evidence JOIN removed per Principle 3 (Database Boundaries)
            # Evidence count loaded via IEvidenceQuery
            query = text("""
                SELECT
                    COUNT(DISTINCT h.hypothesis_id) as hypothesis_count,
                    COUNT(DISTINCT h.hypothesis_id) FILTER (WHERE h.state = 'validated') as validated_hypotheses,
                    COUNT(DISTINCT s.solution_id) as solution_count,
                    COUNT(DISTINCT s.solution_id) FILTER (WHERE s.state = 'implemented') as implemented_solutions,
                    COUNT(DISTINCT m.message_id) as message_count,
                    COUNT(DISTINCT f.file_id) as file_count,
                    SUM(f.size_bytes) as total_file_size
                FROM cases c
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

            analytics = {
                "evidence_count": 0,  # Will be loaded directly
                "hypothesis_count": row[0] or 0,
                "validated_hypotheses": row[1] or 0,
                "solution_count": row[2] or 0,
                "implemented_solutions": row[3] or 0,
                "message_count": row[4] or 0,
                "file_count": row[5] or 0,
                "total_file_size": row[6] or 0,
            }

            # Load evidence count directly (Case owns evidence per module-organization-design.md)
            try:
                count_query = text(
                    "SELECT COUNT(*) FROM evidence_artifacts WHERE case_id = :case_id"
                )
                count_result = await self.db.execute(count_query, {"case_id": case_id})
                count_row = count_result.fetchone()
                if count_row:
                    analytics["evidence_count"] = count_row[0]
            except Exception:
                pass  # Keep default of 0 on failure

            return analytics

        except Exception as e:
            raise RepositoryException(
                f"Failed to get analytics for case {case_id}: {e}"
            ) from e

    async def cleanup_expired(
        self, max_age_days: int = 90, batch_size: int = 100
    ) -> int:
        """Delete closed cases whose ``closed_at`` is older than max_age_days.

        Post-redesign: ``closed_at`` is a first-class column. The DELETE
        compares it directly against the cutoff datetime. The interval
        is built via ``make_interval(days := :max_age_days)`` so the
        bind parameter type-checks (the older
        ``INTERVAL ':max_age_days days'`` form silently quoted the
        whole literal and never interpolated).
        """
        try:
            query = text("""
                DELETE FROM cases
                WHERE case_id IN (
                    SELECT case_id
                    FROM cases
                    WHERE state = 'closed'
                    AND closed_at IS NOT NULL
                    AND closed_at < NOW() - make_interval(days := :max_age_days)
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
        """Upsert main cases table with optimistic concurrency control.

        OCC: attempts UPDATE with a version predicate first; raises
        StaleCaseException on version mismatch; falls back to INSERT
        when no row exists. On success mutates `case.version` in place.

        Post-redesign (storage redesign 2026-04): persists ``description``,
        ``investigation_strategy``, ``current_turn``,
        ``turns_without_progress``, ``closure_reason``,
        ``last_activity_at``, ``resolved_at`` and ``closed_at`` to
        first-class columns instead of the ``metadata`` JSON blob.
        ``last_activity_at`` is bumped to the current UTC time on every
        save so staleness queries work without scanning JSON.

        Deployment-Agnostic Implementation:
        - Detects database dialect (PostgreSQL vs SQLite)
        - Uses PostgreSQL ::jsonb type casts when available
        - Uses plain text/JSON for SQLite compatibility
        """
        # Detect database dialect for deployment-agnostic SQL
        dialect_name = self.db.bind.dialect.name if self.db.bind else "sqlite"
        is_postgresql = dialect_name == "postgresql"

        jsonb = "::jsonb" if is_postgresql else ""
        last_activity_at = datetime.now(timezone.utc)
        params = self._case_record_params(case, last_activity_at)
        expected_version = case.version
        new_version = expected_version + 1
        update_params = {
            **params,
            "expected_version": expected_version,
            "new_version": new_version,
        }

        # Step 1: UPDATE with version check.
        update_query = text(f"""
            UPDATE cases SET
                user_id = :user_id,
                organization_id = :organization_id,
                title = :title,
                description = :description,
                investigation_strategy = :investigation_strategy,
                state = :state,
                closure_reason = :closure_reason,
                current_turn = :current_turn,
                turns_without_progress = :turns_without_progress,
                updated_at = :updated_at,
                last_activity_at = :last_activity_at,
                resolved_at = :resolved_at,
                closed_at = :closed_at,
                disposition_eligibility = :disposition_eligibility,
                inquiry = :inquiry{jsonb},
                problem_verification = :problem_verification{jsonb},
                working_conclusion = :working_conclusion{jsonb},
                root_cause_conclusion = :root_cause_conclusion{jsonb},
                escalation_state = :escalation_state{jsonb},
                documentation = :documentation{jsonb},
                progress = :progress{jsonb},
                metadata = :metadata{jsonb},
                version = :new_version
            WHERE case_id = :case_id AND version = :expected_version
        """)
        result = await self.db.execute(update_query, update_params)

        if result.rowcount > 0:
            case.version = new_version
            return

        # Step 2: no UPDATE — either case is new, or version mismatched.
        probe = await self.db.execute(
            text("SELECT version FROM cases WHERE case_id = :case_id"),
            {"case_id": case.case_id},
        )
        row = probe.fetchone()
        if row is None:
            # New case — plain INSERT with version = 1.
            insert_query = text(f"""
                INSERT INTO cases (
                    case_id, user_id, organization_id, title, description, investigation_strategy,
                    state, closure_reason, current_turn, turns_without_progress,
                    created_at, updated_at, last_activity_at, resolved_at, closed_at,
                    disposition_eligibility,
                    inquiry, problem_verification, working_conclusion,
                    root_cause_conclusion,
                    escalation_state, documentation, progress, metadata,
                    version
                ) VALUES (
                    :case_id, :user_id, :organization_id, :title, :description, :investigation_strategy,
                    :state, :closure_reason, :current_turn, :turns_without_progress,
                    :created_at, :updated_at, :last_activity_at, :resolved_at, :closed_at,
                    :disposition_eligibility,
                    :inquiry{jsonb}, :problem_verification{jsonb}, :working_conclusion{jsonb},
                    :root_cause_conclusion{jsonb},
                    :escalation_state{jsonb}, :documentation{jsonb}, :progress{jsonb}, :metadata{jsonb},
                    1
                )
            """)
            await self.db.execute(insert_query, params)
            case.version = 1
            return

        # Row exists but version mismatched — caller holds stale state.
        raise StaleCaseException(
            case_id=case.case_id,
            expected_version=expected_version,
            actual_version=row[0],
        )

    def _case_record_params(
        self, case: Case, last_activity_at: datetime
    ) -> Dict[str, Any]:
        """Parameter dict for the cases-row INSERT/UPDATE.

        Shared between the UPDATE and fallback INSERT paths in
        _upsert_case_record — keeps column serialization in one place.

        Post-redesign: ``description``, ``investigation_strategy``,
        ``current_turn``, ``turns_without_progress`` are first-class
        columns (the PG hybrid had been writing them as phantom columns
        before the schema baseline; now they're real). The ``metadata``
        JSON blob still holds the transient runtime state (proposed_actions /
        action_attempts / turn_history / pending_transition) — those
        have no first-class column yet.
        """
        return {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "organization_id": case.organization_id,
            "title": case.title,
            "description": case.description or "",
            "investigation_strategy": case.investigation_strategy.value,
            "state": case.state.value,
            "closure_reason": case.closure_reason,
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "last_activity_at": last_activity_at,
            "resolved_at": case.resolved_at,
            "closed_at": case.closed_at,
            "disposition_eligibility": (
                json.dumps(case.disposition_eligibility)
                if case.disposition_eligibility
                else None
            ),
            "inquiry": json.dumps(case.inquiry.model_dump(mode="json")),
            "problem_verification": (
                json.dumps(case.problem_verification.model_dump(mode="json"))
                if case.problem_verification
                else None
            ),
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
            "escalation_state": (
                json.dumps(case.escalation_state.model_dump(mode="json"))
                if case.escalation_state
                else None
            ),
            "documentation": json.dumps(case.documentation.model_dump(mode="json")),
            "progress": json.dumps(case.progress.model_dump(mode="json")),
            "metadata": json.dumps(
                {
                    k: v
                    for k, v in {
                        "pending_transition": case.pending_transition,
                        "proposed_actions": (
                            [a.model_dump(mode="json") for a in case.proposed_actions]
                            if case.proposed_actions
                            else []
                        ),
                        "action_attempts": (
                            [a.model_dump(mode="json") for a in case.action_attempts]
                            if case.action_attempts
                            else []
                        ),
                        "turn_history": (
                            [t.model_dump(mode="json") for t in case.turn_history]
                            if case.turn_history
                            else []
                        ),
                    }.items()
                    if v
                }
            ),
        }

    async def _upsert_evidence(
        self, case_id: str, evidence_list: List[Evidence], organization_id: str
    ) -> None:
        """Upsert evidence records.

        Purely additive: inserts new rows and updates existing ones keyed by
        evidence_id. Does NOT remove rows absent from `evidence_list`. The
        in-memory case is a working snapshot, not the canonical truth for
        which rows should exist — callers holding a stale snapshot (e.g.
        background tasks) must not be able to silently delete rows that
        other concurrent writers have added. For intentional removal, use
        `delete_evidence(case_id, evidence_id)` explicitly.

        File-level metadata (filename, content_type, content_hash, size,
        storage_ref) lives on ``uploaded_files`` and is reached via
        ``source_file_id``. Chat-extracted evidence
        (``source_type=USER_DESCRIPTION``) has ``source_file_id IS NULL``
        and persists no file metadata.
        """
        for evidence in evidence_list:
            query = text("""
                INSERT INTO evidence (
                    evidence_id, case_id, organization_id, source_file_id,
                    category, source_type,
                    summary, extract,
                    primary_purpose, analysis, processing_mode, advances_milestones,
                    is_primary, reliability_score, tags,
                    collected_at_turn, collected_by, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at, updated_at
                ) VALUES (
                    :evidence_id, :case_id, :organization_id, :source_file_id,
                    :category, :source_type,
                    :summary, :extract,
                    :primary_purpose, :analysis, :processing_mode, :advances_milestones,
                    :is_primary, :reliability_score, :tags,
                    :collected_at_turn, :collected_by, :vectorized,
                    :coverage_start_ts, :coverage_end_ts,
                    :metadata::jsonb, :created_at, :updated_at
                )
                ON CONFLICT (evidence_id) DO UPDATE SET
                    source_file_id = EXCLUDED.source_file_id,
                    category = EXCLUDED.category,
                    source_type = EXCLUDED.source_type,
                    summary = EXCLUDED.summary,
                    extract = EXCLUDED.extract,
                    primary_purpose = EXCLUDED.primary_purpose,
                    analysis = EXCLUDED.analysis,
                    processing_mode = EXCLUDED.processing_mode,
                    advances_milestones = EXCLUDED.advances_milestones,
                    is_primary = EXCLUDED.is_primary,
                    reliability_score = EXCLUDED.reliability_score,
                    tags = EXCLUDED.tags,
                    collected_at_turn = EXCLUDED.collected_at_turn,
                    collected_by = EXCLUDED.collected_by,
                    vectorized = EXCLUDED.vectorized,
                    coverage_start_ts = EXCLUDED.coverage_start_ts,
                    coverage_end_ts = EXCLUDED.coverage_end_ts,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
            """)

            now = datetime.now(timezone.utc)
            await self.db.execute(
                query,
                {
                    "evidence_id": evidence.evidence_id,
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "source_file_id": evidence.source_file_id,
                    "category": evidence.category.value,
                    "source_type": evidence.source_type.value,
                    "summary": evidence.summary,
                    "extract": evidence.extract,
                    "primary_purpose": evidence.primary_purpose,
                    "analysis": evidence.analysis,
                    "processing_mode": evidence.processing_mode,
                    "advances_milestones": _serialize_tags(
                        list(evidence.advances_milestones)
                    ),
                    "is_primary": evidence.is_primary,
                    "reliability_score": evidence.reliability_score,
                    "tags": _serialize_tags(evidence.tags),
                    "collected_at_turn": evidence.collected_at_turn,
                    "collected_by": evidence.collected_by,
                    "vectorized": evidence.vectorized,
                    "coverage_start_ts": evidence.coverage_start_ts,
                    "coverage_end_ts": evidence.coverage_end_ts,
                    "metadata": json.dumps(evidence.metadata or {}),
                    "created_at": evidence.collected_at or now,
                    "updated_at": now,
                },
            )

    async def _upsert_evidence_needs(
        self,
        case_id: str,
        needs_list: builtins.list[EvidenceNeed],
        organization_id: str,
        current_turn: int,
    ) -> None:
        """Upsert evidence-need records + fulfillment junction rows.

        Purely additive — see ``_upsert_evidence`` for rationale. The
        junction's ``ON CONFLICT (need_id, evidence_id) DO NOTHING``
        preserves the original ``linked_at_turn`` if the same pair is
        seen again on a re-save.

        Must run AFTER ``_upsert_evidence`` so the junction's FK to
        ``evidence.evidence_id`` is satisfied.
        """
        for need in needs_list:
            query = text("""
                INSERT INTO evidence_needs (
                    need_id, case_id, organization_id,
                    purpose, request_text, rationale,
                    priority, state,
                    motivating_hypothesis_ids,
                    superseded_reason,
                    created_at_turn, created_at, updated_at
                ) VALUES (
                    :need_id, :case_id, :organization_id,
                    :purpose, :request_text, :rationale,
                    :priority, :state,
                    :motivating_hypothesis_ids::jsonb,
                    :superseded_reason,
                    :created_at_turn, :created_at, :updated_at
                )
                ON CONFLICT (need_id) DO UPDATE SET
                    purpose = EXCLUDED.purpose,
                    request_text = EXCLUDED.request_text,
                    rationale = EXCLUDED.rationale,
                    priority = EXCLUDED.priority,
                    state = EXCLUDED.state,
                    motivating_hypothesis_ids = EXCLUDED.motivating_hypothesis_ids,
                    superseded_reason = EXCLUDED.superseded_reason,
                    updated_at = EXCLUDED.updated_at
            """)

            now = datetime.now(timezone.utc)
            await self.db.execute(
                query,
                {
                    "need_id": need.need_id,
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "purpose": need.purpose.value,
                    "request_text": need.request_text,
                    "rationale": need.rationale,
                    "priority": need.priority.value,
                    "state": need.state.value,
                    "motivating_hypothesis_ids": json.dumps(
                        need.motivating_hypothesis_ids
                    ),
                    "superseded_reason": need.superseded_reason,
                    "created_at_turn": need.created_at_turn,
                    "created_at": need.created_at or now,
                    "updated_at": now,
                },
            )

            if need.fulfilling_evidence_ids:
                junction_query = text("""
                    INSERT INTO evidence_need_fulfillment (
                        need_id, evidence_id, organization_id, linked_at_turn
                    ) VALUES (
                        :need_id, :evidence_id, :organization_id, :linked_at_turn
                    )
                    ON CONFLICT (need_id, evidence_id) DO NOTHING
                """)
                for evidence_id in need.fulfilling_evidence_ids:
                    await self.db.execute(
                        junction_query,
                        {
                            "need_id": need.need_id,
                            "evidence_id": evidence_id,
                            "organization_id": organization_id,
                            "linked_at_turn": current_turn,
                        },
                    )

    async def _upsert_hypotheses(
        self, case_id: str, hypotheses_dict: Dict[str, Hypothesis], organization_id: str
    ) -> None:
        """Upsert hypotheses records.

        Purely additive — see `_upsert_evidence` for rationale. There is
        no concrete delete-hypothesis API on the case repo today; if a
        single-hypothesis remove path is needed, add it explicitly.

        The dropped ``hypotheses.evidence_links`` JSON blob has been
        replaced by the ``hypothesis_evidence`` junction table; that
        upsert runs after the parent row is in place so FK constraints
        are satisfied.
        """
        for hypothesis_id, hypothesis in hypotheses_dict.items():
            query = text("""
                INSERT INTO hypotheses (
                    hypothesis_id, case_id, organization_id, statement, state,
                    likelihood, initial_likelihood,
                    generated_at_turn, last_updated_turn, last_progress_at_turn,
                    iterations_without_progress,
                    category, generation_mode, rationale, retirement_reason,
                    refutation_reason,
                    tested_at, concluded_at, proposed_at, updated_at, metadata,
                    created_by, updated_by
                ) VALUES (
                    :hypothesis_id, :case_id, :organization_id, :statement, :state,
                    :likelihood, :initial_likelihood,
                    :generated_at_turn, :last_updated_turn, :last_progress_at_turn,
                    :iterations_without_progress,
                    :category, :generation_mode, :rationale, :retirement_reason,
                    :refutation_reason,
                    :tested_at, :concluded_at, :proposed_at, :updated_at, :metadata::jsonb,
                    :created_by, :updated_by
                )
                ON CONFLICT (hypothesis_id) DO UPDATE SET
                    statement = EXCLUDED.statement,
                    state = EXCLUDED.state,
                    likelihood = EXCLUDED.likelihood,
                    generated_at_turn = EXCLUDED.generated_at_turn,
                    last_updated_turn = EXCLUDED.last_updated_turn,
                    last_progress_at_turn = EXCLUDED.last_progress_at_turn,
                    iterations_without_progress = EXCLUDED.iterations_without_progress,
                    retirement_reason = EXCLUDED.retirement_reason,
                    refutation_reason = EXCLUDED.refutation_reason,
                    concluded_at = EXCLUDED.concluded_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "hypothesis_id": hypothesis_id,
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "statement": hypothesis.statement,
                    "state": hypothesis.state.value,
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
                    "refutation_reason": hypothesis.refutation_reason,
                    "tested_at": hypothesis.tested_at,
                    "concluded_at": hypothesis.concluded_at,
                    "proposed_at": getattr(hypothesis, "proposed_at", None)
                    or datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                    "metadata": json.dumps({}),
                    "created_by": None,
                    "updated_by": None,
                },
            )

            await self._upsert_hypothesis_evidence(
                hypothesis_id, hypothesis.evidence_links, organization_id
            )

    async def _upsert_hypothesis_evidence(
        self,
        hypothesis_id: str,
        links: builtins.list[HypothesisEvidenceLink],
        organization_id: str,
    ) -> None:
        """Upsert rows on the ``hypothesis_evidence`` junction table.

        Purely additive — never deletes rows. Composite PK
        ``(hypothesis_id, evidence_id)`` makes the upsert idempotent.
        Stance → relationship_type mapping is in
        ``_STANCE_TO_RELATIONSHIP``; NEUTRAL maps to ``related`` because
        the junction CHECK constraint only allows
        ``('supports', 'refutes', 'related')``.
        """
        if not links:
            return
        query = text("""
            INSERT INTO hypothesis_evidence (
                hypothesis_id, evidence_id, organization_id,
                relationship_type, confidence, linked_at_turn,
                linked_by, created_at
            ) VALUES (
                :hypothesis_id, :evidence_id, :organization_id,
                :relationship_type, :confidence, :linked_at_turn,
                :linked_by, :created_at
            )
            ON CONFLICT (hypothesis_id, evidence_id) DO UPDATE SET
                relationship_type = EXCLUDED.relationship_type,
                confidence = EXCLUDED.confidence,
                linked_at_turn = EXCLUDED.linked_at_turn,
                linked_by = EXCLUDED.linked_by
        """)

        for link in links:
            relationship = _STANCE_TO_RELATIONSHIP.get(link.stance, "related")
            await self.db.execute(
                query,
                {
                    "hypothesis_id": hypothesis_id,
                    "evidence_id": link.evidence_id,
                    "organization_id": organization_id,
                    "relationship_type": relationship,
                    "confidence": link.stance_confidence,
                    # Domain HypothesisEvidenceLink doesn't carry a turn
                    # number; the junction column is nullable.
                    "linked_at_turn": None,
                    # Linker user_id isn't tracked on the link object —
                    # nullable column, FK SET NULL on user delete.
                    "linked_by": None,
                    "created_at": link.analyzed_at,
                },
            )

    async def _upsert_solutions(
        self, case_id: str, solutions_list: List[Solution], organization_id: str
    ) -> None:
        """Upsert solutions records (normalized table).

        Purely additive — see `_upsert_evidence` for rationale. There is
        no concrete delete-solution API on the case repo today; if a
        single-solution remove path is needed, add it explicitly.

        Post-009 schema: writes the full Solution audit trail
        (proposed_by, applied_at/by, verified_at, verification_method,
        verification_evidence_id, effectiveness). Status is derived
        from the lifecycle fields and included in ON CONFLICT UPDATE.
        """
        for solution in solutions_list:
            applied_at = solution.applied_at
            verified_at = solution.verified_at
            state = self._derive_solution_state(solution)

            query = text("""
                INSERT INTO solutions (
                    solution_id, case_id, organization_id, solution_type, title,
                    immediate_action, longterm_fix, implementation_steps, commands, risks,
                    description, state,
                    proposed_by, applied_by,
                    verification_method, verification_evidence_id, effectiveness,
                    verification_result, verified_at,
                    proposed_at, applied_at, updated_at, metadata
                ) VALUES (
                    :solution_id, :case_id, :organization_id, :solution_type, :title,
                    :immediate_action, :longterm_fix, :implementation_steps::jsonb,
                    :commands::jsonb, :risks::jsonb,
                    :description, :state,
                    :proposed_by, :applied_by,
                    :verification_method, :verification_evidence_id, :effectiveness,
                    :verification_result, :verified_at,
                    :proposed_at, :applied_at, :updated_at, :metadata::jsonb
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
                    state = EXCLUDED.state,
                    proposed_by = EXCLUDED.proposed_by,
                    applied_by = EXCLUDED.applied_by,
                    verification_method = EXCLUDED.verification_method,
                    verification_evidence_id = EXCLUDED.verification_evidence_id,
                    effectiveness = EXCLUDED.effectiveness,
                    verification_result = EXCLUDED.verification_result,
                    verified_at = EXCLUDED.verified_at,
                    applied_at = EXCLUDED.applied_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
            """)

            await self.db.execute(
                query,
                {
                    "solution_id": solution.solution_id,
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "solution_type": solution.solution_type.value,
                    "title": solution.title,
                    "immediate_action": solution.immediate_action,
                    "longterm_fix": solution.longterm_fix,
                    "implementation_steps": json.dumps(
                        list(solution.implementation_steps)
                    ),
                    "commands": json.dumps(list(solution.commands)),
                    "risks": json.dumps(list(solution.risks)),
                    "description": (
                        solution.immediate_action
                        or solution.longterm_fix
                        or solution.title
                    ),
                    "state": state,
                    "proposed_by": solution.proposed_by,
                    "applied_by": solution.applied_by,
                    "verification_method": solution.verification_method,
                    "verification_evidence_id": solution.verification_evidence_id,
                    "effectiveness": solution.effectiveness,
                    "verification_result": None,
                    "verified_at": verified_at,
                    "proposed_at": solution.proposed_at,
                    "applied_at": applied_at,
                    "updated_at": datetime.now(timezone.utc),
                    "metadata": json.dumps({}),
                },
            )

    @staticmethod
    def _derive_solution_state(solution: Solution) -> str:
        """Map Pydantic Solution lifecycle fields to the schema's
        state CHECK vocabulary. Mirrors the SQLite repo logic.
        """
        if solution.verified_at is not None:
            return "verified"
        if solution.applied_at is not None:
            return "implemented"
        return "proposed"

    async def _upsert_uploaded_files(
        self, case_id: str, files_list: List[UploadedFile], organization_id: str
    ) -> None:
        """Upsert uploaded_files records.

        Purely additive — see `_upsert_evidence` for rationale. For
        intentional removal, use `delete_uploaded_file(case_id, file_id)`.

        Renamed columns: ``content_ref`` → ``storage_ref``,
        ``source_type`` → ``upload_source``. Dropped: ``data_type``.
        Added: ``content_hash``, ``content_type`` (MIME), ``uploaded_by``.
        ``case_id`` is now nullable on the table (KB conversion uploads),
        but case-evidence uploads always carry one — passed through
        verbatim from the call site.
        """
        for file in files_list:
            query = text("""
                INSERT INTO uploaded_files (
                    file_id, case_id, organization_id, uploaded_by,
                    filename, size_bytes, content_type, content_hash,
                    storage_ref, upload_source,
                    uploaded_at_turn, uploaded_at,
                    metadata,
                    summary, structural_index, data_type,
                    coverage_start_ts, coverage_end_ts
                ) VALUES (
                    :file_id, :case_id, :organization_id, :uploaded_by,
                    :filename, :size_bytes, :content_type, :content_hash,
                    :storage_ref, :upload_source,
                    :uploaded_at_turn, :uploaded_at,
                    :metadata::jsonb,
                    :summary, :structural_index, :data_type,
                    :coverage_start_ts, :coverage_end_ts
                )
                ON CONFLICT (file_id) DO UPDATE SET
                    uploaded_by = EXCLUDED.uploaded_by,
                    filename = EXCLUDED.filename,
                    size_bytes = EXCLUDED.size_bytes,
                    content_type = EXCLUDED.content_type,
                    content_hash = EXCLUDED.content_hash,
                    storage_ref = EXCLUDED.storage_ref,
                    upload_source = EXCLUDED.upload_source,
                    uploaded_at_turn = EXCLUDED.uploaded_at_turn,
                    metadata = EXCLUDED.metadata,
                    -- Preprocessing artifacts use COALESCE so a failed re-run
                    -- (NULL incoming) cannot clobber a prior good extraction.
                    -- Intentional clearing must go through a dedicated path.
                    summary = COALESCE(EXCLUDED.summary, uploaded_files.summary),
                    structural_index = COALESCE(EXCLUDED.structural_index, uploaded_files.structural_index),
                    data_type = COALESCE(EXCLUDED.data_type, uploaded_files.data_type),
                    coverage_start_ts = COALESCE(EXCLUDED.coverage_start_ts, uploaded_files.coverage_start_ts),
                    coverage_end_ts = COALESCE(EXCLUDED.coverage_end_ts, uploaded_files.coverage_end_ts)
            """)

            await self.db.execute(
                query,
                {
                    "file_id": file.file_id,
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "uploaded_by": file.uploaded_by,
                    "filename": file.filename,
                    "size_bytes": file.size_bytes,
                    "content_type": file.content_type,
                    "content_hash": file.content_hash,
                    "storage_ref": file.storage_ref,
                    "upload_source": file.upload_source,
                    "uploaded_at_turn": file.uploaded_at_turn,
                    "uploaded_at": file.uploaded_at,
                    "metadata": json.dumps({}),
                    "summary": file.summary,
                    "structural_index": file.structural_index,
                    "data_type": file.data_type,
                    "coverage_start_ts": file.coverage_start_ts,
                    "coverage_end_ts": file.coverage_end_ts,
                },
            )

    async def _upsert_messages(
        self, case_id: str, messages_list: List[Dict[str, Any]], organization_id: str
    ) -> None:
        """Upsert case messages (PostgreSQL-optimized).

        Purely additive — messages are an append-only log at the domain
        level; nothing intentionally deletes them. A stale in-memory
        ``case.messages`` MUST NOT silently truncate rows other concurrent
        writers have persisted.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count, metadata
        """
        # Upsert each message
        for idx, msg in enumerate(messages_list):
            # Skip if no message_id (shouldn't happen, but be safe)
            if not msg.get("message_id"):
                continue

            query = text("""
                INSERT INTO case_messages (
                    message_id, case_id, organization_id, turn_number, role, content, created_at, token_count, metadata
                ) VALUES (
                    :message_id, :case_id, :organization_id, :turn_number, :role, :content, :created_at, :token_count, :metadata::jsonb
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
                    "organization_id": organization_id,
                    "turn_number": msg.get("turn_number", idx),
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "created_at": msg.get("created_at") or datetime.now(timezone.utc),
                    "token_count": msg.get("token_count"),
                    "metadata": json.dumps(msg.get("metadata", {})),
                },
            )

    async def _append_case_actions(
        self, case_id: str, transitions: List[CaseAction], organization_id: str
    ) -> None:
        """Append only newly-added case actions (append-only audit trail).

        ``action_history`` is hydrated oldest-first by ``_load_case_actions``
        and new actions are appended to the tail, so the in-memory list is
        always ``[persisted_prefix..., new_tail...]``. Only the unpersisted
        tail is inserted.

        Re-inserting the full list every ``save()`` previously caused
        *geometric* row growth: ``transition_id`` is an autoincrement PK with
        no natural-key conflict target, so the ``ON CONFLICT DO NOTHING``
        clause could never fire and every save duplicated the entire history
        (R rows → 2R + new). Counting already-persisted rows and inserting
        only ``transitions[already_persisted:]`` makes each save O(new), not
        O(history).
        """
        count_result = await self.db.execute(
            text("SELECT COUNT(*) FROM case_actions WHERE case_id = :case_id"),
            {"case_id": case_id},
        )
        already_persisted = count_result.scalar() or 0
        new_transitions = transitions[already_persisted:]
        for transition in new_transitions:
            query = text("""
                INSERT INTO case_actions (
                    case_id, organization_id, from_state, to_state, reason,
                    triggered_by, transitioned_at, metadata
                ) VALUES (
                    :case_id, :organization_id, :from_state, :to_state, :reason,
                    :triggered_by, :transitioned_at, :metadata::jsonb
                )
            """)

            await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "from_state": (
                        transition.from_state.value if transition.from_state else None
                    ),
                    "to_state": transition.to_state.value,
                    "reason": (
                        transition.reason if hasattr(transition, "reason") else None
                    ),
                    "triggered_by": transition.triggered_by,
                    "transitioned_at": transition.triggered_at,
                    "metadata": json.dumps({}),
                },
            )

    async def _load_case_actions(self, case_id: str) -> List[CaseAction]:
        """Hydrate the audit trail for a case from ``case_actions``.

        Replaces the prior write-only pattern (``action_history=[]`` hardcoded
        in ``_to_domain``). Rows are returned ordered oldest-first.
        """
        query = text("""
            SELECT from_state, to_state, reason, triggered_by, transitioned_at
            FROM case_actions
            WHERE case_id = :case_id
            ORDER BY transitioned_at ASC, transition_id ASC
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()
        actions: List[CaseAction] = []
        for row in rows:
            actions.append(
                CaseAction(
                    from_state=(CaseState(row.from_state) if row.from_state else None),
                    to_state=CaseState(row.to_state),
                    triggered_at=row.transitioned_at,
                    triggered_by=row.triggered_by,
                    reason=row.reason or "",
                )
            )
        return actions

    async def _row_to_case(
        self,
        row,
        links_by_hyp: Optional[Dict[str, builtins.list[HypothesisEvidenceLink]]] = None,
    ) -> Case:
        """Reconstruct Case domain object from a SELECT row.

        Post-redesign: ``description``, ``investigation_strategy``,
        ``current_turn``, ``turns_without_progress``, ``closure_reason``,
        ``last_activity_at``, ``resolved_at``, ``closed_at`` all come
        directly from first-class columns. ``is_archived`` /
        ``archived_at`` are gone. Hypothesis evidence_links come from the
        ``hypothesis_evidence`` junction table (the JSON blob is gone),
        loaded by the caller and passed in via ``links_by_hyp``.

        Evidence is NOT populated here — the caller (``get`` /
        ``list``) loads it separately via ``_load_evidence_for_case`` /
        bulk equivalent.
        """
        if links_by_hyp is None:
            links_by_hyp = {}

        def _maybe_load(value: Any) -> Any:
            """JSONB columns arrive as Python objects on PG; legacy
            TEXT rows arrive as JSON-serialized strings. Accept both."""
            if value is None:
                return None
            if isinstance(value, (dict, list)):
                return value
            return json.loads(value)

        inquiry_data = _maybe_load(row.inquiry) or {}
        inquiry = InquiryData(**inquiry_data) if inquiry_data else InquiryData()
        problem_verification = (
            ProblemVerification(**_maybe_load(row.problem_verification))
            if row.problem_verification
            else None
        )
        working_conclusion = (
            WorkingConclusion(**_maybe_load(row.working_conclusion))
            if row.working_conclusion
            else None
        )
        root_cause_conclusion = (
            RootCauseConclusion(**_maybe_load(row.root_cause_conclusion))
            if row.root_cause_conclusion
            else None
        )
        escalation_state = (
            EscalationState(**_maybe_load(row.escalation_state))
            if row.escalation_state
            else None
        )
        documentation = (
            DocumentationData(**_maybe_load(row.documentation))
            if row.documentation
            else DocumentationData()
        )
        progress = (
            InvestigationProgress(**_maybe_load(row.progress))
            if row.progress
            else InvestigationProgress()
        )

        # Parse aggregated JSON sub-collections.
        hypotheses_payload = _maybe_load(row.hypotheses_data) or []
        solutions_payload = _maybe_load(row.solutions_data) or []
        uploaded_files_payload = _maybe_load(row.uploaded_files_data) or []
        messages_payload = _maybe_load(row.messages_data) or []

        # Hydrate hypothesis_evidence links onto each hypothesis.
        hypotheses_dict: Dict[str, Hypothesis] = {}
        for h in hypotheses_payload:
            hyp_id = h["hypothesis_id"]
            h["evidence_links"] = links_by_hyp.get(hyp_id, [])
            hypotheses_dict[hyp_id] = Hypothesis(**h)

        solutions_list = [Solution(**s) for s in solutions_payload]
        uploaded_files = [UploadedFile(**f) for f in uploaded_files_payload]

        # Promoted columns: read directly from the row.
        metadata = _maybe_load(getattr(row, "metadata", None)) or {}

        # ``description`` is now a first-class column. Auto-heal the
        # legacy case where an INVESTIGATING row lost its description
        # (rare; pre-redesign rows that fell through the migration).
        description = row.description or ""
        if (
            CaseState(row.state) == CaseState.INVESTIGATING
            and (not description or not description.strip())
            and inquiry.proposed_problem_statement
        ):
            description = inquiry.proposed_problem_statement
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Auto-healed missing description for case {row.case_id} "
                    f"from proposed_problem_statement"
                )

        # Hydrate the audit trail. PG's _row_to_case is async and is used
        # by both get() and list() (list calls get() per case_id), so the
        # extra round-trip is acceptable at list granularity.
        actions_data = await self._load_case_actions(row.case_id)

        case_data: Dict[str, Any] = {
            "case_id": row.case_id,
            "user_id": row.user_id,
            "organization_id": row.organization_id,
            "title": row.title,
            "state": CaseState(row.state),
            "action_history": actions_data,
            "closure_reason": row.closure_reason,
            "disposition_eligibility": (
                (
                    json.loads(row.disposition_eligibility)
                    if isinstance(row.disposition_eligibility, str)
                    else row.disposition_eligibility
                )
                if getattr(row, "disposition_eligibility", None)
                else None
            ),
            "pending_transition": metadata.get("pending_transition"),
            "progress": progress,
            "current_turn": int(row.current_turn or 0),
            "turns_without_progress": int(row.turns_without_progress or 0),
            "message_count": metadata.get("message_count", 0),
            "turn_history": (
                [TurnProgress(**t) for t in metadata.get("turn_history", [])]
                if metadata.get("turn_history")
                else []
            ),
            "proposed_actions": (
                [ProposedAction(**a) for a in metadata.get("proposed_actions", [])]
                if metadata.get("proposed_actions")
                else []
            ),
            "action_attempts": (
                [ActionAttempt(**a) for a in metadata.get("action_attempts", [])]
                if metadata.get("action_attempts")
                else []
            ),
            "inquiry": inquiry,
            "problem_verification": problem_verification,
            "uploaded_files": uploaded_files,
            "evidence": [],  # Loaded separately
            "hypotheses": hypotheses_dict,
            "solutions": solutions_list,
            "messages": messages_payload,
            "working_conclusion": working_conclusion,
            "root_cause_conclusion": root_cause_conclusion,
            "escalation_state": escalation_state,
            "documentation": documentation,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "version": (
                int(row.version)
                if hasattr(row, "version") and row.version is not None
                else 1
            ),
        }

        if description:
            case_data["description"] = description

        if row.investigation_strategy:
            case_data["investigation_strategy"] = InvestigationStrategy(
                row.investigation_strategy
            )

        if row.last_activity_at:
            case_data["last_activity_at"] = row.last_activity_at

        if row.resolved_at:
            case_data["resolved_at"] = row.resolved_at

        if row.closed_at:
            case_data["closed_at"] = row.closed_at

        return Case(**case_data)

    # ========================================================================
    # Report Operations (TD-001: migrated from IReportStore)
    # ========================================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report to PostgreSQL reports table."""
        from datetime import timezone

        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportType,
            RunbookMetadata,
        )
        from faultmaven.utils.serialization import to_json_compatible

        # If this is marked as current, unmark other reports of the same type for this case
        if report.is_current:
            unmark_query = text("""
                UPDATE reports
                SET is_current = FALSE, updated_at = NOW()
                WHERE case_id = :case_id
                  AND report_type = :report_type
                  AND is_current = TRUE
            """)
            await self.db.execute(
                unmark_query,
                {"case_id": report.case_id, "report_type": report.report_type.value},
            )

        # Insert report
        metadata_json = (
            json.dumps(report.metadata.model_dump(mode="json"))
            if report.metadata
            else "{}"
        )

        # ``organization_id`` is NOT NULL FK CASCADE on reports; derive
        # it from the parent case via subquery so callers don't have to
        # thread it through. ``report_type`` CHECK allows only
        # ('resolution_summary', 'closure_summary').
        insert_query = text("""
            INSERT INTO reports (
                report_id, case_id, organization_id, report_type, version, is_current,
                linked_to_closure, title, content, format,
                generation_status, generation_time_ms, metadata,
                generated_at, updated_at, generated_by
            ) VALUES (
                :report_id, :case_id,
                (SELECT organization_id FROM cases WHERE case_id = :case_id),
                :report_type, :version, :is_current,
                :linked_to_closure, :title, :content, :format,
                :generation_status, :generation_time_ms, :metadata::jsonb,
                :generated_at::timestamptz, :updated_at::timestamptz, :generated_by
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
                updated_at = EXCLUDED.updated_at,
                generated_by = EXCLUDED.generated_by
        """)

        now = datetime.now(timezone.utc)
        generated_at = (
            datetime.fromisoformat(report.generated_at.replace("Z", "+00:00"))
            if isinstance(report.generated_at, str)
            else now
        )
        # Use report.updated_at if set, otherwise use generated_at (for new reports)
        if report.updated_at:
            updated_at = (
                datetime.fromisoformat(report.updated_at.replace("Z", "+00:00"))
                if isinstance(report.updated_at, str)
                else now
            )
        else:
            updated_at = generated_at  # New reports: updated_at same as generated_at (None -> use generated_at)

        await self.db.execute(
            insert_query,
            {
                "report_id": report.report_id,
                "case_id": report.case_id,
                "report_type": report.report_type.value,
                "version": report.version,
                "is_current": report.is_current,
                "linked_to_closure": report.linked_to_closure,
                "title": report.title,
                "content": report.content,
                "format": report.format,
                "generation_status": report.generation_status.value,
                "generation_time_ms": report.generation_time_ms,
                "metadata": metadata_json,
                "generated_at": generated_at,
                "updated_at": updated_at,
                # Auto-generated terminal summaries have no human author,
                # so generated_by is NULL. Explicit user_id threading via
                # API routes deferred.
                "generated_by": getattr(report, "generated_by", None),
            },
        )

        await self.db.commit()
        return report

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Get report by ID from PostgreSQL."""
        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportStatus,
            ReportType,
            RunbookMetadata,
        )
        from faultmaven.utils.serialization import to_json_compatible

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
    ) -> List["CaseReport"]:
        """Get reports for a case with optional filtering."""
        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportStatus,
            ReportType,
            RunbookMetadata,
        )
        from faultmaven.utils.serialization import to_json_compatible

        conditions = ["case_id = :case_id"]
        params = {"case_id": case_id}

        if report_type:
            conditions.append("report_type = :report_type")
            params["report_type"] = report_type.value

        if only_current or not include_history:
            conditions.append("is_current = TRUE")

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

    async def count_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
    ) -> int:
        """Count persisted reports for a case (all versions, not only current)."""
        conditions = ["case_id = :case_id"]
        params: dict[str, Any] = {"case_id": case_id}
        if report_type:
            conditions.append("report_type = :report_type")
            params["report_type"] = report_type.value
        query = text(f"SELECT COUNT(*) FROM reports WHERE {' AND '.join(conditions)}")
        result = await self.db.execute(query, params)
        row = result.fetchone()
        return int(row[0]) if row else 0

    async def update_report(self, report: "CaseReport") -> "CaseReport":
        """Update report in PostgreSQL."""
        from datetime import timezone

        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportType,
            RunbookMetadata,
        )
        from faultmaven.utils.serialization import to_json_compatible

        # If this is marked as current, unmark other reports of the same type for this case
        if report.is_current:
            unmark_query = text("""
                UPDATE reports
                SET is_current = FALSE, updated_at = NOW()
                WHERE case_id = :case_id
                  AND report_type = :report_type
                  AND report_id != :report_id
                  AND is_current = TRUE
            """)
            await self.db.execute(
                unmark_query,
                {
                    "case_id": report.case_id,
                    "report_type": report.report_type.value,
                    "report_id": report.report_id,
                },
            )

        # Update report
        metadata_json = (
            json.dumps(report.metadata.model_dump(mode="json"))
            if report.metadata
            else "{}"
        )
        now = datetime.now(timezone.utc)
        # Use report.updated_at if set, otherwise use current time (for updates, always refresh)
        if report.updated_at:
            updated_at = (
                datetime.fromisoformat(report.updated_at.replace("Z", "+00:00"))
                if isinstance(report.updated_at, str)
                else now
            )
        else:
            updated_at = now  # Default to current time if not set

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
                metadata = :metadata::jsonb,
                updated_at = :updated_at::timestamptz
            WHERE report_id = :report_id
        """)

        result = await self.db.execute(
            update_query,
            {
                "report_id": report.report_id,
                "version": report.version,
                "is_current": report.is_current,
                "linked_to_closure": report.linked_to_closure,
                "title": report.title,
                "content": report.content,
                "format": report.format,
                "generation_status": report.generation_status.value,
                "generation_time_ms": report.generation_time_ms,
                "metadata": metadata_json,
                "updated_at": updated_at,
            },
        )

        await self.db.commit()

        if result.rowcount == 0:
            raise RepositoryException(f"Report {report.report_id} not found")

        return report

    async def delete_report(self, report_id: str) -> bool:
        """Delete report from PostgreSQL."""
        delete_query = text("""
            DELETE FROM reports
            WHERE report_id = :report_id
        """)

        result = await self.db.execute(delete_query, {"report_id": report_id})
        await self.db.commit()

        return result.rowcount > 0

    def _row_to_report(self, row) -> "CaseReport":
        """Convert database row to CaseReport domain object."""
        from faultmaven.modules.case.domain.owned_models.report import (
            CaseReport,
            ReportStatus,
            ReportType,
            RunbookMetadata,
        )
        from faultmaven.utils.serialization import to_json_compatible

        # Parse metadata if present
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

        # Convert timestamps to ISO 8601 strings (ensuring UTC consistency)
        # PostgreSQL TIMESTAMP WITH TIME ZONE is stored in UTC but may return in session timezone
        # Normalize to UTC explicitly to avoid timezone jitter between implementations
        if row.generated_at:
            # Ensure UTC: if timezone-aware, convert to UTC; if naive, assume UTC
            gen_dt = row.generated_at
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            elif gen_dt.tzinfo != timezone.utc:
                gen_dt = gen_dt.astimezone(timezone.utc)
            generated_at = to_json_compatible(gen_dt)
        else:
            generated_at = to_json_compatible(datetime.now(timezone.utc))

        if row.updated_at:
            # Ensure UTC: if timezone-aware, convert to UTC; if naive, assume UTC
            upd_dt = row.updated_at
            if upd_dt.tzinfo is None:
                upd_dt = upd_dt.replace(tzinfo=timezone.utc)
            elif upd_dt.tzinfo != timezone.utc:
                upd_dt = upd_dt.astimezone(timezone.utc)
            updated_at = to_json_compatible(upd_dt)
        else:
            updated_at = generated_at  # Fallback to generated_at if NULL

        return CaseReport(
            report_id=row.report_id,
            case_id=row.case_id,
            report_type=ReportType(row.report_type),
            version=row.version,
            is_current=row.is_current,
            linked_to_closure=row.linked_to_closure,
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
    # Agent Execution & Tool Call Persistence (PostgreSQL)
    # Schema reference: docs/architecture/data-and-storage/schemas/case-schema.md §4.11
    # ============================================================

    async def _resolve_organization_id(self, execution: Any, case_id: str) -> str:
        org_id = getattr(execution, "organization_id", None)
        if org_id:
            return org_id
        result = await self.db.execute(
            text("SELECT organization_id FROM cases WHERE case_id = :case_id"),
            {"case_id": case_id},
        )
        row = result.fetchone()
        if not row or not row[0]:
            raise RepositoryException(
                f"Cannot resolve organization_id: case {case_id} not found"
            )
        return str(row[0])

    async def create_agent_execution(self, execution: Any) -> Any:

        try:
            organization_id = await self._resolve_organization_id(
                execution, execution.case_id
            )
            params = agent_mappers.execution_insert_params(execution, organization_id)
            await self.db.execute(
                text("""
                    INSERT INTO agent_executions (
                        execution_id, case_id, organization_id, agent_type,
                        agent_model, status, started_at, completed_at,
                        execution_duration_ms, prompt, response, error_message,
                        token_usage, metadata, session_id, created_at, updated_at
                    ) VALUES (
                        :execution_id, :case_id, :organization_id, :agent_type,
                        :agent_model, :status, :started_at, :completed_at,
                        :execution_duration_ms, :prompt, :response, :error_message,
                        :token_usage, :metadata, :session_id, :created_at, :updated_at
                    )
                """),
                params,
            )
            await self.db.commit()
            execution.organization_id = organization_id
            execution.tool_calls = []
            return execution
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to create agent execution {execution.execution_id}: {e}"
            ) from e

    async def get_agent_execution(self, execution_id: str) -> Optional[Any]:

        result = await self.db.execute(
            text("""
                SELECT execution_id, case_id, organization_id, agent_type,
                       agent_model, status, started_at, completed_at,
                       execution_duration_ms, prompt, response, error_message,
                       token_usage, metadata AS metadata, created_at, updated_at
                FROM agent_executions
                WHERE execution_id = :execution_id
            """),
            {"execution_id": execution_id},
        )
        row = result.fetchone()
        if not row:
            return None
        tool_call_rows = await self._fetch_tool_call_rows(execution_id)
        return agent_mappers.row_to_execution(row, tool_call_rows)

    async def list_agent_executions_by_case(
        self,
        case_id: str,
        status: Optional[str] = None,
        agent_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        return await self._list_executions(
            where_clause="case_id = :case_id",
            where_params={"case_id": case_id},
            status=status,
            agent_type=agent_type,
            order_by="created_at DESC",
            limit=limit,
            offset=offset,
        )

    async def list_agent_executions_by_session(
        self,
        session_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[List[Any], int]:
        return await self._list_executions(
            where_clause="session_id = :session_id",
            where_params={"session_id": session_id},
            status=status,
            agent_type=None,
            order_by="created_at ASC",
            limit=limit,
            offset=offset,
        )

    async def _list_executions(
        self,
        where_clause: str,
        where_params: dict,
        status: Optional[str],
        agent_type: Optional[str],
        order_by: str,
        limit: int,
        offset: int,
    ) -> tuple[List[Any], int]:

        conditions = [where_clause]
        params: dict[str, Any] = dict(where_params)
        if status is not None:
            status_val = status.value if hasattr(status, "value") else str(status)
            conditions.append("status = :status")
            params["status"] = status_val
        if agent_type is not None:
            agent_type_val = (
                agent_type.value if hasattr(agent_type, "value") else str(agent_type)
            )
            conditions.append("agent_type = :agent_type")
            params["agent_type"] = agent_type_val
        where_sql = " AND ".join(conditions)

        count_result = await self.db.execute(
            text(f"SELECT COUNT(*) FROM agent_executions WHERE {where_sql}"),
            params,
        )
        total = int(count_result.scalar() or 0)

        page_params = dict(params)
        page_params["limit"] = limit
        page_params["offset"] = offset
        page_result = await self.db.execute(
            text(f"""
                SELECT execution_id, case_id, organization_id, agent_type,
                       agent_model, status, started_at, completed_at,
                       execution_duration_ms, prompt, response, error_message,
                       token_usage, metadata AS metadata, created_at, updated_at
                FROM agent_executions
                WHERE {where_sql}
                ORDER BY {order_by}
                LIMIT :limit OFFSET :offset
            """),
            page_params,
        )
        rows = page_result.fetchall()

        executions: List[Any] = []
        for row in rows:
            tool_call_rows = await self._fetch_tool_call_rows(row.execution_id)
            executions.append(agent_mappers.row_to_execution(row, tool_call_rows))
        return executions, total

    async def update_agent_execution(self, execution: Any) -> Any:

        try:
            execution.updated_at = datetime.now(timezone.utc)
            params = agent_mappers.execution_update_params(execution)
            result = await self.db.execute(
                text("""
                    UPDATE agent_executions
                    SET agent_type = :agent_type,
                        agent_model = :agent_model,
                        status = :status,
                        started_at = :started_at,
                        completed_at = :completed_at,
                        execution_duration_ms = :execution_duration_ms,
                        prompt = :prompt,
                        response = :response,
                        error_message = :error_message,
                        token_usage = :token_usage,
                        metadata = :metadata,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                """),
                params,
            )
            await self.db.commit()
            if result.rowcount == 0:
                raise RepositoryException(
                    f"Agent execution {execution.execution_id} not found"
                )
            execution.tool_calls = await self.get_agent_tool_calls_for_execution(
                execution.execution_id
            )
            return execution
        except RepositoryException:
            raise
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update agent execution {execution.execution_id}: {e}"
            ) from e

    async def delete_agent_execution(self, execution_id: str) -> bool:
        # PostgreSQL enforces ON DELETE CASCADE on the agent_tool_calls FK.
        try:
            result = await self.db.execute(
                text("DELETE FROM agent_executions WHERE execution_id = :execution_id"),
                {"execution_id": execution_id},
            )
            await self.db.commit()
            return result.rowcount > 0
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to delete agent execution {execution_id}: {e}"
            ) from e

    async def create_agent_tool_call(self, tool_call: Any) -> Any:

        try:
            organization_id = getattr(tool_call, "organization_id", None)
            if not organization_id:
                result = await self.db.execute(
                    text(
                        "SELECT organization_id FROM agent_executions "
                        "WHERE execution_id = :execution_id"
                    ),
                    {"execution_id": tool_call.execution_id},
                )
                row = result.fetchone()
                if not row or not row[0]:
                    raise RepositoryException(
                        f"Cannot resolve organization_id for tool call "
                        f"{tool_call.tool_call_id}: parent execution "
                        f"{tool_call.execution_id} not found"
                    )
                organization_id = str(row[0])
            params = agent_mappers.tool_call_insert_params(tool_call, organization_id)
            await self.db.execute(
                text("""
                    INSERT INTO agent_tool_calls (
                        tool_call_id, execution_id, organization_id, tool_name,
                        tool_input, tool_output, status, error_message,
                        started_at, completed_at, duration_ms,
                        created_at, updated_at
                    ) VALUES (
                        :tool_call_id, :execution_id, :organization_id, :tool_name,
                        :tool_input, :tool_output, :status, :error_message,
                        :started_at, :completed_at, :duration_ms,
                        :created_at, :updated_at
                    )
                """),
                params,
            )
            await self.db.commit()
            tool_call.organization_id = organization_id
            return tool_call
        except RepositoryException:
            raise
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to create tool call {tool_call.tool_call_id}: {e}"
            ) from e

    async def update_agent_tool_call(self, tool_call: Any) -> Any:

        try:
            tool_call.updated_at = datetime.now(timezone.utc)
            params = agent_mappers.tool_call_update_params(tool_call)
            result = await self.db.execute(
                text("""
                    UPDATE agent_tool_calls
                    SET tool_name = :tool_name,
                        tool_input = :tool_input,
                        tool_output = :tool_output,
                        status = :status,
                        error_message = :error_message,
                        started_at = :started_at,
                        completed_at = :completed_at,
                        duration_ms = :duration_ms,
                        updated_at = :updated_at
                    WHERE tool_call_id = :tool_call_id
                """),
                params,
            )
            await self.db.commit()
            if result.rowcount == 0:
                raise RepositoryException(
                    f"Tool call {tool_call.tool_call_id} not found"
                )
            return tool_call
        except RepositoryException:
            raise
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to update tool call {tool_call.tool_call_id}: {e}"
            ) from e

    async def get_agent_tool_calls_for_execution(self, execution_id: str) -> List[Any]:

        rows = await self._fetch_tool_call_rows(execution_id)
        return [agent_mappers.row_to_tool_call(row) for row in rows]

    async def _fetch_tool_call_rows(self, execution_id: str) -> List[Any]:
        result = await self.db.execute(
            text("""
                SELECT tool_call_id, execution_id, organization_id, tool_name,
                       tool_input, tool_output, status, error_message,
                       started_at, completed_at, duration_ms,
                       created_at, updated_at
                FROM agent_tool_calls
                WHERE execution_id = :execution_id
                ORDER BY created_at ASC
            """),
            {"execution_id": execution_id},
        )
        return list(result.fetchall())

    async def count_agent_executions_by_case(self, case_id: str) -> int:
        result = await self.db.execute(
            text("SELECT COUNT(*) FROM agent_executions WHERE case_id = :case_id"),
            {"case_id": case_id},
        )
        return int(result.scalar() or 0)

    async def get_latest_agent_execution(
        self,
        case_id: str,
        agent_type: Optional[str] = None,
    ) -> Optional[Any]:

        conditions = ["case_id = :case_id"]
        params: dict[str, Any] = {"case_id": case_id}
        if agent_type is not None:
            agent_type_val = (
                agent_type.value if hasattr(agent_type, "value") else str(agent_type)
            )
            conditions.append("agent_type = :agent_type")
            params["agent_type"] = agent_type_val
        where_sql = " AND ".join(conditions)

        result = await self.db.execute(
            text(f"""
                SELECT execution_id, case_id, organization_id, agent_type,
                       agent_model, status, started_at, completed_at,
                       execution_duration_ms, prompt, response, error_message,
                       token_usage, metadata AS metadata, created_at, updated_at
                FROM agent_executions
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT 1
            """),
            params,
        )
        row = result.fetchone()
        if not row:
            return None
        tool_call_rows = await self._fetch_tool_call_rows(row.execution_id)
        return agent_mappers.row_to_execution(row, tool_call_rows)

    # ========================================================================
    # Checkpoint Operations (TASK-028)
    # ========================================================================

    async def create_checkpoint(self, checkpoint: CaseCheckpoint) -> CaseCheckpoint:
        """Create a new case checkpoint (PostgreSQL)."""
        from faultmaven.utils.serialization import to_json_compatible

        try:
            query = text("""
                INSERT INTO case_checkpoints (
                    checkpoint_id, case_id, organization_id, turn_number, case_snapshot,
                    snapshot_hash, trigger, created_at, metadata
                ) VALUES (
                    :checkpoint_id, :case_id,
                    (SELECT COALESCE(organization_id, '00000000-0000-0000-0000-000000000001') FROM cases WHERE case_id = :case_id),
                    :turn_number, :case_snapshot::jsonb,
                    :snapshot_hash, :trigger, :created_at::timestamptz, :metadata::jsonb
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
        """Get a checkpoint by ID (PostgreSQL)."""
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
        """Get all checkpoints for a case (PostgreSQL)."""
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
        snapshot_data = row.case_snapshot
        if isinstance(snapshot_data, str):
            snapshot_data = json.loads(snapshot_data)

        metadata = row.metadata
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        created_at = row.created_at
        if created_at and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        return CaseCheckpoint(
            checkpoint_id=row.checkpoint_id,
            case_id=row.case_id,
            turn_number=row.turn_number,
            case_snapshot=snapshot_data or {},
            snapshot_hash=row.snapshot_hash,
            trigger=row.trigger,
            created_at=created_at,
            metadata=metadata or {},
        )


class RepositoryException(Exception):
    """Exception raised for repository errors."""

    pass
