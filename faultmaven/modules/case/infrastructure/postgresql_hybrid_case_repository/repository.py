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
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.evidence import (
    CaseEntity,
    EntityType,
    Evidence,
    UploadedFile,
)
from faultmaven.modules.case.domain.models.lifecycle import (
    CaseState,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint

# Case-owned models (per module-organization-design.md)
from faultmaven.modules.case.domain.owned_models.report import CaseReport, ReportType
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.modules.case.infrastructure.case_scope import case_scope_where
from faultmaven.modules.case.infrastructure.created_bounds import created_bounds_where
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.loading import (
    _load_causal_graph_for_case,
    _load_evidence_for_case,
    _load_evidence_needs_for_case,
    _load_hypothesis_evidence_links,
    _row_to_case,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.rows import (
    _as_datetime,
    _cast,
    _row_to_case_checkpoint,
    _row_to_evidence,
    _row_to_report,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.saving import (
    _append_case_actions,
    _insert_checkpoint,
    _insert_report,
    _org_lookup_case_id,
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

# TYPE_CHECKING imports not needed - models imported directly above

logger = logging.getLogger(__name__)


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
        self._is_pg = self._detect_postgresql(db_session)

    @staticmethod
    def _detect_postgresql(session: AsyncSession) -> bool:
        """Resolve whether ``session`` is bound to PostgreSQL.

        Tries ``session.bind`` first, then ``session.get_bind()`` — the SAME
        order the repository-selection factory uses
        (``sessionless_case_repository.get_repository_for_dialect``). The
        fallback matters: a session whose ``.bind`` is None but whose
        ``get_bind()`` resolves to PostgreSQL is routed to THIS repository by
        the factory, so the cast logic must agree with that decision or it
        would emit SQLite-style bare ``:name`` on a live PG connection.
        """
        try:
            bind = session.bind
            if bind is None and hasattr(session, "get_bind"):
                bind = session.get_bind()
            return bind is not None and bind.dialect.name == "postgresql"
        except Exception:
            # Mirror the factory's safe default: unknown bind -> not PG.
            return False

    def _org_lookup_case_id(self) -> str:
        """``:case_id`` cast to VARCHAR, for the tenancy-derivation subqueries
        (see ``saving._org_lookup_case_id`` for why the cast is load-bearing)."""
        return _org_lookup_case_id(self._is_pg)

    # ========================================================================
    # Core CRUD Operations
    # ========================================================================

    async def save(
        self,
        case: Case,
        *,
        reports: Sequence[CaseReport] = (),
        checkpoints: Sequence[CaseCheckpoint] = (),
    ) -> Case:
        """
        Save case using hybrid schema with transactions.

        Strategy:
        1. Upsert cases table (main record + JSONB)
        2. Upsert normalized tables (evidence, hypotheses, solutions)
        3. Append-only tables (messages, case_actions)
        4. Insert the turn's reports and checkpoints (#1882)

        One transaction: the RLS tenant is bound once, at its BEGIN, by the
        engine's ``begin`` listener, so step 4's rows are written under the same
        enterprise as the case and commit with it or not at all.

        Args:
            case: Case domain object
            reports: Report rows to commit with the case
            checkpoints: Checkpoint rows to commit with the case

        Returns:
            Saved case with updated timestamps

        Raises:
            StaleCaseException: The case changed since it was read
            RepositoryException: If save fails
        """
        self.check_turn_rows(case, reports, checkpoints)
        try:
            # Self-heal any turn-sequence anomaly into consecutive history
            # (with SKIPPED placeholders) before persisting, so a transient gap
            # can never wedge the case. No-op on healthy cases.
            case.reconcile_turn_sequence()

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

            # Two columns, two meanings (ADR-017 D1/D2). ``enterprise_id`` is
            # the isolation key every child row inherits from its case;
            # ``organization_id`` is the billing attribution beside it, and is
            # ``None`` whenever nobody pays for the account that owns the case.
            enterprise_id = case.enterprise_id
            organization_id = case.organization_id

            await _upsert_case_record(self._is_pg, self.db, case)
            # Post-010: evidence.source_file_id is a real FK to
            # uploaded_files.file_id, so files must exist before any
            # evidence row that references them gets inserted.
            await _upsert_uploaded_files(
                self._is_pg,
                self.db,
                case.case_id,
                case.uploaded_files,
                enterprise_id,
                organization_id,
            )
            await _upsert_evidence(
                self._is_pg,
                self.db,
                case.case_id,
                case.evidence,
                enterprise_id,
                organization_id,
            )
            # Needs and the fulfillment junction must run AFTER evidence
            # so the junction FK to evidence.evidence_id is satisfied.
            await _upsert_evidence_needs(
                self._is_pg,
                self.db,
                case.case_id,
                case.evidence_needs,
                enterprise_id,
                organization_id,
                case.current_turn,
            )
            # Causal graph before hypotheses/solutions: hypotheses.root_node_id
            # and solutions.node_id FK causal_nodes; causal_node_evidence FKs
            # evidence (already upserted above). Nodes before edges (edges FK
            # nodes).
            await _upsert_causal_nodes(
                self._is_pg,
                self.db,
                case.case_id,
                case.causal_nodes,
                enterprise_id,
                organization_id,
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
                self._is_pg,
                self.db,
                case.case_id,
                case.hypotheses,
                enterprise_id,
                organization_id,
            )
            await _upsert_solutions(
                self._is_pg,
                self.db,
                case.case_id,
                case.solutions,
                enterprise_id,
                organization_id,
            )
            await _upsert_messages(
                self._is_pg,
                self.db,
                self.normalise_message_row,
                case.case_id,
                case.messages,
                enterprise_id,
                organization_id,
            )
            if case.action_history:
                await _append_case_actions(
                    self._is_pg,
                    self.db,
                    case.case_id,
                    case.action_history,
                    enterprise_id,
                    organization_id,
                )

            # After the case row (their enterprise is read from it) and before
            # the commit (they commit with it or not at all).
            for report in reports:
                await _insert_report(self._is_pg, self.db, report)
            for checkpoint in checkpoints:
                await _insert_checkpoint(self._is_pg, self.db, checkpoint)

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
                            'root_node_id', h.root_node_id,
                            'path', h.path,
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
                            'node_id', s.node_id,
                            'quadrant', s.quadrant,
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
                            'coverage_end_ts', f.coverage_end_ts,
                            'coverage_source', f.coverage_source
                        )) FILTER (WHERE f.file_id IS NOT NULL),
                        '[]'::json
                    ) as uploaded_files_data,

                    -- Case messages via a correlated subquery with an explicit
                    -- ORDER BY. Every reader of case_messages orders by
                    -- (created_at, turn_number) and defers ties to insertion
                    -- order; `tests/unit/architecture/
                    -- test_message_read_order_is_uniform.py` enforces that
                    -- across all five, which is the relation a comment cannot
                    -- hold on its own (#1428). Do NOT add a message_id
                    -- tiebreaker: it is a uuid4, so it orders ties at random
                    -- and inverts same-turn exchanges.
                    --
                    -- This comment used to claim it "matches the SQLite path's
                    -- ORDER BY created_at". It did not -- it carried a
                    -- turn_number tiebreaker the others lacked -- so the
                    -- divergence was documented as parity.
                    -- Do NOT fold this back into a `json_agg(DISTINCT ...)` over a
                    -- joined `case_messages`: DISTINCT makes PostgreSQL sort the
                    -- aggregated jsonb objects shortest-key-first — i.e. by `role`
                    -- (the 4-char key) — which returns all 'assistant' messages
                    -- before all 'user' messages and made the transcript render
                    -- role-grouped with the leading block mislabeled "Turn 0".
                    -- The subquery also avoids multiplying this table into the
                    -- other 1:many joins (smaller cartesian product).
                    COALESCE((
                        SELECT json_agg(jsonb_build_object(
                            'message_id', m.message_id,
                            'turn_number', m.turn_number,
                            'role', m.role,
                            'content', m.content,
                            'created_at', m.created_at,
                            'token_count', m.token_count,
                            'metadata', m.metadata,
                            'author_id', m.author_id
                        ) ORDER BY m.created_at ASC, m.turn_number ASC)
                        FROM case_messages m
                        WHERE m.case_id = c.case_id
                    ), '[]'::json) as messages_data

                FROM cases c
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

            # Hydrate junction-table links for every hypothesis on the row.
            hypotheses_payload = (
                row.hypotheses_data
                if isinstance(row.hypotheses_data, list)
                else json.loads(row.hypotheses_data)
            )
            hypothesis_ids = [
                h["hypothesis_id"] for h in hypotheses_payload if h.get("hypothesis_id")
            ]
            links_by_hyp = await _load_hypothesis_evidence_links(
                self.db, hypothesis_ids
            )

            case = await _row_to_case(self.db, row, links_by_hyp)

            # Load evidence separately — the Pydantic reconstruction needs
            # column-by-column conversion that doesn't fit cleanly in a
            # JSONB aggregate.
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

    async def list(
        self,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
        include_empty: bool = True,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
    ) -> tuple[List[Case], int]:
        """
        List cases with optional filters and pagination.

        Performance: ~20ms for 50 cases (indexed queries)

        Args:
            user_id: Filter by user
            enterprise_id: Retained for interface symmetry; does NOT scope
                reads (single-tenant standalone; multi-tenant isolation is
                PostgreSQL RLS keyed on the enterprise, ADR-010/ADR-017)
            state: Filter by state
            limit: Maximum results
            offset: Pagination offset
            source: Filter by originating surface (``copilot``/``slack``/
                ``api``). A FALSY value means no filter — see
                ``ICaseRepository.list``.
            shared_case_ids: Case ids readable via a team share (ADR-013 §D4);
                widens owner-only scope to ``owned ∪ shared-to-my-teams``.
            restrict_case_ids: Filter-by-team facet — narrows the result to one
                team's shared case ids (the caller resolves/authorizes the team).
            created_after: INCLUSIVE lower bound on ``created_at``
            created_before: EXCLUSIVE upper bound — the window is
                ``[created_after, created_before)``

        Returns:
            Tuple of (cases, total_count)
        """
        try:
            # Build WHERE clause dynamically
            where_clauses = []
            params = {"limit": limit, "offset": offset}

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

            # No per-query tenant filter: multi-tenant isolation is enforced by
            # in-core PostgreSQL RLS keyed on ``enterprise_id`` (ADR-010,
            # ADR-017), not by per-query repository filters;
            # standalone-on-postgres is single-tenant. The enterprise_id param is
            # retained for interface symmetry but does not scope reads.

            if state:
                where_clauses.append("state = :state")
                params["state"] = state.value

            if source:
                where_clauses.append("source = :source")
                params["source"] = source

            # Exclude empty cases (no conversation yet) when requested. Applied
            # in SQL — not as a Python post-filter — so the same predicate
            # constrains both the COUNT and the paginated SELECT, keeping the
            # page/total contract sound (parity with the SQLite repository).
            if not include_empty:
                where_clauses.append("current_turn > 0")

            # Creation-date window `[created_after, created_before)`. The helper
            # normalizes to UTC HERE rather than trusting a caller two layers up
            # to have done it: ``created_at`` is ``timestamptz`` and asyncpg
            # raises on a naive bound, which CaseService.list_user_cases would
            # then swallow into an empty list.
            where_clauses.extend(
                created_bounds_where(params, created_after, created_before)
            )

            where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            # Count query
            count_query = text(f"SELECT COUNT(*) FROM cases {where_sql}")
            count_result = await self.db.execute(count_query, params)
            total_count = count_result.scalar()

            # List query (simplified - just get case IDs, then fetch full cases).
            # case_id breaks updated_at ties (a bulk statement stamps one
            # transaction time on many rows), so a page boundary falls in the
            # same place on every read — and in the same place as the
            # cross-enterprise operator list, which orders the same way.
            list_query = text(f"""
                SELECT case_id
                FROM cases
                {where_sql}
                ORDER BY updated_at DESC, case_id
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

    async def list_all_case_ids(self) -> List[str]:
        """Every case row's id, regardless of state (see CaseRepository).

        Under the multi-tenant provider RLS scopes this to the org bound in
        the tenant context — a complete cross-tenant set requires the
        maintenance DB role (BYPASSRLS), which the jobs runner enforces for
        cross_tenant jobs.
        """
        try:
            result = await self.db.execute(text("SELECT case_id FROM cases"))
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            raise RepositoryException(f"Failed to list case ids: {e}") from e

    async def list_all_storage_refs(self) -> Set[str]:
        """Every non-null uploaded_files.storage_ref (see CaseRepository).

        ``uploaded_files`` is RLS-tenanted and FAIL-CLOSED (migration 018): a
        session with no org bound sees ZERO rows. For the delete-deciding
        orphan sweep that is the worst possible failure — every live object
        would read as unreferenced — so the sweep runs on the audited
        maintenance path (BYPASSRLS role), and refuses to delete when the
        answer overlaps none of its candidates.

        The SQL is deliberately character-identical to the SQLite
        implementation — ``test_storage_ref_sql_is_identical_across_dialects``
        pins that, because a divergence here is a divergence in which files
        the cloud path protects.

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

        ``enterprise_id`` is NOT NULL on case_entities and ``organization_id``
        beside it is nullable billing attribution; both are derived from the
        parent case row in the INSERT so callers don't have to thread them
        through. Deriving rather than accepting them is also what makes a child
        row incapable of landing in a different enterprise from its case.
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

            insert_q = text(f"""
                INSERT INTO case_entities (
                    case_id, enterprise_id, organization_id, entity_type, entity_value, evidence_id,
                    mention_count, in_error_context, first_seen_ts
                ) VALUES (
                    :case_id,
                    (SELECT enterprise_id FROM cases
                     WHERE case_id = {self._org_lookup_case_id()}),
                    (SELECT organization_id FROM cases
                     WHERE case_id = {self._org_lookup_case_id()}),
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

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        limit: int = 20,
        shared_case_ids: Optional[List[str]] = None,
        restrict_case_ids: Optional[List[str]] = None,
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
            enterprise_id: Retained for interface symmetry; does NOT scope
                reads (single-tenant standalone; multi-tenant isolation is
                PostgreSQL RLS keyed on the enterprise, ADR-010/ADR-017)
            state: Narrow to one lifecycle state, in the same WHERE clause as
                the full-text predicate — and so ahead of the LIMIT, which
                ranks by ts_rank and would otherwise decide which states the
                filter ever gets to see.
            limit: Maximum results
            shared_case_ids: Case ids readable via a team share (ADR-013 §D4);
                widens owner-only scope to ``owned ∪ shared-to-my-teams``.
            restrict_case_ids: Filter-by-team facet — narrows the result to one
                team's shared case ids (the caller resolves/authorizes the team).

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

            # owned ∪ shared-to-my-teams (ADR-013 §D4); owner-only when no shares.
            # restrict_case_ids narrows to one team's shares (filter-by-team).
            # The full-text query aliases ``cases`` as ``c``, so scope on ``c.``.
            scope_clause = case_scope_where(
                params,
                user_id,
                shared_case_ids,
                col_prefix="c.",
                restrict_case_ids=restrict_case_ids,
            )
            if scope_clause:
                where_clauses.append(scope_clause)

            # No per-query org filter: multi-tenant isolation is in-core
            # PostgreSQL RLS (ADR-010); standalone is single-tenant.

            # Lifecycle state — spelled as `list` spells it, qualified with the
            # `c.` alias this query uses, and in the same WHERE clause as the
            # text predicate so it constrains what the LIMIT ranks.
            if state:
                where_clauses.append("c.state = :state")
                params["state"] = state.value

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # The TRUE match count, from the same WHERE clause and BEFORE the
            # LIMIT — as ``list`` above computes it. This method's contract is
            # ``(cases, total_count)`` and it was returning ``len(cases)``, the
            # page length. Nothing consumes it today, which is precisely why it
            # could stay wrong unnoticed. Same safe-direction divergence
            # ``list`` documents: a raw COUNT(*) over-reports if a row fails to
            # hydrate below, and never hides a result.
            count_query = text(f"SELECT COUNT(*) FROM cases c {where_sql}")
            total_count = (await self.db.execute(count_query, params)).scalar() or 0

            # Search query with relevance ranking.
            # Evidence JOIN removed per Principle 3 (Database Boundaries), so
            # `cases c` has no fan-out and each case_id is already unique — do
            # NOT re-add `SELECT DISTINCT`: with DISTINCT, PostgreSQL requires
            # every ORDER BY expression to be in the select list, so
            # `ORDER BY ..., c.updated_at` raised "for SELECT DISTINCT, ORDER BY
            # expressions must appear in select list" — which the service caught
            # and turned into an empty result, silently breaking ALL search
            # (title and case-id alike).
            search_query = text(f"""
                SELECT c.case_id,
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

            return cases, total_count

        except Exception as e:
            raise RepositoryException(f"Failed to search cases: {e}") from e

    # ========================================================================
    # Message Operations (Normalized Table)
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
            probe = await self.db.execute(
                text("SELECT 1 FROM cases WHERE case_id = :case_id"),
                {"case_id": case_id},
            )
            if probe.fetchone() is None:
                return False

            # ``timestamp`` is an accepted alias for ``created_at`` on this
            # backend; resolve it BEFORE normalising, or the normaliser fills
            # ``created_at`` with ``now()`` and the alias is silently ignored.
            # A COPY, unlike the aggregate save's in-place call: this method
            # does a plain INSERT with no ON CONFLICT, so nothing needs writing
            # back, and stamping the caller's dict would make a REUSED template
            # dict carry the first call's id into the second, where it hits the
            # primary key. ``add_message`` has always been non-mutating.
            row = dict(message_dict)
            # Shared with ``_upsert_messages`` so the two writers of this table
            # cannot disagree about an incomplete row (#1418).
            self.normalise_message_row(row, stamp_created_at=True)
            message_id = row["message_id"]
            # Coerce: message dicts may carry an ISO-STRING created_at, which
            # asyncpg rejects for the timestamptz column (see _as_datetime).
            # ``row`` already carries a created_at (supplied, aliased, or
            # minted by the normaliser); coerce it because the normaliser mints
            # an ISO STRING and asyncpg rejects a str for timestamptz.
            created_at = _as_datetime(row["created_at"], datetime.now(timezone.utc))

            query = text(f"""
                INSERT INTO case_messages (
                    message_id, case_id, enterprise_id, organization_id, turn_number, role, content,
                    author_id, created_at, token_count, metadata
                ) VALUES (
                    :message_id, :case_id,
                    (SELECT enterprise_id FROM cases
                     WHERE case_id = {self._org_lookup_case_id()}),
                    (SELECT organization_id FROM cases
                     WHERE case_id = {self._org_lookup_case_id()}),
                    :turn_number, :role, :content,
                    :author_id, :created_at, :token_count, {_cast(self._is_pg, 'metadata')}
                )
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
    ) -> List[dict]:
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

    # ========================================================================
    # Report Operations (TD-001: migrated from IReportStore)
    # ========================================================================

    async def add_report(self, report: "CaseReport") -> "CaseReport":
        """Add report to PostgreSQL reports table in its own transaction."""
        await _insert_report(self._is_pg, self.db, report)
        await self.db.commit()
        return report

    async def get_report(self, report_id: str) -> Optional["CaseReport"]:
        """Get report by ID from PostgreSQL."""
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

    async def get_reports(
        self,
        case_id: str,
        report_type: Optional["ReportType"] = None,
        include_history: bool = False,
        only_current: bool = False,
    ) -> List["CaseReport"]:
        """Get reports for a case with optional filtering."""
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

        return [_row_to_report(row) for row in rows]

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

        update_query = text(f"""
            UPDATE reports
            SET version = :version,
                is_current = :is_current,
                linked_to_closure = :linked_to_closure,
                title = :title,
                content = :content,
                format = :format,
                generation_status = :generation_status,
                generation_time_ms = :generation_time_ms,
                metadata = {_cast(self._is_pg, 'metadata')},
                updated_at = {_cast(self._is_pg, 'updated_at', 'TIMESTAMPTZ')}
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

    # ============================================================
    # Agent Execution & Tool Call Persistence (PostgreSQL)
    # Schema reference: docs/architecture/data-and-storage/schemas/case-schema.md §4.11
    # ============================================================

    async def create_checkpoint(self, checkpoint: CaseCheckpoint) -> CaseCheckpoint:
        """Create a new case checkpoint in its own transaction (PostgreSQL)."""
        try:
            await _insert_checkpoint(self._is_pg, self.db, checkpoint)
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

            return _row_to_case_checkpoint(row)

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

            return [_row_to_case_checkpoint(row) for row in rows]

        except Exception as e:
            raise RepositoryException(
                f"Failed to get checkpoints for case {case_id}: {e}"
            ) from e


class RepositoryException(Exception):
    """Exception raised for repository errors."""

    pass
