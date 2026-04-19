"""Unit tests for SQLiteCaseRepository.

Exercises the SQLite-backed local-mode persistence layer for cases against
a real in-memory SQLite engine. Focuses on repository semantics (round-trip
persistence, query filters, deletion, transactional boundaries) rather than
Pydantic validation.

Run with:
    pytest tests/unit/modules/case/infrastructure/test_sqlite_case_repository.py -v

Coverage:
    pytest tests/unit/modules/case/infrastructure/test_sqlite_case_repository.py \
        --cov=faultmaven/modules/case/infrastructure/sqlite_case_repository \
        --cov-report=term-missing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    InquiryData,
    InvestigationStrategy,
    Solution,
    SolutionType,
    UploadedFile,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    RepositoryException,
    SQLiteCaseRepository,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Async engine with fresh in-memory SQLite database per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Async session bound to the in-memory engine."""
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def repository(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _make_case(
    *,
    user_id: str = "user_alpha",
    organization_id: str = "org_alpha",
    title: str = "Test case",
    status: CaseStatus = CaseStatus.INQUIRY,
    description: str = "",
) -> Case:
    """Build a minimal Case suitable for persistence."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id=organization_id,
        title=title,
        description=description,
        status=status,
    )


def _make_evidence(
    *,
    category: EvidenceCategory = EvidenceCategory.SYMPTOM_EVIDENCE,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
    summary: str = "Connection refused errors",
) -> Evidence:
    return Evidence(
        category=category,
        primary_purpose="symptom_verified",
        summary=summary,
        preprocessed_content="ERROR: connection refused on port 5432",
        content_size_bytes=120,
        preprocessing_method="crime_scene_extraction",
        source_type=source_type,
        form=EvidenceForm.DOCUMENT,
        collected_by="user_alpha",
        collected_at_turn=1,
    )


def _make_hypothesis(
    statement: str = "Database connection pool exhausted",
) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.DATABASE,
        status=HypothesisStatus.CAPTURED,
        likelihood=0.6,
        initial_likelihood=0.6,
        generated_at_turn=1,
        last_updated_turn=1,
        last_progress_at_turn=1,
        iterations_without_progress=0,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="Matches error pattern",
    )


def _make_solution(title: str = "Restart service") -> Solution:
    return Solution(
        title=title,
        solution_type=SolutionType.CONFIG_CHANGE,
        immediate_action="Restart the app to clear stuck connections",
        implementation_steps=["Stop service", "Start service"],
        commands=["systemctl restart app"],
        risks=["Brief downtime"],
    )


def _make_uploaded_file(filename: str = "app.log") -> UploadedFile:
    return UploadedFile(
        filename=filename,
        size_bytes=2048,
        data_type="log",
        uploaded_at_turn=1,
        source_type="file_upload",
        preprocessing_summary="3 errors observed",
    )


# ============================================================
# save / get — Core CRUD
# ============================================================


class TestSaveAndGet:
    """Round-trip persistence semantics for the main `cases` table."""

    @pytest.mark.asyncio
    async def test_saves_and_retrieves_minimal_case(self, repository):
        case = _make_case(title="API outage")

        saved = await repository.save(case)

        assert saved.case_id == case.case_id
        retrieved = await repository.get(case.case_id)
        assert retrieved is not None
        assert retrieved.case_id == case.case_id
        assert retrieved.user_id == case.user_id
        assert retrieved.organization_id == case.organization_id
        assert retrieved.title == "API outage"
        assert retrieved.status == CaseStatus.INQUIRY

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_case(self, repository):
        assert await repository.get("case_doesnotexist") is None

    @pytest.mark.asyncio
    async def test_save_updates_timestamp(self, repository):
        case = _make_case()
        original_updated_at = case.updated_at

        saved = await repository.save(case)

        # save() bumps updated_at to now(UTC)
        assert saved.updated_at >= original_updated_at

    @pytest.mark.asyncio
    async def test_save_is_upsert_by_case_id(self, repository):
        case = _make_case(title="Original")
        await repository.save(case)

        case.title = "Updated"
        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert retrieved.title == "Updated"

    @pytest.mark.asyncio
    async def test_persists_investigating_status_with_full_inquiry(self, repository):
        inquiry = InquiryData()
        inquiry.proposed_problem_statement = "Latency spike in API"
        inquiry.problem_statement_confirmed = True
        inquiry.decided_to_investigate = True
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="user_alpha",
            organization_id="org_alpha",
            title="Investigating case",
            description="Latency spike in API",
            status=CaseStatus.INVESTIGATING,
            investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
            inquiry=inquiry,
        )
        case.progress.symptom_verified = True
        case.current_turn = 3
        case.message_count = 6

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert retrieved.status == CaseStatus.INVESTIGATING
        assert retrieved.progress.symptom_verified is True
        assert retrieved.current_turn == 3
        assert retrieved.message_count == 6

    @pytest.mark.asyncio
    async def test_persists_terminal_case_with_closure(self, repository):
        now = datetime.now(timezone.utc)
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="user_alpha",
            organization_id="org_alpha",
            title="Closed case",
            description="Already closed",
            status=CaseStatus.RESOLVED,
            created_at=now - timedelta(hours=1),
            resolved_at=now,
            closed_at=now,
            closure_reason="resolved",
        )

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert retrieved.status == CaseStatus.RESOLVED
        assert retrieved.closure_reason == "resolved"
        # closed_at and resolved_at are stashed in metadata JSON for SQLite
        assert retrieved.closed_at is not None
        assert retrieved.resolved_at is not None

    @pytest.mark.asyncio
    async def test_save_rollback_wraps_in_repository_exception(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        # Make begin() return an async context manager that raises on enter
        fake_begin = AsyncMock()
        fake_begin.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        fake_begin.__aexit__ = AsyncMock(return_value=False)
        repo.db.begin = lambda: fake_begin
        repo.db.rollback = AsyncMock()

        case = _make_case()

        with pytest.raises(RepositoryException) as exc_info:
            await repo.save(case)

        assert "Failed to save case" in str(exc_info.value)
        repo.db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_wraps_errors_in_repository_exception(self, async_session):
        repository = SQLiteCaseRepository(async_session)
        repository.db = AsyncMock()
        repository.db.execute.side_effect = RuntimeError("disk gone")

        with pytest.raises(RepositoryException) as exc_info:
            await repository.get("case_abc")

        assert "Failed to get case" in str(exc_info.value)


# ============================================================
# Evidence / Hypotheses / Solutions / Files round-trip
# ============================================================


class TestRelatedDataRoundTrip:
    """Normalized table round-trip: evidence, hypotheses, solutions, files."""

    @pytest.mark.asyncio
    async def test_round_trips_evidence(self, repository):
        case = _make_case()
        case.evidence.append(_make_evidence(summary="evidence one"))
        case.evidence.append(
            _make_evidence(
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.METRICS,
                summary="evidence two",
            )
        )

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert len(retrieved.evidence) == 2
        summaries = {ev.summary for ev in retrieved.evidence}
        assert summaries == {"evidence one", "evidence two"}

    @pytest.mark.asyncio
    async def test_evidence_upsert_removes_deleted_entries(self, repository):
        case = _make_case()
        ev1 = _make_evidence(summary="keep")
        ev2 = _make_evidence(summary="discard")
        case.evidence.extend([ev1, ev2])
        await repository.save(case)

        # Remove ev2 locally and re-save
        case.evidence = [ev1]
        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert len(retrieved.evidence) == 1
        assert retrieved.evidence[0].summary == "keep"

    @pytest.mark.asyncio
    async def test_round_trips_hypotheses(self, repository):
        case = _make_case()
        h = _make_hypothesis()
        case.hypotheses[h.hypothesis_id] = h

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert h.hypothesis_id in retrieved.hypotheses
        got = retrieved.hypotheses[h.hypothesis_id]
        assert got.statement == h.statement
        assert got.category == HypothesisCategory.DATABASE
        assert got.likelihood == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_hypothesis_upsert_removes_missing_entries(self, repository):
        case = _make_case()
        h1 = _make_hypothesis(statement="Hypothesis A")
        h2 = _make_hypothesis(statement="Hypothesis B")
        case.hypotheses[h1.hypothesis_id] = h1
        case.hypotheses[h2.hypothesis_id] = h2
        await repository.save(case)

        case.hypotheses = {h1.hypothesis_id: h1}
        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert set(retrieved.hypotheses.keys()) == {h1.hypothesis_id}

    @pytest.mark.asyncio
    async def test_round_trips_solutions(self, repository):
        case = _make_case()
        sol = _make_solution(title="Scale pool")
        case.solutions.append(sol)

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert len(retrieved.solutions) == 1
        assert retrieved.solutions[0].title == "Scale pool"
        assert retrieved.solutions[0].commands == ["systemctl restart app"]

    @pytest.mark.asyncio
    async def test_round_trips_uploaded_files(self, repository):
        case = _make_case()
        case.uploaded_files.append(_make_uploaded_file("api.log"))
        case.uploaded_files.append(_make_uploaded_file("nginx.log"))

        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert {f.filename for f in retrieved.uploaded_files} == {
            "api.log",
            "nginx.log",
        }

    @pytest.mark.asyncio
    async def test_uploaded_files_upsert_removes_missing(self, repository):
        case = _make_case()
        f1 = _make_uploaded_file("keep.log")
        f2 = _make_uploaded_file("discard.log")
        case.uploaded_files.extend([f1, f2])
        await repository.save(case)

        case.uploaded_files = [f1]
        await repository.save(case)

        retrieved = await repository.get(case.case_id)
        assert [f.filename for f in retrieved.uploaded_files] == ["keep.log"]

    @pytest.mark.asyncio
    async def test_action_history_persisted_via_case_actions(self, repository):
        """action_history transitions go into case_actions table."""
        case = _make_case()
        transition = CaseAction(
            from_status=CaseStatus.INQUIRY,
            to_status=CaseStatus.INVESTIGATING,
            triggered_by="user_alpha",
            reason="Starting investigation",
        )
        case.action_history.append(transition)

        await repository.save(case)

        # The repo doesn't hydrate action_history back into the Case (empty by design),
        # but the rows should be present in the table.
        from sqlalchemy import text

        result = await repository.db.execute(
            text(
                "SELECT from_status, to_status, reason FROM case_actions WHERE case_id = :cid"
            ),
            {"cid": case.case_id},
        )
        rows = result.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "inquiry"
        assert rows[0][1] == "investigating"
        assert rows[0][2] == "Starting investigation"


# ============================================================
# list / count / search
# ============================================================


class TestListAndSearch:
    @pytest.mark.asyncio
    async def test_list_filters_by_user(self, repository):
        for _ in range(3):
            await repository.save(_make_case(user_id="user_a"))
        await repository.save(_make_case(user_id="user_b"))

        cases, total = await repository.list(user_id="user_a")

        assert total == 3
        assert len(cases) == 3
        assert all(c.user_id == "user_a" for c in cases)

    @pytest.mark.asyncio
    async def test_list_filters_by_organization(self, repository):
        await repository.save(_make_case(organization_id="org_left"))
        await repository.save(_make_case(organization_id="org_left"))
        await repository.save(_make_case(organization_id="org_right"))

        cases, total = await repository.list(organization_id="org_right")

        assert total == 1
        assert cases[0].organization_id == "org_right"

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, repository):
        await repository.save(_make_case(status=CaseStatus.INQUIRY))
        await repository.save(_make_case(status=CaseStatus.INQUIRY))
        # Build an INVESTIGATING case (requires description + inquiry)
        inq = InquiryData()
        inq.proposed_problem_statement = "X"
        inq.problem_statement_confirmed = True
        inq.decided_to_investigate = True
        investigating = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="user_alpha",
            organization_id="org_alpha",
            title="I",
            description="X",
            status=CaseStatus.INVESTIGATING,
            inquiry=inq,
        )
        await repository.save(investigating)

        inquiry_cases, total = await repository.list(status=CaseStatus.INQUIRY)

        assert total == 2
        assert all(c.status == CaseStatus.INQUIRY for c in inquiry_cases)

    @pytest.mark.asyncio
    async def test_list_pagination(self, repository):
        for _ in range(5):
            await repository.save(_make_case(user_id="user_p"))

        page1, total = await repository.list(user_id="user_p", limit=2, offset=0)
        page2, _ = await repository.list(user_id="user_p", limit=2, offset=2)
        page3, _ = await repository.list(user_id="user_p", limit=2, offset=4)

        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1

    @pytest.mark.asyncio
    async def test_list_orders_by_updated_at_desc(self, repository):
        c1 = _make_case(title="older", user_id="user_order")
        c2 = _make_case(title="newer", user_id="user_order")
        await repository.save(c1)
        # Force a later updated_at on c2 via save bumping it naturally
        await repository.save(c2)

        cases, _ = await repository.list(user_id="user_order")

        # c2 was saved most recently so should come first
        assert cases[0].case_id == c2.case_id

    @pytest.mark.asyncio
    async def test_list_returns_empty_when_no_match(self, repository):
        await repository.save(_make_case(user_id="user_a"))

        cases, total = await repository.list(user_id="nobody")

        assert cases == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to list cases"):
            await repo.list()

    @pytest.mark.asyncio
    async def test_search_matches_title_substring(self, repository):
        await repository.save(_make_case(title="Database connection refused"))
        await repository.save(_make_case(title="API Performance Issue"))
        await repository.save(_make_case(title="Memory leak"))

        results, count = await repository.search(query="database")

        assert count == 1
        assert "Database" in results[0].title

    @pytest.mark.asyncio
    async def test_search_matches_case_id_substring(self, repository):
        target = _make_case(title="Unrelated title")
        await repository.save(target)

        # Search using a slice of the case_id
        id_slice = target.case_id[5:10]
        results, _ = await repository.search(query=id_slice)

        assert any(r.case_id == target.case_id for r in results)

    @pytest.mark.asyncio
    async def test_search_scopes_to_user(self, repository):
        await repository.save(_make_case(user_id="user_a", title="Shared token"))
        await repository.save(_make_case(user_id="user_b", title="Shared token"))

        results, _ = await repository.search(query="Shared", user_id="user_a")

        assert len(results) == 1
        assert results[0].user_id == "user_a"

    @pytest.mark.asyncio
    async def test_search_scopes_to_organization(self, repository):
        await repository.save(_make_case(organization_id="org_1", title="Net issue"))
        await repository.save(_make_case(organization_id="org_2", title="Net issue"))

        results, _ = await repository.search(query="Net", organization_id="org_1")

        assert len(results) == 1
        assert results[0].organization_id == "org_1"

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, repository):
        for i in range(5):
            await repository.save(_make_case(user_id="user_s", title=f"Match {i}"))

        results, _ = await repository.search(query="Match", user_id="user_s", limit=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to search cases"):
            await repo.search(query="anything")

    @pytest.mark.asyncio
    async def test_count_user_cases_on_date(self, repository):
        today = datetime.now(timezone.utc)
        for _ in range(3):
            case = _make_case(user_id="user_count")
            case.created_at = today
            await repository.save(case)

        count = await repository.count_user_cases_on_date("user_count", today.date())

        assert count == 3

    @pytest.mark.asyncio
    async def test_count_user_cases_on_date_with_string(self, repository):
        case = _make_case(user_id="user_str")
        target_date = datetime.now(timezone.utc).date()
        case.created_at = datetime.combine(
            target_date, datetime.min.time(), tzinfo=timezone.utc
        )
        await repository.save(case)

        # Pass as string (no strftime)
        count = await repository.count_user_cases_on_date("user_str", str(target_date))

        assert count == 1

    @pytest.mark.asyncio
    async def test_count_user_cases_on_date_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to count user cases"):
            await repo.count_user_cases_on_date("u", datetime.now(timezone.utc).date())


# ============================================================
# delete
# ============================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_on_success(self, repository):
        case = _make_case()
        await repository.save(case)

        result = await repository.delete(case.case_id)

        assert result is True
        assert await repository.get(case.case_id) is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, repository):
        result = await repository.delete("case_doesnotexist")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("disk gone")

        with pytest.raises(RepositoryException, match="Failed to delete case"):
            await repo.delete("case_abc")


# ============================================================
# Messages
# ============================================================


class TestMessages:
    @pytest.mark.asyncio
    async def test_add_message_persists_and_round_trips(self, repository):
        case = _make_case()
        await repository.save(case)

        result = await repository.add_message(
            case.case_id,
            {
                "message_id": f"msg_{uuid4().hex[:16]}",
                "role": "user",
                "content": "What is happening?",
                "turn_number": 1,
                "token_count": 4,
            },
        )
        assert result is True

        msgs = await repository.get_messages(case.case_id)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "What is happening?"
        assert msgs[0]["role"] == "user"
        assert msgs[0]["turn_number"] == 1
        assert msgs[0]["token_count"] == 4

    @pytest.mark.asyncio
    async def test_add_message_defaults_message_id(self, repository):
        case = _make_case()
        await repository.save(case)

        await repository.add_message(
            case.case_id, {"role": "assistant", "content": "Hi", "turn_number": 0}
        )

        msgs = await repository.get_messages(case.case_id)
        assert len(msgs) == 1
        assert msgs[0]["message_id"].startswith("msg_")

    @pytest.mark.asyncio
    async def test_add_message_serializes_metadata(self, repository):
        case = _make_case()
        await repository.save(case)
        metadata = {"source": "copilot", "tags": ["a", "b"]}

        await repository.add_message(
            case.case_id,
            {
                "message_id": f"msg_{uuid4().hex[:16]}",
                "role": "user",
                "content": "meta test",
                "turn_number": 1,
                "metadata": metadata,
            },
        )

        msgs = await repository.get_messages(case.case_id)
        assert msgs[0]["metadata"] == metadata

    @pytest.mark.asyncio
    async def test_add_message_wraps_errors(self, repository):
        # Missing case_id reference; SQLite FK isn't enforced but the subquery
        # COALESCE returns a fallback. Force a real error via broken execute.
        repository.db.execute = AsyncMock(side_effect=RuntimeError("disk gone"))
        repository.db.rollback = AsyncMock()

        with pytest.raises(RepositoryException, match="Failed to add message"):
            await repository.add_message(
                "case_abc", {"role": "user", "content": "hi", "turn_number": 1}
            )

    @pytest.mark.asyncio
    async def test_get_messages_pagination(self, repository):
        case = _make_case()
        await repository.save(case)

        base_time = datetime.now(timezone.utc)
        for i in range(5):
            await repository.add_message(
                case.case_id,
                {
                    "message_id": f"msg_{uuid4().hex[:16]}",
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"message {i}",
                    "turn_number": i,
                    "created_at": base_time + timedelta(seconds=i),
                },
            )

        page1 = await repository.get_messages(case.case_id, limit=2, offset=0)
        page2 = await repository.get_messages(case.case_id, limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["content"] == "message 0"
        assert page2[0]["content"] == "message 2"

    @pytest.mark.asyncio
    async def test_get_messages_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to get messages"):
            await repo.get_messages("case_abc")

    @pytest.mark.asyncio
    async def test_case_save_upserts_messages_list(self, repository):
        case = _make_case()
        msg_id = f"msg_{uuid4().hex[:16]}"
        case.messages = [
            {
                "message_id": msg_id,
                "role": "user",
                "content": "saved via case",
                "turn_number": 1,
                "created_at": datetime.now(timezone.utc),
            }
        ]

        await repository.save(case)

        msgs = await repository.get_messages(case.case_id)
        assert len(msgs) == 1
        assert msgs[0]["message_id"] == msg_id
        assert msgs[0]["content"] == "saved via case"


# ============================================================
# Analytics
# ============================================================


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_analytics_counts_related_rows(self, repository):
        case = _make_case()
        # 2 hypotheses, 1 solution, 1 file
        h1 = _make_hypothesis(statement="hyp 1")
        h2 = _make_hypothesis(statement="hyp 2")
        case.hypotheses = {h1.hypothesis_id: h1, h2.hypothesis_id: h2}
        case.solutions.append(_make_solution())
        case.uploaded_files.append(_make_uploaded_file("sz.log"))
        await repository.save(case)

        # 1 message
        await repository.add_message(
            case.case_id,
            {
                "message_id": f"msg_{uuid4().hex[:16]}",
                "role": "user",
                "content": "hi",
                "turn_number": 1,
            },
        )

        analytics = await repository.get_analytics(case.case_id)

        assert analytics["hypothesis_count"] == 2
        assert analytics["solution_count"] == 1
        assert analytics["file_count"] == 1
        assert analytics["message_count"] == 1
        assert analytics["total_file_size"] == 2048

    @pytest.mark.asyncio
    async def test_analytics_zeros_for_empty_case(self, repository):
        case = _make_case()
        await repository.save(case)

        analytics = await repository.get_analytics(case.case_id)

        assert analytics["hypothesis_count"] == 0
        assert analytics["solution_count"] == 0
        assert analytics["message_count"] == 0
        assert analytics["file_count"] == 0
        assert analytics["total_file_size"] == 0
        assert analytics["validated_hypotheses"] == 0
        assert analytics["evidence_count"] == 0

    @pytest.mark.asyncio
    async def test_analytics_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to get analytics"):
            await repo.get_analytics("case_abc")


# ============================================================
# update_activity_timestamp
# ============================================================


class TestActivityTimestamp:
    """
    update_activity_timestamp refreshes the case's `updated_at` column
    (which serves as the activity marker — the ORM schema has no dedicated
    `last_activity_at` column).
    """

    @pytest.mark.asyncio
    async def test_update_activity_timestamp_success(self, repository):
        case = _make_case()
        await repository.save(case)

        updated = await repository.update_activity_timestamp(case.case_id)

        assert updated is True

    @pytest.mark.asyncio
    async def test_update_activity_timestamp_missing_case(self, repository):
        # Unknown case_id: the UPDATE matches zero rows, returns False.
        updated = await repository.update_activity_timestamp("case_does_not_exist")

        assert updated is False

    @pytest.mark.asyncio
    async def test_update_activity_timestamp_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")
        repo.db.rollback = AsyncMock()

        with pytest.raises(RepositoryException, match="Failed to update activity"):
            await repo.update_activity_timestamp("case_x")


# ============================================================
# cleanup_expired
# ============================================================


class TestCleanupExpired:
    """
    cleanup_expired deletes closed cases whose closure timestamp is older
    than `max_age_days`. `closed_at` lives inside the metadata JSON column,
    extracted via json_extract in the DELETE query.
    """

    @pytest.mark.asyncio
    async def test_cleanup_no_expired_cases(self, repository):
        # No cases in the table → deletes nothing, returns 0.
        deleted = await repository.cleanup_expired(max_age_days=90)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_cleanup_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")
        repo.db.rollback = AsyncMock()

        with pytest.raises(RepositoryException, match="Failed to cleanup expired"):
            await repo.cleanup_expired()


# ============================================================
# Reports
# ============================================================


def _make_report(
    case_id: str, *, report_type: ReportType = ReportType.RESOLUTION_SUMMARY
) -> CaseReport:
    return CaseReport(
        case_id=case_id,
        report_type=report_type,
        title="Terminal Resolution Summary",
        content="# Summary\n\nInvestigation resolved via cache bust.",
        generation_status=ReportStatus.COMPLETED,
        generation_time_ms=1500,
    )


class TestReports:
    @pytest.mark.asyncio
    async def test_add_and_get_report(self, repository):
        case = _make_case()
        await repository.save(case)
        report = _make_report(case.case_id)

        saved = await repository.add_report(report)

        assert saved.report_id == report.report_id
        retrieved = await repository.get_report(report.report_id)
        assert retrieved is not None
        assert retrieved.title == report.title
        assert retrieved.report_type == ReportType.RESOLUTION_SUMMARY
        assert retrieved.generation_status == ReportStatus.COMPLETED
        assert retrieved.is_current is True

    @pytest.mark.asyncio
    async def test_get_report_missing_returns_none(self, repository):
        assert await repository.get_report("report_missing") is None

    @pytest.mark.asyncio
    async def test_add_report_unmarks_previous_current(self, repository):
        case = _make_case()
        await repository.save(case)
        first = _make_report(case.case_id)
        await repository.add_report(first)

        # Save a new current report of the same type
        second = _make_report(case.case_id)
        await repository.add_report(second)

        first_reloaded = await repository.get_report(first.report_id)
        second_reloaded = await repository.get_report(second.report_id)
        assert first_reloaded.is_current is False
        assert second_reloaded.is_current is True

    @pytest.mark.asyncio
    async def test_get_reports_only_current_by_default(self, repository):
        case = _make_case()
        await repository.save(case)
        first = _make_report(case.case_id)
        await repository.add_report(first)
        second = _make_report(case.case_id)
        await repository.add_report(second)  # makes first no longer current

        reports = await repository.get_reports(case.case_id)

        assert len(reports) == 1
        assert reports[0].report_id == second.report_id

    @pytest.mark.asyncio
    async def test_get_reports_include_history_returns_all_versions(self, repository):
        case = _make_case()
        await repository.save(case)
        first = _make_report(case.case_id)
        await repository.add_report(first)
        second = _make_report(case.case_id)
        await repository.add_report(second)

        reports = await repository.get_reports(case.case_id, include_history=True)

        assert len(reports) == 2

    @pytest.mark.asyncio
    async def test_get_reports_filters_by_type(self, repository):
        case = _make_case()
        await repository.save(case)
        res_report = _make_report(
            case.case_id, report_type=ReportType.RESOLUTION_SUMMARY
        )
        rb_report = _make_report(case.case_id, report_type=ReportType.RUNBOOK)
        await repository.add_report(res_report)
        await repository.add_report(rb_report)

        runbooks = await repository.get_reports(
            case.case_id, report_type=ReportType.RUNBOOK
        )

        assert len(runbooks) == 1
        assert runbooks[0].report_type == ReportType.RUNBOOK

    @pytest.mark.asyncio
    async def test_update_report_changes_content(self, repository):
        case = _make_case()
        await repository.save(case)
        report = _make_report(case.case_id)
        await repository.add_report(report)

        report.content = "# Updated\n\nNew content"
        report.title = "Updated Resolution Summary"
        await repository.update_report(report)

        retrieved = await repository.get_report(report.report_id)
        assert retrieved.content == "# Updated\n\nNew content"
        assert retrieved.title == "Updated Resolution Summary"

    @pytest.mark.asyncio
    async def test_update_report_nonexistent_raises(self, repository):
        report = _make_report("case_missing_xx")
        with pytest.raises(RepositoryException, match="not found"):
            await repository.update_report(report)

    @pytest.mark.asyncio
    async def test_delete_report(self, repository):
        case = _make_case()
        await repository.save(case)
        report = _make_report(case.case_id)
        await repository.add_report(report)

        deleted = await repository.delete_report(report.report_id)

        assert deleted is True
        assert await repository.get_report(report.report_id) is None

    @pytest.mark.asyncio
    async def test_delete_report_missing(self, repository):
        assert await repository.delete_report("report_missing") is False


# ============================================================
# Checkpoints
# ============================================================


def _make_checkpoint(case_id: str, turn: int = 1) -> CaseCheckpoint:
    return CaseCheckpoint(
        checkpoint_id=f"{case_id}:turn:{turn}",
        case_id=case_id,
        turn_number=turn,
        case_snapshot={"status": "inquiry", "turn": turn},
        snapshot_hash=f"hash_{turn}",
        trigger="turn_complete",
        created_at=datetime.now(timezone.utc),
        metadata={"note": f"checkpoint {turn}"},
    )


class TestCheckpoints:
    @pytest.mark.asyncio
    async def test_create_and_get_checkpoint(self, repository):
        case = _make_case()
        await repository.save(case)
        cp = _make_checkpoint(case.case_id, turn=1)

        saved = await repository.create_checkpoint(cp)
        retrieved = await repository.get_checkpoint(saved.checkpoint_id)

        assert retrieved is not None
        assert retrieved.checkpoint_id == cp.checkpoint_id
        assert retrieved.turn_number == 1
        assert retrieved.snapshot_hash == "hash_1"
        assert retrieved.case_snapshot["turn"] == 1
        assert retrieved.metadata == {"note": "checkpoint 1"}

    @pytest.mark.asyncio
    async def test_get_checkpoint_missing_returns_none(self, repository):
        assert await repository.get_checkpoint("nope") is None

    @pytest.mark.asyncio
    async def test_get_checkpoints_ordered_by_turn_asc(self, repository):
        case = _make_case()
        await repository.save(case)
        await repository.create_checkpoint(_make_checkpoint(case.case_id, turn=2))
        await repository.create_checkpoint(_make_checkpoint(case.case_id, turn=1))
        await repository.create_checkpoint(_make_checkpoint(case.case_id, turn=3))

        checkpoints = await repository.get_checkpoints(case.case_id)

        assert [c.turn_number for c in checkpoints] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_get_checkpoints_empty_list_for_case_without_checkpoints(
        self, repository
    ):
        case = _make_case()
        await repository.save(case)

        checkpoints = await repository.get_checkpoints(case.case_id)
        assert checkpoints == []

    @pytest.mark.asyncio
    async def test_create_checkpoint_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")
        repo.db.rollback = AsyncMock()

        cp = _make_checkpoint("case_abc", turn=1)
        with pytest.raises(RepositoryException, match="Failed to create checkpoint"):
            await repo.create_checkpoint(cp)

    @pytest.mark.asyncio
    async def test_get_checkpoint_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to get checkpoint"):
            await repo.get_checkpoint("cp_abc")

    @pytest.mark.asyncio
    async def test_get_checkpoints_wraps_errors(self, async_session):
        repo = SQLiteCaseRepository(async_session)
        repo.db = AsyncMock()
        repo.db.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RepositoryException, match="Failed to get checkpoints"):
            await repo.get_checkpoints("case_abc")


# ============================================================
# Stubbed operations (share, agent executions)
# ============================================================


class TestStubbedOperations:
    """Stubbed methods should raise NotImplementedError to signal unsupported-in-SQLite."""

    @pytest.mark.asyncio
    async def test_share_case_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.share_case("c", "u", "viewer")

    @pytest.mark.asyncio
    async def test_unshare_case_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.unshare_case("c", "u")

    @pytest.mark.asyncio
    async def test_get_case_participants_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.get_case_participants("c")

    @pytest.mark.asyncio
    async def test_create_agent_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.create_agent_execution(object())

    @pytest.mark.asyncio
    async def test_get_agent_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.get_agent_execution("x")

    @pytest.mark.asyncio
    async def test_list_agent_executions_by_case_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.list_agent_executions_by_case("c")

    @pytest.mark.asyncio
    async def test_list_agent_executions_by_session_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.list_agent_executions_by_session("s")

    @pytest.mark.asyncio
    async def test_update_agent_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.update_agent_execution(object())

    @pytest.mark.asyncio
    async def test_delete_agent_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.delete_agent_execution("x")

    @pytest.mark.asyncio
    async def test_create_agent_tool_call_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.create_agent_tool_call(object())

    @pytest.mark.asyncio
    async def test_update_agent_tool_call_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.update_agent_tool_call(object())

    @pytest.mark.asyncio
    async def test_get_agent_tool_calls_for_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.get_agent_tool_calls_for_execution("x")

    @pytest.mark.asyncio
    async def test_count_agent_executions_by_case_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.count_agent_executions_by_case("c")

    @pytest.mark.asyncio
    async def test_get_latest_agent_execution_stub(self, repository):
        with pytest.raises(NotImplementedError):
            await repository.get_latest_agent_execution("c")


# ============================================================
# Legacy data conversion (_convert_legacy_inquiry_data)
# ============================================================


class TestLegacyInquiryConversion:
    """_convert_legacy_inquiry_data normalizes pre-schema LLM format."""

    def test_converts_null_preliminary_guidance_to_empty_string(self, repository):
        data = {"problem_confirmation": {"preliminary_guidance": None}}
        result = repository._convert_legacy_inquiry_data(data)
        assert result["problem_confirmation"]["preliminary_guidance"] == ""

    def test_lowercases_urgency_level(self, repository):
        data = {"preliminary_urgency": {"level": "HIGH"}}
        result = repository._convert_legacy_inquiry_data(data)
        assert result["preliminary_urgency"]["level"] == "high"

    def test_adds_missing_assessed_at_turn(self, repository):
        data = {"preliminary_urgency": {"level": "medium"}}
        result = repository._convert_legacy_inquiry_data(data)
        assert result["preliminary_urgency"]["assessed_at_turn"] == 1

    def test_passes_through_unchanged_data(self, repository):
        data = {"unrelated_key": "unchanged"}
        result = repository._convert_legacy_inquiry_data(data)
        assert result == {"unrelated_key": "unchanged"}

    def test_handles_empty_dict(self, repository):
        assert repository._convert_legacy_inquiry_data({}) == {}

    def test_does_not_touch_populated_guidance(self, repository):
        data = {"problem_confirmation": {"preliminary_guidance": "already set"}}
        result = repository._convert_legacy_inquiry_data(data)
        assert result["problem_confirmation"]["preliminary_guidance"] == "already set"
