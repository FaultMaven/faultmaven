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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.contracts import (
    Case,
    CaseEntity,
    CaseReport,
    CaseState,
    EntityType,
    Evidence,
    ReportType,
    TurnReceipt,
    TurnReceiptExistsError,
    UploadedFile,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.modules.case.infrastructure.case_scope import case_scope_where
from faultmaven.modules.case.infrastructure.created_bounds import created_bounds_where
from faultmaven.modules.case.infrastructure.sqlite_case_repository.loading import (
    _load_case_actions,
    _load_causal_graph_for_case,
    _load_evidence_for_case,
    _load_evidence_for_cases_bulk,
    _load_evidence_needs_for_case,
    _load_hypotheses,
    _load_hypotheses_bulk,
    _load_messages,
    _load_messages_bulk,
    _load_solutions,
    _load_solutions_bulk,
    _load_uploaded_files,
    _load_uploaded_files_bulk,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.rows import (
    _row_to_case,
    _row_to_evidence,
    _row_to_report,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.saving import (
    _append_case_actions,
    _insert_report,
    _insert_turn_receipt,
    _reconcile_causal_graph,
    _upsert_case_record,
    _upsert_causal_edges,
    _upsert_causal_nodes,
    _upsert_evidence,
    _upsert_evidence_needs,
    _upsert_hypotheses,
    _upsert_messages,
    _upsert_solutions,
    _upsert_uploaded_files,
)
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

    async def save(
        self,
        case: Case,
        *,
        reports: Sequence[CaseReport] = (),
        receipt: Optional[TurnReceipt] = None,
    ) -> Case:
        """Save case using hybrid schema with transactions.

        Optimistic concurrency control is enforced inside
        `_upsert_case_record`: the in-memory `case.version` is checked
        against the DB row, and `StaleCaseException` is raised on
        mismatch. On success the same `case` instance is mutated with
        the new version and returned — callers can use either the
        return value or the passed-in object.

        ``reports`` and ``receipt`` are written in the same transaction, after
        the case rows and before the commit, so they commit with the case or
        not at all (#1882, #1888).
        """
        self.check_turn_rows(case, reports, receipt)
        # Restored if the save does not commit: see SAVE_STAMPED_FIELDS.
        stamps = self.save_stamps(case)
        # Self-heal any turn-sequence anomaly into consecutive history (with
        # SKIPPED placeholders) BEFORE persisting, so a transient gap can never
        # wedge the case. No-op on healthy cases.
        case.reconcile_turn_sequence()

        # Defense in depth: catch swallowed validation exceptions or bypassed validators
        # by re-verifying the entire aggregate state before persisting.
        Case.model_validate(case.model_dump(mode="python"))

        try:
            case.updated_at = datetime.now(UTC)

            # P3 chokepoint: refresh denormalized disposition_eligibility
            # from current case content. Single site so the column stays
            # in sync with state/root_cause/solutions/progress without
            # per-mutation-site write burden.
            from faultmaven.core.investigation.terminal_transitions import (
                derive_disposition_eligibility,
            )

            case.disposition_eligibility = derive_disposition_eligibility(case)

            # Two columns, two meanings (ADR-017 D1/D2): the enterprise
            # isolates and every child row inherits it from its case; the
            # organization is nullable billing attribution beside it.
            enterprise_id = case.enterprise_id
            organization_id = case.organization_id
            await _upsert_case_record(self.db, case)
            # evidence.source_file_id is a real FK to uploaded_files.file_id,
            # so files must exist before any evidence row that references them.
            await _upsert_uploaded_files(
                self.db,
                case.case_id,
                case.uploaded_files,
                enterprise_id,
                organization_id,
            )
            await _upsert_evidence(
                self.db, case.case_id, case.evidence, enterprise_id, organization_id
            )
            # Needs and the fulfillment junction must run AFTER evidence
            # so the junction FK to evidence.evidence_id is satisfied.
            await _upsert_evidence_needs(
                self.db,
                case.case_id,
                case.evidence_needs,
                enterprise_id,
                organization_id,
                case.current_turn,
            )
            # Causal graph before hypotheses/solutions: hypotheses.root_node_id
            # and solutions.node_id FK causal_nodes; causal_node_evidence FKs
            # evidence (already upserted). Nodes before edges (edges FK nodes).
            await _upsert_causal_nodes(
                self.db, case.case_id, case.causal_nodes, enterprise_id, organization_id
            )
            await _upsert_causal_edges(
                self.db, case.case_id, case.causal_edges, enterprise_id, organization_id
            )
            await _reconcile_causal_graph(
                self.db,
                case.case_id,
                set(case.causal_nodes.keys()),
                {e.edge_id for e in case.causal_edges},
            )
            await _upsert_hypotheses(
                self.db, case.case_id, case.hypotheses, enterprise_id, organization_id
            )
            await _upsert_solutions(
                self.db, case.case_id, case.solutions, enterprise_id, organization_id
            )
            await _upsert_messages(
                self.db,
                self.normalise_message_row,
                case.case_id,
                case.messages,
                enterprise_id,
                organization_id,
            )

            if case.action_history:
                await _append_case_actions(
                    self.db,
                    case.case_id,
                    case.action_history,
                    enterprise_id,
                    organization_id,
                )

            # After the case row (their enterprise is read from it) and before
            # the commit (they commit with it or not at all).
            for report in reports:
                await _insert_report(self.db, report)
            if receipt is not None:
                try:
                    await _insert_turn_receipt(self.db, case, receipt)
                except IntegrityError as refused:
                    await self._raise_if_receipt_exists(case, receipt, refused)
                    raise

            await self.db.commit()
            return case

        except (StaleCaseException, TurnReceiptExistsError):
            # Propagate unwrapped so callers can retry or surface 409
            # without unwrapping a generic RepositoryException.
            await self.db.rollback()
            self.restore_save_stamps(case, stamps)
            raise
        except Exception as e:
            await self.db.rollback()
            self.restore_save_stamps(case, stamps)
            raise RepositoryException(f"Failed to save case {case.case_id}: {e}") from e

    async def _raise_if_receipt_exists(
        self, case: Case, receipt: TurnReceipt, refused: IntegrityError
    ) -> None:
        """Raise ``TurnReceiptExistsError`` when the receipt INSERT was refused
        because the key already has a receipt (#1888).

        Decided by re-reading, not by the driver's message: an
        ``IntegrityError`` names its constraint differently per dialect (see
        ``team_repository``). The transaction is rolled back first (a refused
        statement aborts it on PostgreSQL); the read runs in a fresh one, which
        the session's ``begin`` listener binds to the same tenant. A key with
        no receipt means the refusal was some other constraint, and the
        caller re-raises it unchanged.
        """
        await self.db.rollback()
        existing = await self.get_turn_receipt(
            enterprise_id=case.enterprise_id,
            case_id=case.case_id,
            author_id=receipt.author_id,
            idempotency_key=receipt.idempotency_key,
        )
        if existing is not None:
            raise TurnReceiptExistsError(
                case.case_id, receipt.idempotency_key
            ) from refused

    async def get_turn_receipt(
        self,
        *,
        enterprise_id: str,
        case_id: str,
        author_id: str,
        idempotency_key: str,
    ) -> TurnReceipt | None:
        """The receipt a committed keyed turn left, or ``None`` (#1888).

        By the table's full primary key, enterprise first, so the read is one
        index probe.
        """
        try:
            row = (
                await self.db.execute(
                    text("""
                        SELECT case_id, author_id, idempotency_key,
                               request_fingerprint, turn_number, response,
                               created_at
                        FROM turn_receipts
                        WHERE enterprise_id = :enterprise_id
                          AND case_id = :case_id
                          AND author_id = :author_id
                          AND idempotency_key = :idempotency_key
                    """),
                    {
                        "enterprise_id": enterprise_id,
                        "case_id": case_id,
                        "author_id": author_id,
                        "idempotency_key": idempotency_key,
                    },
                )
            ).fetchone()
        except Exception as e:
            raise RepositoryException(
                f"Failed to read the turn receipt for case {case_id}: {e}"
            ) from e
        if row is None:
            return None
        return TurnReceipt(
            case_id=row[0],
            author_id=row[1],
            idempotency_key=row[2],
            request_fingerprint=row[3],
            turn_number=row[4],
            response=json.loads(row[5]),
            created_at=(
                row[6]
                if isinstance(row[6], datetime)
                else datetime.fromisoformat(row[6])
            ),
        )

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
            hypotheses_data = await _load_hypotheses(self.db, case_id)
            solutions_data = await _load_solutions(self.db, case_id)
            uploaded_files_data = await _load_uploaded_files(self.db, case_id)
            messages_data = await _load_messages(self.db, case_id)
            actions_data = await _load_case_actions(self.db, case_id)

            # Reconstruct Case domain object
            case = _row_to_case(
                row,
                hypotheses_data,
                solutions_data,
                uploaded_files_data,
                messages_data,
                actions_data,
            )

            # Load evidence directly
            if case:
                await _load_evidence_for_case(self.db, case)
                await _load_evidence_needs_for_case(self.db, case)
                await _load_causal_graph_for_case(self.db, case)
                # Self-heal any persisted turn-sequence anomaly (e.g. a case
                # wedged before this fix) on load, before the engine uses it.
                case.reconcile_turn_sequence()

            return case

        except Exception as e:
            raise RepositoryException(f"Failed to get case {case_id}: {e}") from e

    # ========================================================================
    # Bulk loaders — used by list() to avoid the N+1 per-case fan-out that
    # would otherwise run (N * 5) queries for a page of N cases. Each loader
    # issues one SELECT with WHERE case_id IN (:ids) and groups results by
    # case_id in Python. Shapes mirror the per-case _load_* helpers above so
    # _row_to_case can consume either.
    # ========================================================================

    async def list(
        self,
        user_id: str | None = None,
        enterprise_id: str | None = None,
        state: CaseState | None = None,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        shared_case_ids: list[str] | None = None,
        restrict_case_ids: list[str] | None = None,
        include_empty: bool = True,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
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

            # owned ∪ shared-to-my-teams (ADR-013 §D4). None when user_id is
            # falsy (cross-tenant admin path); owner-only when no shares.
            # restrict_case_ids narrows to one team's shares (filter-by-team).
            scope_clause = case_scope_where(
                params,
                user_id,
                shared_case_ids,
                restrict_case_ids=restrict_case_ids,
            )
            if scope_clause:
                where_clauses.append(scope_clause)

            # No per-query org filter: standalone is single-tenant (one implicit
            # org), and multi-tenant isolation is enforced by in-core PostgreSQL
            # RLS (ADR-010, migration 018), not by per-query repository filters.
            # The enterprise_id param is retained for interface symmetry with
            # the write-path signatures; it does not scope reads.

            if state:
                where_clauses.append("state = :state")
                params["state"] = state.value

            if source:
                where_clauses.append("source = :source")
                params["source"] = source

            # Exclude empty cases (no conversation yet) when requested. Applied
            # in SQL — not as a Python post-filter — so the same predicate
            # constrains both the COUNT and the paginated SELECT, keeping the
            # page/total contract sound.
            if not include_empty:
                where_clauses.append("current_turn > 0")

            # Creation-date window `[created_after, created_before)`. The
            # normalization to UTC is NOT cosmetic here: this column is stored
            # as adapter-rendered TEXT, so the comparison is lexicographic and
            # blind to the offset suffix — see created_bounds.py.
            where_clauses.extend(
                created_bounds_where(params, created_after, created_before)
            )

            where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            # Count query.
            count_query = text(f"SELECT COUNT(*) FROM cases {where_sql}")
            count_result = await self.db.execute(count_query, params)
            total_count = count_result.scalar()

            # Page query — fetch full case rows (we need every column for
            # _row_to_case; a later optimization could project only the
            # fields the caller declares it needs).
            # case_id breaks updated_at ties, so a page boundary between rows
            # updated at the same instant falls in the same place on every read.
            list_query = text(f"""
                SELECT *
                FROM cases
                {where_sql}
                ORDER BY updated_at DESC, case_id
                LIMIT :limit OFFSET :offset
            """)
            rows = (await self.db.execute(list_query, params)).fetchall()

            if not rows:
                return [], total_count

            case_ids = [row.case_id for row in rows]

            # Batch-load every owned sub-collection with one SELECT each.
            hypotheses_by_case = await _load_hypotheses_bulk(self.db, case_ids)
            solutions_by_case = await _load_solutions_bulk(self.db, case_ids)
            uploaded_files_by_case = await _load_uploaded_files_bulk(self.db, case_ids)
            messages_by_case = await _load_messages_bulk(self.db, case_ids)

            cases: list[Case] = []
            for row in rows:
                try:
                    case = _row_to_case(
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
            await _load_evidence_for_cases_bulk(self.db, cases)

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

    async def list_all_case_ids(self) -> List[str]:
        """Every case row's id, regardless of state (see CaseRepository)."""
        try:
            result = await self.db.execute(text("SELECT case_id FROM cases"))
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            raise RepositoryException(f"Failed to list case ids: {e}") from e

    async def list_all_storage_refs(self) -> Set[str]:
        """Every non-null uploaded_files.storage_ref (see CaseRepository).

        One scan into a set, not N point lookups: ``storage_ref`` carries no
        index (unlike ``content_hash``), so per-candidate lookups would each
        be a full scan. DISTINCT because the set is what the caller wants and
        duplicates only cost transfer.

        Two things a future optimiser must not "fix":

        **Never add a LIMIT.** A truncated reference set is indistinguishable
        from a smaller one, and every row it drops becomes a file the sweep
        believes is unreferenced — so capping this query converts a memory
        concern into silent data loss. If the set ever outgrows the job pod,
        the answer is to stream it into the membership test, not to shorten
        it. (The sweep's disjoint guard catches a set that misses EVERY
        candidate; it cannot catch one that misses some.)

        **Do not filter to case-bound rows.** ``case_id`` is nullable — KB
        conversion uploads carry none — and those rows still reference stored
        objects. Excluding them would shrink the protected set, which is a
        deletion, not a tidy-up. It does mean the operator-facing count
        includes rows no case owns; that is the honest number for "rows that
        reference storage".
        """
        try:
            result = await self.db.execute(
                text(
                    "SELECT DISTINCT storage_ref FROM uploaded_files "
                    "WHERE storage_ref IS NOT NULL"
                )
            )
            return {row[0] for row in result.fetchall() if row[0]}
        except Exception as e:
            raise RepositoryException(f"Failed to list storage refs: {e}") from e

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
                    advances_milestones, collected_by,
                    coverage_source
                FROM evidence
                WHERE {' AND '.join(where_clauses)}
                ORDER BY coverage_start_ts ASC
                LIMIT 1000
                """)
            result = await self.db.execute(query, params)
            rows = result.fetchall()

            evidence_list: List[Evidence] = []
            for row in rows:
                ev = _row_to_evidence(row)
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

        ``enterprise_id`` is NOT NULL on case_entities (``organization_id``
        beside it is nullable billing attribution); we derive both
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
                    case_id, enterprise_id, organization_id, entity_type, entity_value, evidence_id,
                    mention_count, in_error_context, first_seen_ts
                ) VALUES (
                    :case_id,
                    (SELECT enterprise_id FROM cases WHERE case_id = :case_id),
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
        enterprise_id: str | None = None,
        state: CaseState | None = None,
        limit: int = 20,
        shared_case_ids: builtins.list[str] | None = None,
        restrict_case_ids: builtins.list[str] | None = None,
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

            # owned ∪ shared-to-my-teams (ADR-013 §D4); owner-only when no shares.
            # restrict_case_ids narrows to one team's shares (filter-by-team).
            scope_clause = case_scope_where(
                params,
                user_id,
                shared_case_ids,
                restrict_case_ids=restrict_case_ids,
            )
            if scope_clause:
                where_clauses.append(scope_clause)

            # No per-query org filter: standalone is single-tenant (one implicit
            # org), and multi-tenant isolation is enforced by in-core PostgreSQL
            # RLS (ADR-010, migration 018), not by per-query repository filters.
            # The enterprise_id param is retained for interface symmetry with
            # the write-path signatures; it does not scope reads.

            # Lifecycle state — spelled exactly as `list` spells it, in the
            # same WHERE clause as the text predicate and therefore ahead of
            # the LIMIT below.
            if state:
                where_clauses.append("state = :state")
                params["state"] = state.value

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # The TRUE match count, from the same WHERE clause and BEFORE the
            # LIMIT — as ``list`` above computes it, and for the reason the
            # interface has always stated: this method's contract is
            # ``(cases, total_count)``, and returning ``len(cases)`` made the
            # second value the page length instead. Nothing consumes it today
            # (``search_cases`` discards it and the route is
            # ``response_model=List[CaseSummary]``), which is exactly why it
            # could stay wrong unnoticed — a declared value that is not the
            # value declared, one return slot over from the field #1416 is
            # about. Same safe-direction divergence ``list`` documents: this is
            # a raw COUNT(*), so a row that fails to hydrate below makes it
            # over-report rather than hide a result.
            count_query = text(f"SELECT COUNT(*) FROM cases {where_sql}")
            total_count = (await self.db.execute(count_query, params)).scalar() or 0

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

            return cases, total_count

        except Exception as e:
            raise RepositoryException(f"Failed to search cases: {e}") from e

    # ========================================================================
    # Message Operations
    # ========================================================================

    async def add_message(self, case_id: str, message_dict: dict) -> bool:
        """Add message to case_messages table.

        Returns False (not raise) if the parent case doesn't exist —
        enterprise_id is NOT NULL on case_messages (organization_id beside it is
        nullable billing attribution) and both are derived
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
            # NOT NULL on case_messages.enterprise_id.
            probe = await self.db.execute(
                text("SELECT 1 FROM cases WHERE case_id = :case_id"),
                {"case_id": case_id},
            )
            if probe.fetchone() is None:
                return False

            # Shared with the aggregate save's ``_upsert_messages`` so the two
            # writers of this table cannot disagree about an incomplete row
            # (#1418). Note this also fixes a defect of its own here: the old
            # default was a ``datetime``, whose ``str()`` uses a SPACE
            # separator, and the column is compared as TEXT — a row stamped
            # that way sorted before every ISO-8601 row and jumped to the front
            # of the transcript.
            # A COPY, unlike the aggregate save's in-place call. This method
            # does a plain INSERT with no ON CONFLICT, so nothing needs to be
            # written back — and stamping the caller's dict would make a
            # REUSED template dict carry the first call's id into the second,
            # where it hits the primary key. ``add_message`` has always been
            # non-mutating; it stays that way.
            row = self.normalise_message_row(dict(message_dict), stamp_created_at=True)
            message_id = row["message_id"]
            created_at = row["created_at"]

            # SQLite-compatible: no ::jsonb type cast
            # Both tenancy columns derived from the parent case (already
            # verified to exist by the probe above), so a message can never land
            # in a different enterprise from the case it belongs to.
            query = text("""
                INSERT INTO case_messages (message_id, case_id, enterprise_id, organization_id, turn_number, role, content, author_id, created_at, token_count, metadata)
                VALUES (:message_id, :case_id,
                        (SELECT enterprise_id FROM cases WHERE case_id = :case_id),
                        (SELECT organization_id FROM cases WHERE case_id = :case_id),
                        :turn_number, :role, :content, :author_id, :created_at, :token_count, :metadata)
            """)

            await self.db.execute(
                query,
                {
                    "message_id": message_id,
                    "case_id": case_id,
                    "turn_number": row["turn_number"],
                    "role": message_dict.get("role", "user"),
                    "content": message_dict.get("content", ""),
                    "author_id": message_dict.get("author_id"),
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
    ) -> builtins.list[dict]:
        """Get messages for case with pagination.

        Schema per design spec (case-schema.md §4.7):
        - message_id, turn_number, role, content, created_at, token_count,
          metadata, author_id

        ``author_id`` is selected last so the pre-existing positional indices
        below keep their meaning.
        """
        try:
            query = text("""
                SELECT message_id, turn_number, role, content, created_at, token_count, metadata,
                       author_id
                FROM case_messages
                WHERE case_id = :case_id
                ORDER BY created_at ASC, turn_number ASC
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
                        "author_id": row[7],
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

    async def update_metadata_fields(
        self,
        case_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Scoped UPDATE of cosmetic metadata fields — does NOT bump version.

        title/description are user-facing labels, not investigation state.
        Writing them through ``save(case)`` would bump ``cases.version``
        and stale-conflict any in-flight turn save (which can hold the
        case in memory for tens of seconds during the LLM tool loop).

        Pass only the fields you want to change; ``None`` means "leave
        as-is". Returns True if a row was updated.
        """
        if title is None and description is None:
            return False

        sets: list[str] = ["updated_at = :updated_at"]
        params: dict[str, Any] = {
            "case_id": case_id,
            "updated_at": datetime.now(UTC),
        }
        if title is not None:
            sets.append("title = :title")
            params["title"] = title
        if description is not None:
            sets.append("description = :description")
            params["description"] = description

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

        The aggregate save does NOT remove these rows (purely additive
        upserts), so targeted removal must be explicit. This is that path;
        deleting the whole case also removes them, via ON DELETE CASCADE.
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

        The aggregate save does NOT remove these rows (purely additive
        upserts), so targeted removal must be explicit. This is that path;
        deleting the whole case also removes them, via ON DELETE CASCADE.
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
                    (SELECT COUNT(*) FROM hypotheses WHERE case_id = :case_id AND state = 'validated') as validated_hypotheses,
                    (SELECT COUNT(*) FROM solutions WHERE case_id = :case_id) as solution_count,
                    (SELECT COUNT(*) FROM solutions WHERE case_id = :case_id AND state = 'implemented') as implemented_solutions,
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
                files = await _load_uploaded_files(self.db, case_id)
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
                    WHERE state = 'closed'
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

    # ========================================================================
    # Report Operations (SQLite-compatible)
    # ========================================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report to reports table in its own transaction (SQLite-compatible)."""
        await _insert_report(self.db, report)
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

        return _row_to_report(row)

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

        return [_row_to_report(row) for row in rows]

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

    # ============================================================
    # Agent Execution & Tool Call Persistence (SQLite)
    # Schema reference: docs/architecture/data-and-storage/schemas/case-schema.md §4.11
    # ============================================================


class RepositoryException(Exception):
    """Exception raised for repository errors."""

    pass
