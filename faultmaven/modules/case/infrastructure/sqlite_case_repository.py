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
    - case_actions (1:N normalized table)
"""

import builtins
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseCheckpoint,
    CaseEntity,
    CaseReport,
    CaseStatus,
    DocumentationData,
    EntityType,
    EscalationState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisEvidenceLink,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    PathSelection,
    ProblemVerification,
    ProposedAction,
    ReportStatus,
    ReportType,
    RootCauseConclusion,
    RunbookMetadata,
    Solution,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    _agent_execution_mappers as agent_mappers,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.serialization import to_json_compatible

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _row_to_case_entity(row: Any) -> CaseEntity:
    """Build a domain ``CaseEntity`` from a SELECT row.

    Columns in fixed order:
    ``(case_id, entity_type, entity_value, evidence_id,
        mention_count, in_error_context, first_seen_ts)``
    """
    entity_type_str = row[1]
    try:
        entity_type = EntityType(entity_type_str)
    except ValueError:
        # Registry may contain stale values from retired enum members.
        # Fall back to the first type to keep the read path non-
        # failing; the agent will see the value but not be able to
        # filter on type meaningfully.
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


def _serialize_tags(tags: Optional[List[str]]) -> Optional[str]:
    """Serialize an Evidence.tags list to the SQLite TEXT column.

    Empty list and None both round-trip as NULL. Comma-separated for
    SQLite (Pydantic's ``_no_commas_in_tags`` validator forbids commas
    inside individual tag values, which keeps the round-trip lossless).
    """
    if not tags:
        return None
    return ",".join(tags)


def _deserialize_tags(value: Optional[str]) -> List[str]:
    """Inverse of ``_serialize_tags``. Empty/None → empty list."""
    if not value:
        return []
    return [t for t in value.split(",") if t]


_STANCE_TO_RELATIONSHIP: Dict[EvidenceStance, str] = {
    EvidenceStance.SUPPORTS: "supports",
    EvidenceStance.REFUTES: "refutes",
    # The junction CHECK constraint allows ('supports', 'refutes', 'related').
    # Map domain NEUTRAL → 'related' (the closest neutral-not-irrelevant slot).
    EvidenceStance.NEUTRAL: "related",
}

_RELATIONSHIP_TO_STANCE: Dict[str, EvidenceStance] = {
    "supports": EvidenceStance.SUPPORTS,
    "refutes": EvidenceStance.REFUTES,
    "related": EvidenceStance.NEUTRAL,
}


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
        """Save case using hybrid schema with transactions.

        Optimistic concurrency control is enforced inside
        `_upsert_case_record`: the in-memory `case.version` is checked
        against the DB row, and `StaleCaseException` is raised on
        mismatch. On success the same `case` instance is mutated with
        the new version and returned — callers can use either the
        return value or the passed-in object.
        """
        # Defense in depth: catch swallowed validation exceptions or bypassed validators
        # by re-verifying the entire aggregate state before persisting.
        Case.model_validate(case.model_dump(mode="python"))

        try:
            case.updated_at = datetime.now(UTC)

            organization_id = case.organization_id
            await self._upsert_case_record(case)
            # evidence.source_file_id is a real FK to uploaded_files.file_id,
            # so files must exist before any evidence row that references them.
            await self._upsert_uploaded_files(
                case.case_id, case.uploaded_files, organization_id
            )
            await self._upsert_evidence(case.case_id, case.evidence, organization_id)
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
            # Propagate unwrapped so callers can retry or surface 409
            # without unwrapping a generic RepositoryException.
            await self.db.rollback()
            raise
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
            actions_data = await self._load_case_actions(case_id)

            # Reconstruct Case domain object
            case = self._row_to_case(
                row,
                hypotheses_data,
                solutions_data,
                uploaded_files_data,
                messages_data,
                actions_data,
            )

            # Load evidence directly
            if case:
                await self._load_evidence_for_case(case)

            return case

        except Exception as e:
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    async def _load_hypotheses(self, case_id: str) -> list[dict]:
        """Load hypotheses for a case.

        Returns dicts keyed by hypothesis_id with ``evidence_links`` set to
        a List[HypothesisEvidenceLink] hydrated from the
        ``hypothesis_evidence`` junction table (not the dropped
        ``hypotheses.evidence_links`` JSON blob).
        """
        query = text("""
            SELECT hypothesis_id, statement, status, likelihood, initial_likelihood,
                   generated_at_turn, last_updated_turn, last_progress_at_turn,
                   iterations_without_progress,
                   category, generation_mode, rationale, retirement_reason,
                   refutation_reason,
                   tested_at, concluded_at, proposed_at, updated_at, metadata
            FROM hypotheses
            WHERE case_id = :case_id
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()

        hypothesis_ids = [row[0] for row in rows]
        links_by_hyp = await self._load_hypothesis_evidence_links(hypothesis_ids)

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
                    "refutation_reason": row[13],
                    "evidence_links": links_by_hyp.get(row[0], []),
                    "tested_at": row[14],
                    "concluded_at": row[15],
                    "proposed_at": row[16],
                    "updated_at": row[17],
                    "metadata": json.loads(row[18]) if row[18] else {},
                }
            )
        return hypotheses

    async def _load_hypothesis_evidence_links(
        self, hypothesis_ids: builtins.list[str]
    ) -> dict[str, builtins.list[HypothesisEvidenceLink]]:
        """Load junction-table rows and return them as
        ``{hypothesis_id: [HypothesisEvidenceLink, ...]}``.

        Empty input returns ``{}``. Hypotheses with no links are absent
        from the result (callers default to an empty list per hypothesis).
        """
        if not hypothesis_ids:
            return {}
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, hypothesis_ids)
        # Reuse _bind_ids for hypothesis_ids by re-keying — _bind_ids
        # produces ``cid_<i>`` keys but we just need any unique placeholders.
        query = text(f"""
            SELECT hypothesis_id, evidence_id, relationship_type, confidence,
                   linked_at_turn, created_at
            FROM hypothesis_evidence
            WHERE hypothesis_id IN ({placeholders})
        """)
        result = await self.db.execute(query, params)
        rows = result.fetchall()

        by_hyp: dict[str, builtins.list[HypothesisEvidenceLink]] = {}
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
                    analyzed_at = datetime.now(UTC)
            elif analyzed_at is None:
                analyzed_at = datetime.now(UTC)
            link = HypothesisEvidenceLink(
                hypothesis_id=str(hyp_id),
                evidence_id=str(row[1]),
                stance=stance,
                # ``reasoning`` is required on the Pydantic model but the
                # junction table doesn't carry the LLM's free-text rationale —
                # it lives on case_messages / agent reasoning logs. Persist
                # an empty marker; the Pydantic validator allows any string.
                reasoning="",
                stance_confidence=max(0.0, min(1.0, confidence)),
                analyzed_at=analyzed_at,
            )
            by_hyp.setdefault(str(hyp_id), []).append(link)
        return by_hyp

    async def _load_solutions(self, case_id: str) -> list[dict]:
        """Load solutions for a case.

        Selects the full audit trail (proposed_by, applied_at/by,
        verified_at, verification_method, verification_evidence_id,
        effectiveness) so ``Solution(**s)`` reconstruction is faithful
        to what was persisted. Pre-009 this loader returned only a
        subset, leaving every audit field at its Pydantic default.
        """
        query = text("""
            SELECT solution_id, solution_type, title, immediate_action, longterm_fix,
                   implementation_steps, commands, risks,
                   proposed_at, proposed_by,
                   applied_at, applied_by,
                   verified_at, verification_method,
                   verification_evidence_id, effectiveness
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
                    "proposed_by": row[9],
                    "applied_at": row[10],
                    "applied_by": row[11],
                    "verified_at": row[12],
                    "verification_method": row[13],
                    "verification_evidence_id": row[14],
                    "effectiveness": row[15],
                }
            )
        return solutions

    async def _load_uploaded_files(self, case_id: str) -> list[dict]:
        """Load uploaded files for a case.

        Schema columns: ``file_id``, ``filename``, ``size_bytes``,
        ``content_type`` (MIME), ``content_hash``, ``storage_ref``,
        ``upload_source``, ``uploaded_at_turn``, ``uploaded_at``,
        ``uploaded_by``, plus the preprocessing artifacts (``summary``,
        ``structural_index``, ``data_type``, ``coverage_start_ts``,
        ``coverage_end_ts``).
        """
        query = text("""
            SELECT file_id, filename, size_bytes, content_type, content_hash,
                   storage_ref, upload_source, uploaded_at_turn, uploaded_at,
                   uploaded_by,
                   summary, structural_index, data_type,
                   coverage_start_ts, coverage_end_ts
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
                    "content_type": row_dict.get("content_type"),
                    "content_hash": row_dict.get("content_hash"),
                    "storage_ref": row_dict.get("storage_ref"),
                    "upload_source": row_dict.get("upload_source", "file_upload"),
                    "uploaded_at_turn": row_dict.get("uploaded_at_turn", 0),
                    "uploaded_at": row_dict.get("uploaded_at"),
                    "uploaded_by": row_dict.get("uploaded_by"),
                    "summary": row_dict.get("summary"),
                    "structural_index": row_dict.get("structural_index"),
                    "data_type": row_dict.get("data_type"),
                    "coverage_start_ts": row_dict.get("coverage_start_ts"),
                    "coverage_end_ts": row_dict.get("coverage_end_ts"),
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
        """Load investigation evidence from the evidence table.

        Columns selected (in this fixed order, consumed positionally by
        ``_row_to_evidence``): ``evidence_id``, ``category``,
        ``source_type``, ``summary``, ``extract``, ``is_primary``,
        ``reliability_score``, ``tags``, ``collected_at_turn``,
        ``source_file_id``, ``vectorized``, ``coverage_start_ts``,
        ``coverage_end_ts``, ``metadata``, ``created_at``,
        ``primary_purpose``, ``analysis``, ``processing_mode``,
        ``advances_milestones``, ``collected_by``.
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
            logging.getLogger(__name__).warning(
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
            # Strict category validation — bad rows fail loudly. Every row
            # is born with a valid 4-category classification.
            category = EvidenceCategory(row[1])

            source_type = EvidenceSourceType(row[2]) if row[2] else None

            metadata_raw = row[13]
            parsed_metadata: Optional[Dict[str, Any]] = None
            if metadata_raw:
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
                    collected_at = datetime.now(UTC)
            elif collected_at is None:
                collected_at = datetime.now(UTC)

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
                tags=_deserialize_tags(row[7]),
                advances_milestones=_deserialize_tags(row[18]),
                collected_by=row[19] or "system",
                collected_at=collected_at,
                collected_at_turn=row[8] if row[8] else 0,
                vectorized=bool(row[10]),
                metadata=parsed_metadata,
                coverage_start_ts=row[11],
                coverage_end_ts=row[12],
            )
        except Exception as ev_err:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Failed to load evidence %s: %s", row[0], ev_err
            )
            return None

    # ========================================================================
    # Bulk loaders — used by list() to avoid the N+1 per-case fan-out that
    # would otherwise run (N * 5) queries for a page of N cases. Each loader
    # issues one SELECT with WHERE case_id IN (:ids) and groups results by
    # case_id in Python. Shapes mirror the per-case _load_* helpers above so
    # _row_to_case can consume either.
    # ========================================================================

    def _bind_ids(self, params: dict[str, Any], ids: builtins.list[str]) -> str:
        """Expand a list of case_ids into named parameters. Returns the
        SQL placeholder clause (``:cid_0, :cid_1, ...``) to splice into
        an ``IN (...)`` and mutates ``params`` with the values."""
        names = []
        for i, cid in enumerate(ids):
            key = f"cid_{i}"
            params[key] = cid
            names.append(f":{key}")
        return ", ".join(names)

    async def _load_hypotheses_bulk(
        self, case_ids: builtins.list[str]
    ) -> dict[str, builtins.list[dict]]:
        if not case_ids:
            return {}
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, case_ids)
        query = text(f"""
            SELECT case_id, hypothesis_id, statement, status, likelihood,
                   initial_likelihood, generated_at_turn, last_updated_turn,
                   last_progress_at_turn, iterations_without_progress,
                   category, generation_mode, rationale, retirement_reason,
                   refutation_reason, tested_at, concluded_at,
                   proposed_at, updated_at, metadata
            FROM hypotheses
            WHERE case_id IN ({placeholders})
        """)
        rows = (await self.db.execute(query, params)).fetchall()

        # Hydrate evidence_links from the junction table for every hypothesis
        # we just loaded, in one round-trip.
        all_hyp_ids = [row[1] for row in rows]
        links_by_hyp = await self._load_hypothesis_evidence_links(all_hyp_ids)

        by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
        for row in rows:
            by_case.setdefault(row[0], []).append(
                {
                    "hypothesis_id": row[1],
                    "statement": row[2],
                    "status": row[3],
                    "likelihood": row[4],
                    "initial_likelihood": row[5],
                    "generated_at_turn": row[6] or 0,
                    "last_updated_turn": row[7],
                    "last_progress_at_turn": row[8],
                    "iterations_without_progress": row[9],
                    "category": row[10],
                    "generation_mode": row[11],
                    "rationale": row[12],
                    "retirement_reason": row[13],
                    "refutation_reason": row[14],
                    "evidence_links": links_by_hyp.get(row[1], []),
                    "tested_at": row[15],
                    "concluded_at": row[16],
                    "proposed_at": row[17],
                    "updated_at": row[18],
                    "metadata": json.loads(row[19]) if row[19] else {},
                }
            )
        return by_case

    async def _load_solutions_bulk(
        self, case_ids: builtins.list[str]
    ) -> dict[str, builtins.list[dict]]:
        if not case_ids:
            return {}
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, case_ids)
        query = text(f"""
            SELECT case_id, solution_id, solution_type, title, immediate_action,
                   longterm_fix, implementation_steps, commands, risks,
                   proposed_at, proposed_by,
                   applied_at, applied_by,
                   verified_at, verification_method,
                   verification_evidence_id, effectiveness
            FROM solutions
            WHERE case_id IN ({placeholders})
        """)
        rows = (await self.db.execute(query, params)).fetchall()

        by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
        for row in rows:
            by_case.setdefault(row[0], []).append(
                {
                    "solution_id": row[1],
                    "solution_type": row[2] or "other",
                    "title": row[3] or "Untitled solution",
                    "immediate_action": row[4],
                    "longterm_fix": row[5],
                    "implementation_steps": json.loads(row[6]) if row[6] else [],
                    "commands": json.loads(row[7]) if row[7] else [],
                    "risks": json.loads(row[8]) if row[8] else [],
                    "proposed_at": row[9],
                    "proposed_by": row[10],
                    "applied_at": row[11],
                    "applied_by": row[12],
                    "verified_at": row[13],
                    "verification_method": row[14],
                    "verification_evidence_id": row[15],
                    "effectiveness": row[16],
                }
            )
        return by_case

    async def _load_uploaded_files_bulk(
        self, case_ids: builtins.list[str]
    ) -> dict[str, builtins.list[dict]]:
        if not case_ids:
            return {}
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, case_ids)
        query = text(f"""
            SELECT case_id, file_id, filename, size_bytes, content_type,
                   content_hash, storage_ref, upload_source, uploaded_at_turn,
                   uploaded_at, uploaded_by,
                   summary, structural_index, data_type,
                   coverage_start_ts, coverage_end_ts
            FROM uploaded_files
            WHERE case_id IN ({placeholders})
        """)
        result = await self.db.execute(query, params)
        rows = result.fetchall()

        by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
        for row in rows:
            by_case.setdefault(row[0], []).append(
                {
                    "file_id": row[1],
                    "filename": row[2],
                    "size_bytes": row[3] or 0,
                    "content_type": row[4],
                    "content_hash": row[5],
                    "storage_ref": row[6],
                    "upload_source": row[7] or "file_upload",
                    "uploaded_at_turn": row[8] or 0,
                    "uploaded_at": row[9],
                    "uploaded_by": row[10],
                    "summary": row[11],
                    "structural_index": row[12],
                    "data_type": row[13],
                    "coverage_start_ts": row[14],
                    "coverage_end_ts": row[15],
                }
            )
        return by_case

    async def _load_messages_bulk(
        self, case_ids: builtins.list[str]
    ) -> dict[str, builtins.list[dict]]:
        if not case_ids:
            return {}
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, case_ids)
        query = text(f"""
            SELECT case_id, message_id, turn_number, role, content,
                   created_at, token_count, metadata
            FROM case_messages
            WHERE case_id IN ({placeholders})
            ORDER BY case_id, created_at ASC
        """)
        rows = (await self.db.execute(query, params)).fetchall()

        by_case: dict[str, builtins.list[dict]] = {cid: [] for cid in case_ids}
        for row in rows:
            msg_timestamp = row[5]
            if msg_timestamp:
                if isinstance(msg_timestamp, str):
                    msg_timestamp = msg_timestamp.replace(" ", "T")
                elif hasattr(msg_timestamp, "isoformat"):
                    msg_timestamp = msg_timestamp.isoformat()

            metadata_raw = row[7]
            if isinstance(metadata_raw, str):
                parsed_metadata = json.loads(metadata_raw) if metadata_raw else {}
            elif metadata_raw is None:
                parsed_metadata = {}
            else:
                parsed_metadata = metadata_raw

            by_case.setdefault(row[0], []).append(
                {
                    "message_id": row[1],
                    "turn_number": row[2] or 0,
                    "role": row[3],
                    "content": row[4],
                    "created_at": msg_timestamp,
                    "token_count": row[6],
                    "metadata": parsed_metadata,
                }
            )
        return by_case

    async def _load_evidence_for_cases_bulk(self, cases: builtins.list[Case]) -> None:
        """Hydrate ``Case.evidence`` on every case in ``cases`` with one
        SELECT. Failures on individual rows are logged and skipped so one
        bad evidence row doesn't blank the whole list.
        """
        if not cases:
            return
        case_ids = [c.case_id for c in cases]
        params: dict[str, Any] = {}
        placeholders = self._bind_ids(params, case_ids)
        try:
            query = text(f"""
                SELECT
                    evidence_id, case_id, category, source_type,
                    summary, extract,
                    is_primary, reliability_score, tags,
                    collected_at_turn, source_file_id, vectorized,
                    coverage_start_ts, coverage_end_ts,
                    metadata, created_at,
                    primary_purpose, analysis, processing_mode,
                    advances_milestones, collected_by
                FROM evidence
                WHERE case_id IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1000
            """)
            rows = (await self.db.execute(query, params)).fetchall()

            by_case: dict[str, builtins.list[Evidence]] = {cid: [] for cid in case_ids}
            for row in rows:
                # Bulk row order is offset by one column (case_id at index 1).
                # _row_to_evidence expects the per-case shape; remap by skipping
                # case_id when calling.
                ev_row = (
                    row[0],  # evidence_id
                    row[2],  # category
                    row[3],  # source_type
                    row[4],  # summary
                    row[5],  # extract
                    row[6],  # is_primary
                    row[7],  # reliability_score
                    row[8],  # tags
                    row[9],  # collected_at_turn
                    row[10],  # source_file_id
                    row[11],  # vectorized
                    row[12],  # coverage_start_ts
                    row[13],  # coverage_end_ts
                    row[14],  # metadata
                    row[15],  # created_at
                    row[16],  # primary_purpose
                    row[17],  # analysis
                    row[18],  # processing_mode
                    row[19],  # advances_milestones
                    row[20],  # collected_by
                )
                ev = self._row_to_evidence(ev_row)
                if ev is not None:
                    by_case.setdefault(row[1], []).append(ev)
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Failed to bulk-load evidence for %d cases: %s", len(cases), e
            )
            return

        for case in cases:
            case.evidence = by_case.get(case.case_id, [])

    async def list(
        self,
        user_id: str | None = None,
        organization_id: str | None = None,
        status: CaseStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Case], int]:
        """List cases with optional filters and pagination.

        Uses batched loads for owned sub-collections (one SELECT per
        table with ``WHERE case_id IN (...)``), then assembles Cases in
        Python. The earlier N+1 pattern — calling ``self.get(cid)`` per
        row — didn't scale past ~30 cases under the per-case 5-table
        fan-out (hypotheses/solutions/uploaded_files/messages/evidence).
        """
        try:
            where_clauses = []
            params: dict[str, Any] = {"limit": limit, "offset": offset}

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

            # Count query.
            count_query = text(f"SELECT COUNT(*) FROM cases {where_sql}")
            count_result = await self.db.execute(count_query, params)
            total_count = count_result.scalar()

            # Page query — fetch full case rows (we need every column for
            # _row_to_case; a later optimization could project only the
            # fields the caller declares it needs).
            list_query = text(f"""
                SELECT *
                FROM cases
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT :limit OFFSET :offset
            """)
            rows = (await self.db.execute(list_query, params)).fetchall()

            if not rows:
                return [], total_count

            case_ids = [row.case_id for row in rows]

            # Batch-load every owned sub-collection with one SELECT each.
            hypotheses_by_case = await self._load_hypotheses_bulk(case_ids)
            solutions_by_case = await self._load_solutions_bulk(case_ids)
            uploaded_files_by_case = await self._load_uploaded_files_bulk(case_ids)
            messages_by_case = await self._load_messages_bulk(case_ids)

            cases: list[Case] = []
            for row in rows:
                try:
                    case = self._row_to_case(
                        row,
                        hypotheses_by_case.get(row.case_id, []),
                        solutions_by_case.get(row.case_id, []),
                        uploaded_files_by_case.get(row.case_id, []),
                        messages_by_case.get(row.case_id, []),
                    )
                    if case:
                        cases.append(case)
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "Skipping case %s in list: deserialization failed: %s",
                        getattr(row, "case_id", "<unknown>"),
                        e,
                    )
                    continue

            # Evidence is loaded last (it's the heaviest table and the only
            # one that needs mutation of the already-constructed Case
            # instances). Same batched shape.
            await self._load_evidence_for_cases_bulk(cases)

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
                # organization_id not on Pydantic UploadedFile (it's a
                # persistence-layer tenancy concern); skip row[1].
                # case_id not on the domain model either; skip row[2].
                uploaded_by=row[3],
                filename=row[4],
                size_bytes=row[5],
                content_type=row[6],
                content_hash=row[7],
                storage_ref=row[8],
                upload_source=row[9],
                uploaded_at_turn=row[10],
                # metadata (row[11]) is a JSON blob — domain UploadedFile
                # doesn't model it today; the dedup path doesn't need it.
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
        timeless evidence isn't time-windowable.
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
                params["end_ts"] = end.isoformat()
            if start is not None:
                where_clauses.append("coverage_end_ts >= :start_ts")
                params["start_ts"] = start.isoformat()

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

        Phase 4 — see ``CaseRepository.upsert_case_entities``. The
        delete scopes to ``(case_id, evidence_id)`` rather than just
        ``evidence_id`` as a belt-and-suspenders check: a bug that
        crossed evidence_ids between cases would otherwise silently
        corrupt the registry.

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
                        "in_error_context": (1 if entity.in_error_context else 0),
                        "first_seen_ts": (
                            entity.first_seen_ts.isoformat()
                            if entity.first_seen_ts
                            else None
                        ),
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
        """Exact-value lookup across evidence in a case.

        Uses ``idx_case_entities_lookup`` (case_id + entity_type +
        entity_value) when ``entity_type`` is supplied; degrades to an
        index scan on the case_id prefix when it isn't.
        """
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
            return [_row_to_case_entity(row) for row in rows]
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
        """Top-N aggregation by entity_value.

        Returns one representative row per distinct entity_value, with
        ``mention_count`` equal to the sum across evidence. The
        representative's ``evidence_id`` is the one with the largest
        individual count (MAX(mention_count) tiebreak).
        """
        try:
            # Aggregate via GROUP BY. SQLite doesn't support ORDER BY
            # on the aggregated count directly in a subquery window
            # under all aiosqlite versions, so we aggregate and sort in
            # two passes via a single SELECT with GROUP BY + ORDER BY.
            query = text("""
                SELECT
                    entity_value,
                    SUM(mention_count) AS total_mentions,
                    MAX(mention_count) AS max_individual,
                    MAX(in_error_context) AS any_error,
                    MIN(first_seen_ts) AS earliest_ts
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
            # Synthesize representative CaseEntity rows. evidence_id is
            # the composite PK constraint in the domain model, so we
            # fetch a representative row per value.
            representatives: List[CaseEntity] = []
            for row in rows:
                value = row[0]
                rep_q = text("""
                    SELECT evidence_id
                    FROM case_entities
                    WHERE case_id = :case_id
                      AND entity_type = :entity_type
                      AND entity_value = :entity_value
                    ORDER BY mention_count DESC
                    LIMIT 1
                    """)
                rep_result = await self.db.execute(
                    rep_q,
                    {
                        "case_id": case_id,
                        "entity_type": entity_type.value,
                        "entity_value": value,
                    },
                )
                rep_row = rep_result.fetchone()
                evidence_id = rep_row[0] if rep_row else ""
                first_seen_ts = row[4]
                representatives.append(
                    CaseEntity(
                        case_id=case_id,
                        entity_type=entity_type,
                        entity_value=value,
                        evidence_id=evidence_id,
                        mention_count=int(row[1]),
                        in_error_context=bool(row[3]),
                        first_seen_ts=first_seen_ts,
                    )
                )
            return representatives
        except Exception as e:
            raise RepositoryException(
                f"Failed to list top entities for case {case_id}: {e}"
            ) from e

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
                "(title LIKE :search_pattern OR title LIKE :search_pattern2 OR case_id LIKE :search_pattern)"
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
            # Pre-check the case exists to keep the (case_id missing → False)
            # contract; the INSERT below would otherwise hit
            # NOT NULL on case_messages.organization_id.
            probe = await self.db.execute(
                text("SELECT 1 FROM cases WHERE case_id = :case_id"),
                {"case_id": case_id},
            )
            if probe.fetchone() is None:
                return False

            message_id = message_dict.get("message_id", f"msg_{uuid4().hex[:16]}")
            created_at = message_dict.get("created_at") or datetime.now(UTC)

            # SQLite-compatible: no ::jsonb type cast
            # organization_id derived from the parent case (already verified
            # to exist by the probe above).
            query = text("""
                INSERT INTO case_messages (message_id, case_id, organization_id, turn_number, role, content, created_at, token_count, metadata)
                VALUES (:message_id, :case_id, (SELECT COALESCE(organization_id, '00000000-0000-0000-0000-000000000001') FROM cases WHERE case_id = :case_id), :turn_number, :role, :content, :created_at, :token_count, :metadata)
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
    # Checkpoint Operations (TASK-028)
    # ========================================================================

    async def create_checkpoint(self, checkpoint: CaseCheckpoint) -> CaseCheckpoint:
        """Create a new case checkpoint (SQLite-compatible)."""
        try:
            query = text("""
                INSERT INTO case_checkpoints (
                    checkpoint_id, case_id, organization_id, turn_number, case_snapshot,
                    snapshot_hash, trigger, created_at, metadata
                ) VALUES (
                    :checkpoint_id, :case_id,
                    (SELECT COALESCE(organization_id, '00000000-0000-0000-0000-000000000001') FROM cases WHERE case_id = :case_id),
                    :turn_number, :case_snapshot,
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
        """Refresh the case's activity timestamp.

        The ORM schema has no dedicated `last_activity_at` column; `updated_at`
        already tracks the last modification time (with an `onupdate=func.now()`
        ORM hook for session-level writes). Raw SQL updates bypass that hook,
        so we set `updated_at` explicitly here.
        """
        try:
            query = text("""
                UPDATE cases
                SET updated_at = datetime('now')
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

    async def update_evidence_vectorized(
        self, case_id: str, evidence_id: str, vectorized: bool
    ) -> bool:
        """Scoped UPDATE of the `vectorized` column on one evidence row.

        Replaces the `save(case)` path previously used by background
        vectorization tasks — that path rewrote the whole case aggregate from
        a potentially stale snapshot and silently truncated newer writes on
        concurrent tables (see milestone_engine._vectorize_evidence).
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
                    "vectorized": 1 if vectorized else 0,
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
        """Scoped DELETE of a single evidence row.

        Explicit alternative to the mirror-delete the aggregate save performs.
        Use this for intentional removals rather than popping from
        `case.evidence` and calling `save(case)`.
        """
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
        """Scoped DELETE of a single uploaded_file row.

        Explicit alternative to the mirror-delete the aggregate save performs.
        """
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
        """Delete closed cases whose ``closed_at`` is older than max_age_days.

        Post-redesign: ``closed_at`` is a first-class column. The DELETE
        compares it directly against the cutoff datetime.
        """
        try:
            query = text("""
                DELETE FROM cases
                WHERE case_id IN (
                    SELECT case_id
                    FROM cases
                    WHERE status = 'closed'
                    AND closed_at IS NOT NULL
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
        """Upsert main cases table (SQLite-compatible - no type casts).

        Optimistic concurrency control: first attempts an UPDATE with a
        version predicate; raises StaleCaseException on version mismatch;
        falls back to INSERT when no row exists for ``case_id``. The
        in-memory ``case.version`` is bumped on successful update so
        subsequent saves within the same flow work without reloading.

        Phase 6 (storage redesign 2026-04): persists ``closure_reason``,
        ``last_activity_at``, ``resolved_at`` and ``closed_at`` to first-class
        columns instead of the ``metadata`` JSON blob. ``last_activity_at`` is
        bumped to the current UTC time on every save so staleness queries
        work without scanning JSON. The legacy ``metadata`` JSON keeps the
        same fields for backward compatibility with rows that pre-date the
        columns.
        """
        last_activity_at = datetime.now(UTC)
        params = self._case_record_params(case, last_activity_at)

        # Step 1: attempt UPDATE with version check.
        update_query = text("""
            UPDATE cases SET
                user_id = :user_id,
                organization_id = :organization_id,
                title = :title,
                description = :description,
                status = :status,
                investigation_strategy = :investigation_strategy,
                current_turn = :current_turn,
                turns_without_progress = :turns_without_progress,
                updated_at = :updated_at,
                closure_reason = :closure_reason,
                last_activity_at = :last_activity_at,
                resolved_at = :resolved_at,
                closed_at = :closed_at,
                inquiry = :inquiry,
                problem_verification = :problem_verification,
                working_conclusion = :working_conclusion,
                root_cause_conclusion = :root_cause_conclusion,
                path_selection = :path_selection,
                escalation_state = :escalation_state,
                documentation = :documentation,
                progress = :progress,
                metadata = :metadata,
                version = :new_version
            WHERE case_id = :case_id AND version = :expected_version
        """)
        expected_version = case.version
        new_version = expected_version + 1
        update_params = {
            **params,
            "expected_version": expected_version,
            "new_version": new_version,
        }
        result = await self.db.execute(update_query, update_params)

        if result.rowcount > 0:
            case.version = new_version
            return

        # Step 2: UPDATE matched no rows — either the case is new, or the
        # version predicate failed. One SELECT to disambiguate.
        probe = await self.db.execute(
            text("SELECT version FROM cases WHERE case_id = :case_id"),
            {"case_id": case.case_id},
        )
        row = probe.fetchone()
        if row is None:
            # New case — INSERT with version = 1.
            insert_query = text("""
                INSERT INTO cases (
                    case_id, user_id, organization_id, title, description,
                    status, investigation_strategy, current_turn,
                    turns_without_progress, created_at, updated_at,
                    closure_reason, last_activity_at, resolved_at, closed_at,
                    inquiry, problem_verification, working_conclusion,
                    root_cause_conclusion, path_selection,
                    escalation_state, documentation, progress, metadata,
                    version
                ) VALUES (
                    :case_id, :user_id, :organization_id, :title, :description,
                    :status, :investigation_strategy, :current_turn,
                    :turns_without_progress, :created_at, :updated_at,
                    :closure_reason, :last_activity_at, :resolved_at, :closed_at,
                    :inquiry, :problem_verification, :working_conclusion,
                    :root_cause_conclusion, :path_selection,
                    :escalation_state, :documentation, :progress, :metadata,
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
    ) -> dict[str, Any]:
        """Build the parameter dict for the cases-row INSERT/UPDATE.

        Shared between the UPDATE and fallback INSERT paths in
        _upsert_case_record — keeps column serialization in one place.

        Post-redesign: ``description``, ``investigation_strategy``,
        ``current_turn``, ``turns_without_progress`` are first-class
        columns. The ``metadata`` JSON blob still holds the transient
        runtime state (proposed_actions / action_attempts / turn_history /
        pending_transition / message_count) — those have no first-class
        column yet.
        """
        return {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "organization_id": case.organization_id,
            "title": case.title,
            "description": case.description or "",
            "status": case.status.value,
            "investigation_strategy": case.investigation_strategy.value,
            "current_turn": case.current_turn,
            "turns_without_progress": case.turns_without_progress,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "closure_reason": getattr(case, "closure_reason", None),
            "last_activity_at": last_activity_at,
            "resolved_at": getattr(case, "resolved_at", None),
            "closed_at": getattr(case, "closed_at", None),
            "inquiry": json.dumps(to_json_compatible(case.inquiry.model_dump())),
            "problem_verification": (
                json.dumps(to_json_compatible(case.problem_verification.model_dump()))
                if case.problem_verification
                else None
            ),
            "working_conclusion": (
                json.dumps(to_json_compatible(case.working_conclusion.model_dump()))
                if case.working_conclusion
                else None
            ),
            "root_cause_conclusion": (
                json.dumps(to_json_compatible(case.root_cause_conclusion.model_dump()))
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
                    "message_count": case.message_count,
                    "pending_transition": case.pending_transition,
                    "proposed_actions": (
                        [
                            to_json_compatible(a.model_dump())
                            for a in case.proposed_actions
                        ]
                        if case.proposed_actions
                        else []
                    ),
                    "action_attempts": (
                        [
                            to_json_compatible(a.model_dump())
                            for a in case.action_attempts
                        ]
                        if case.action_attempts
                        else []
                    ),
                    "turn_history": (
                        [to_json_compatible(t.model_dump()) for t in case.turn_history]
                        if case.turn_history
                        else []
                    ),
                }
            ),
        }

    async def _upsert_evidence(
        self, case_id: str, evidence_list: builtins.list[Evidence], organization_id: str
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
                    :metadata, :created_at, :updated_at
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

            now = datetime.now(UTC)
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
                    # advances_milestones uses the same TagsArray storage
                    # shape as tags — comma-encoded TEXT on SQLite,
                    # TEXT[] on PG. The same _serialize_tags helper applies.
                    "advances_milestones": _serialize_tags(
                        list(evidence.advances_milestones)
                    ),
                    "is_primary": 1 if evidence.is_primary else 0,
                    "reliability_score": evidence.reliability_score,
                    "tags": _serialize_tags(evidence.tags),
                    "collected_at_turn": evidence.collected_at_turn,
                    "collected_by": evidence.collected_by,
                    "vectorized": 1 if evidence.vectorized else 0,
                    "coverage_start_ts": (
                        evidence.coverage_start_ts.isoformat()
                        if evidence.coverage_start_ts
                        else None
                    ),
                    "coverage_end_ts": (
                        evidence.coverage_end_ts.isoformat()
                        if evidence.coverage_end_ts
                        else None
                    ),
                    "metadata": json.dumps(evidence.metadata or {}),
                    "created_at": evidence.collected_at or now,
                    "updated_at": now,
                },
            )

    async def _upsert_hypotheses(
        self, case_id: str, hypotheses_dict: dict[str, Hypothesis], organization_id: str
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
                    hypothesis_id, case_id, organization_id, statement, status,
                    likelihood, initial_likelihood,
                    generated_at_turn, last_updated_turn, last_progress_at_turn,
                    iterations_without_progress,
                    category, generation_mode, rationale, retirement_reason,
                    refutation_reason,
                    tested_at, concluded_at, proposed_at, updated_at, metadata,
                    created_by, updated_by
                ) VALUES (
                    :hypothesis_id, :case_id, :organization_id, :statement, :status,
                    :likelihood, :initial_likelihood,
                    :generated_at_turn, :last_updated_turn, :last_progress_at_turn,
                    :iterations_without_progress,
                    :category, :generation_mode, :rationale, :retirement_reason,
                    :refutation_reason,
                    :tested_at, :concluded_at, :proposed_at, :updated_at, :metadata,
                    :created_by, :updated_by
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
                    "refutation_reason": hypothesis.refutation_reason,
                    "tested_at": hypothesis.tested_at,
                    "concluded_at": hypothesis.concluded_at,
                    "proposed_at": getattr(hypothesis, "proposed_at", None)
                    or datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
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
                    # Domain ``HypothesisEvidenceLink`` doesn't carry a
                    # turn number; the junction column is nullable.
                    "linked_at_turn": None,
                    # Linker user_id isn't tracked on the link object —
                    # nullable column, FK SET NULL on user delete.
                    "linked_by": None,
                    "created_at": link.analyzed_at,
                },
            )

    async def _upsert_solutions(
        self,
        case_id: str,
        solutions_list: builtins.list[Solution],
        organization_id: str,
    ) -> None:
        """Upsert solutions records (SQLite-compatible).

        Purely additive — see `_upsert_evidence` for rationale. There is
        no concrete delete-solution API on the case repo today; if a
        single-solution remove path is needed, add it explicitly.

        Post-009 schema: writes the full Solution audit trail
        (proposed_by, applied_at/by, verified_at, verification_method,
        verification_evidence_id, effectiveness). Status is included
        in ON CONFLICT UPDATE so the lifecycle can advance.
        """
        for solution in solutions_list:
            applied_at = solution.applied_at
            verified_at = solution.verified_at
            status = self._derive_solution_status(solution)

            query = text("""
                INSERT INTO solutions (
                    solution_id, case_id, organization_id, solution_type, title,
                    immediate_action, longterm_fix, implementation_steps, commands, risks,
                    description, status,
                    proposed_by, applied_by,
                    verification_method, verification_evidence_id, effectiveness,
                    verification_result, verified_at,
                    proposed_at, applied_at, updated_at, metadata
                ) VALUES (
                    :solution_id, :case_id, :organization_id, :solution_type, :title,
                    :immediate_action, :longterm_fix, :implementation_steps, :commands, :risks,
                    :description, :status,
                    :proposed_by, :applied_by,
                    :verification_method, :verification_evidence_id, :effectiveness,
                    :verification_result, :verified_at,
                    :proposed_at, :applied_at, :updated_at, :metadata
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
                    "status": status,
                    "proposed_by": solution.proposed_by,
                    "applied_by": solution.applied_by,
                    "verification_method": solution.verification_method,
                    "verification_evidence_id": solution.verification_evidence_id,
                    "effectiveness": solution.effectiveness,
                    "verification_result": None,
                    "verified_at": verified_at,
                    "proposed_at": solution.proposed_at,
                    "applied_at": applied_at,
                    "updated_at": datetime.now(UTC),
                    "metadata": json.dumps({}),
                },
            )

    @staticmethod
    def _derive_solution_status(solution: Solution) -> str:
        """Map Pydantic Solution lifecycle fields to the schema's
        status CHECK vocabulary ('proposed', 'accepted', 'rejected',
        'implemented', 'verified'). Single source of truth for the
        write path, used by both ``_upsert_solutions`` and the PG
        hybrid repo.

        verified_at present  -> 'verified' (the model_validator
                                guarantees effectiveness is also set)
        applied_at present   -> 'implemented'
        otherwise            -> 'proposed'
        """
        if solution.verified_at is not None:
            return "verified"
        if solution.applied_at is not None:
            return "implemented"
        return "proposed"

    async def _upsert_uploaded_files(
        self,
        case_id: str,
        files_list: builtins.list[UploadedFile],
        organization_id: str,
    ) -> None:
        """Upsert ``uploaded_files`` records.

        Purely additive — see ``_upsert_evidence`` for rationale. For
        intentional removal use ``delete_uploaded_file(case_id, file_id)``.
        Preprocessing artifacts (``summary``, ``structural_index``,
        ``data_type``, ``coverage_start_ts``, ``coverage_end_ts``) ride on
        this row; ``COALESCE`` on UPDATE prevents a failed re-extraction
        from clobbering a prior good extraction.
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
                    :metadata,
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
        self, case_id: str, messages_list: builtins.list[dict], organization_id: str
    ) -> None:
        """Upsert case messages (SQLite-compatible).

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
                    :message_id, :case_id, :organization_id, :turn_number, :role, :content, :created_at, :token_count, :metadata
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
                    "created_at": msg.get("created_at") or datetime.now(UTC),
                    "token_count": msg.get("token_count"),
                    "metadata": json.dumps(msg.get("metadata", {})),
                },
            )

    async def _append_case_actions(
        self, case_id: str, transitions: builtins.list[CaseAction], organization_id: str
    ) -> None:
        """Append case actions (SQLite-compatible)."""
        for transition in transitions:
            query = text("""
                INSERT INTO case_actions (
                    case_id, organization_id, from_status, to_status, reason,
                    triggered_by, transitioned_at, metadata
                ) VALUES (
                    :case_id, :organization_id, :from_status, :to_status, :reason,
                    :triggered_by, :transitioned_at, :metadata
                )
                ON CONFLICT DO NOTHING
            """)

            await self.db.execute(
                query,
                {
                    "case_id": case_id,
                    "organization_id": organization_id,
                    "from_status": (
                        transition.from_status.value if transition.from_status else None
                    ),
                    "to_status": transition.to_status.value,
                    "reason": (
                        transition.reason if hasattr(transition, "reason") else None
                    ),
                    "triggered_by": transition.triggered_by,
                    "transitioned_at": transition.triggered_at,
                    "metadata": json.dumps({}),
                },
            )

    async def _load_case_actions(self, case_id: str) -> builtins.list[CaseAction]:
        """Hydrate the audit trail for a case from ``case_actions``.

        Replaces the prior write-only pattern (``action_history=[]`` hardcoded
        in ``_to_domain``). Rows are returned ordered oldest-first to match
        the in-memory append order.
        """
        query = text("""
            SELECT from_status, to_status, reason, triggered_by, transitioned_at
            FROM case_actions
            WHERE case_id = :case_id
            ORDER BY transitioned_at ASC, transition_id ASC
        """)
        result = await self.db.execute(query, {"case_id": case_id})
        rows = result.fetchall()
        actions: builtins.list[CaseAction] = []
        for row in rows:
            actions.append(
                CaseAction(
                    from_status=(
                        CaseStatus(row.from_status) if row.from_status else None
                    ),
                    to_status=CaseStatus(row.to_status),
                    triggered_at=row.transitioned_at,
                    triggered_by=row.triggered_by,
                    reason=row.reason or "",
                )
            )
        return actions

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
        actions_data: builtins.list[CaseAction] | None = None,
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

        # Parse metadata for the transient runtime state that has no
        # first-class column yet (proposed_actions / action_attempts /
        # turn_history / pending_transition / message_count).
        metadata = json.loads(row.metadata) if row.metadata else {}

        # Promoted columns: read directly from the row.
        case_data = {
            "case_id": row.case_id,
            "user_id": row.user_id,
            "organization_id": row.organization_id,  # NOT NULL in DB
            "title": row.title,
            "status": CaseStatus(row.status),
            "action_history": actions_data or [],
            "closure_reason": row.closure_reason,
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
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            # Optimistic concurrency token. Must round-trip so the next
            # save() can assert it still matches the DB.
            "version": (
                int(row.version)
                if hasattr(row, "version") and row.version is not None
                else 1
            ),
        }

        # ``description`` is now a first-class column. Auto-heal the
        # legacy case where an INVESTIGATING row lost its description
        # (rare; pre-redesign rows that fell through the migration).
        description = row.description or ""
        if (
            CaseStatus(row.status) == CaseStatus.INVESTIGATING
            and (not description or not description.strip())
            and inquiry.proposed_problem_statement
        ):
            description = inquiry.proposed_problem_statement
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Auto-healed missing description for case {row.case_id} "
                    f"from proposed_problem_statement"
                )

        if description:
            case_data["description"] = description

        # ``investigation_strategy`` is now a first-class column.
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

        # ``organization_id`` is NOT NULL FK CASCADE on reports; derive
        # it from the parent case via subquery so callers don't have to
        # thread it through.
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
                :generation_status, :generation_time_ms, :metadata,
                :generated_at, :updated_at, :generated_by
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
                # Auto-generated terminal summaries have no human author,
                # so generated_by is NULL. Explicit user_id can be threaded
                # through later via an add_report() signature change when
                # API routes start carrying it.
                "generated_by": getattr(report, "generated_by", None),
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

    # ============================================================
    # Agent Execution & Tool Call Persistence (SQLite)
    # Schema reference: docs/architecture/data-and-storage/schemas/case-schema.md §4.11
    # ============================================================

    async def _resolve_organization_id(self, execution: Any, case_id: str) -> str:
        """Resolve organization_id for an execution.

        Production callers (agent_orchestration_service) populate
        execution.organization_id from the authenticated session. Tests and
        legacy callers may leave it None; in that case fall back to the
        parent case row.
        """
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
            # Mirror in-memory semantics: returned object has empty tool_calls;
            # callers persist tool calls separately via create_agent_tool_call.
            execution.tool_calls = []
            return execution
        except Exception as e:
            await self.db.rollback()
            raise RepositoryException(
                f"Failed to create agent execution {execution.execution_id}: {e}"
            ) from e

    async def get_agent_execution(self, execution_id: str) -> Any | None:

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
        status: str | None = None,
        agent_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
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
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[Any], int]:
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
        status: str | None,
        agent_type: str | None,
        order_by: str,
        limit: int,
        offset: int,
    ) -> tuple[builtins.list[Any], int]:

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

        executions: builtins.list[Any] = []
        for row in rows:
            tool_call_rows = await self._fetch_tool_call_rows(row.execution_id)
            executions.append(agent_mappers.row_to_execution(row, tool_call_rows))
        return executions, total

    async def update_agent_execution(self, execution: Any) -> Any:

        try:
            execution.updated_at = datetime.now(UTC)
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
        # SQLite does not enforce ON DELETE CASCADE unless the per-connection
        # PRAGMA foreign_keys=ON is set, and the engine setup in
        # infrastructure/persistence/database.py does not set it. We perform
        # an explicit two-phase delete to guarantee tool calls are removed.
        try:
            await self.db.execute(
                text("DELETE FROM agent_tool_calls WHERE execution_id = :execution_id"),
                {"execution_id": execution_id},
            )
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
                # Derive from parent execution row.
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
            tool_call.updated_at = datetime.now(UTC)
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

    async def get_agent_tool_calls_for_execution(
        self, execution_id: str
    ) -> builtins.list[Any]:

        rows = await self._fetch_tool_call_rows(execution_id)
        return [agent_mappers.row_to_tool_call(row) for row in rows]

    async def _fetch_tool_call_rows(self, execution_id: str) -> builtins.list[Any]:
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
        self, case_id: str, agent_type: str | None = None
    ) -> Any | None:

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


class RepositoryException(Exception):
    """Exception raised for repository errors."""

    pass
