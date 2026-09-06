"""Integration tests for SQLiteCaseRepository with real SQLite database.

This test module verifies that SQLiteCaseRepository works correctly with
actual SQLite database operations, not mocked sessions.

These tests address the design gap documented in:
docs/architecture/data-and-storage/data-storage-design-gaps.md

Key validations:
1. Case creation works without PostgreSQL-specific type casts (::jsonb)
2. Case retrieval works without PostgreSQL functions (jsonb_build_object, FILTER)
3. Search works with LIKE instead of to_tsvector/ts_rank
4. All CRUD operations use SQLite-compatible SQL
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from faultmaven.infrastructure.persistence.models import Base


@pytest.fixture
async def sqlite_db_path():
    """Create a temporary SQLite database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest.fixture
async def sqlite_engine(sqlite_db_path):
    """Create SQLAlchemy async engine for SQLite with schema from ORM models."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{sqlite_db_path}",
        echo=False,
        poolclass=NullPool,
    )
    # Schema comes from the ORM — single source of truth, no drift between
    # hand-rolled CREATE TABLE, models.py, and alembic on column additions.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sqlite_session(sqlite_engine):
    """Create SQLAlchemy async session for SQLite."""
    async_session_factory = sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        yield session


@pytest.mark.asyncio
@pytest.mark.integration
class TestSQLiteCaseRepository:
    """Integration tests for SQLiteCaseRepository with real SQLite database."""

    async def test_case_creation_sqlite_compatible(self, sqlite_session):
        """Test that case creation works with SQLite (no ::jsonb type casts).

        This test verifies the fix for the issue documented in:
        docs/architecture/data-and-storage/data-storage-design-gaps.md

        Expected error BEFORE fix:
            sqlite3.ProgrammingError: Incorrect number of bindings supplied.
            The current statement uses 16, and there are 6 supplied.
            [SQL: INSERT INTO cases (...) VALUES (..., :inquiry::jsonb, ...)]
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        # Create repository with real SQLite session
        repo = SQLiteCaseRepository(sqlite_session)

        # Create test case
        case_id = f"case_{uuid4().hex[:12]}"
        test_case = Case(
            case_id=case_id,
            user_id="test_user_123",
            enterprise_id="test_ent_123",
            title="Test Case for SQLite Compatibility",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        # This should NOT raise:
        # sqlite3.ProgrammingError: Incorrect number of bindings supplied
        saved_case = await repo.save(test_case)

        # Verify case was saved
        assert saved_case is not None
        assert saved_case.case_id == case_id

        # Verify case can be retrieved
        retrieved_case = await repo.get(case_id)
        assert retrieved_case is not None
        assert retrieved_case.case_id == case_id
        assert retrieved_case.title == "Test Case for SQLite Compatibility"

    async def test_case_retrieval_sqlite_compatible(self, sqlite_session):
        """Test that case retrieval works with SQLite (no jsonb_build_object, FILTER).

        Expected error BEFORE fix:
            sqlite3.OperationalError: unrecognized token: ":"
            [SQL: SELECT ... '[]'::json ... jsonb_build_object(...) FILTER (WHERE ...)]
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create and save case
        case_id = f"case_{uuid4().hex[:12]}"
        test_case = Case(
            case_id=case_id,
            user_id="test_user_456",
            enterprise_id="test_ent_456",
            title="Retrieval Test Case",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        await repo.save(test_case)

        # This should NOT raise:
        # sqlite3.OperationalError: unrecognized token
        retrieved = await repo.get(case_id)

        assert retrieved is not None
        assert retrieved.case_id == case_id
        assert retrieved.user_id == "test_user_456"

    async def test_case_search_sqlite_compatible(self, sqlite_session):
        """Test that search works with SQLite LIKE (no to_tsvector/ts_rank).

        PostgreSQL uses to_tsvector/ts_rank for full-text search.
        SQLite uses LIKE pattern matching instead.
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create multiple test cases
        for i, title in enumerate(
            [
                "Database Error Investigation",
                "Network Latency Issue",
                "Memory Leak Analysis",
            ]
        ):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id="search_user",
                enterprise_id="search_ent",
                title=title,
                state=CaseState.INQUIRY,
                inquiry=InquiryData(),
                documentation=DocumentationData(),
                progress=InvestigationProgress(),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            await repo.save(case)

        # Search for "Database" - should find 1 result
        results, count = await repo.search("Database", user_id="search_user")
        assert len(results) >= 1
        assert any("Database" in c.title for c in results)

        # Search for "Issue" - should find at least 1 result
        results2, count2 = await repo.search("Issue", user_id="search_user")
        assert len(results2) >= 1

    async def test_case_list_sqlite_compatible(self, sqlite_session):
        """Test that list operation works with SQLite."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create test cases
        user_id = f"user_{uuid4().hex[:8]}"
        enterprise_id = f"ent_{uuid4().hex[:8]}"
        for i in range(3):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"List Test Case {i}",
                state=CaseState.INQUIRY,
                inquiry=InquiryData(),
                documentation=DocumentationData(),
                progress=InvestigationProgress(),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            await repo.save(case)

        # List cases
        cases, total = await repo.list(user_id=user_id)
        assert len(cases) == 3
        assert total == 3

    async def test_list_pagination_and_include_empty_soundness(self, sqlite_session):
        """Real SQLite: limit/offset paginate and include_empty is pushed into
        the WHERE clause so the COUNT and the SELECT stay consistent.

        This is the pagination-soundness contract for the case-list endpoint:
        the total reflects the same filters as the page, and empty cases
        (current_turn == 0) are excluded from BOTH the count and every page
        when include_empty=False.
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        user_id = f"user_{uuid4().hex[:8]}"
        enterprise_id = f"ent_{uuid4().hex[:8]}"

        # 4 active (current_turn > 0) + 2 empty (current_turn == 0) = 6 rows.
        for i in range(6):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Paginate Case {i}",
                state=CaseState.INQUIRY,
                inquiry=InquiryData(),
                documentation=DocumentationData(),
                progress=InvestigationProgress(),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            object.__setattr__(case, "current_turn", 0 if i >= 4 else i + 1)
            await repo.save(case)

        # Pagination: distinct, non-overlapping pages; total is the true count.
        page1, total1 = await repo.list(user_id=user_id, limit=2, offset=0)
        page2, total2 = await repo.list(user_id=user_id, limit=2, offset=2)
        assert total1 == 6 and total2 == 6
        assert len(page1) == 2 and len(page2) == 2
        assert {c.case_id for c in page1}.isdisjoint({c.case_id for c in page2})

        # has_more boundary: last page reports the remainder, no overrun.
        last_page, total_last = await repo.list(user_id=user_id, limit=2, offset=4)
        assert total_last == 6
        assert len(last_page) == 2
        # offset past the end returns an empty page but the true total.
        beyond, total_beyond = await repo.list(user_id=user_id, limit=2, offset=6)
        assert beyond == []
        assert total_beyond == 6

        # include_empty pushed into SQL: total drops to 4 and every returned row
        # is non-empty. Walk all pages and confirm page/total agree.
        _, total_active = await repo.list(user_id=user_id, include_empty=False)
        assert total_active == 4
        seen, offset = [], 0
        while offset < total_active:
            page, total = await repo.list(
                user_id=user_id, include_empty=False, limit=3, offset=offset
            )
            assert total == 4  # count matches the filtered SELECT, every page
            seen.extend(page)
            offset += 3
        assert len(seen) == 4
        assert all(c.current_turn > 0 for c in seen)

    async def test_message_operations_sqlite_compatible(self, sqlite_session):
        """Test that message operations work with SQLite (no ::jsonb)."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create case first
        case_id = f"case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="msg_user",
            enterprise_id="msg_ent",
            title="Message Test Case",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await repo.save(case)

        # Add message (should NOT fail with ::jsonb type cast error)
        result = await repo.add_message(
            case_id,
            {
                "role": "user",
                "current_phase": "inquiry",
                "content": "This is a test message",
                "metadata": {"source": "test"},
            },
        )
        assert result is True

        # Retrieve messages
        messages = await repo.get_messages(case_id)
        assert len(messages) == 1
        assert messages[0]["content"] == "This is a test message"
        assert messages[0]["role"] == "user"

    async def test_analytics_sqlite_compatible(self, sqlite_session):
        """Test that analytics work with SQLite (no FILTER clause)."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create case
        case_id = f"case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="analytics_user",
            enterprise_id="analytics_ent",
            title="Analytics Test Case",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await repo.save(case)

        # Get analytics (should NOT fail with FILTER clause error)
        analytics = await repo.get_analytics(case_id)
        assert analytics is not None
        assert "hypothesis_count" in analytics
        assert "solution_count" in analytics
        assert "message_count" in analytics

    async def test_case_delete_sqlite_compatible(self, sqlite_session):
        """Test that delete works with SQLite."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create and save case
        case_id = f"case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="delete_user",
            enterprise_id="delete_ent",
            title="Delete Test Case",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await repo.save(case)

        # Verify it exists
        assert await repo.get(case_id) is not None

        # Delete
        result = await repo.delete(case_id)
        assert result is True

        # Verify it's gone
        assert await repo.get(case_id) is None

    async def test_hypothesis_with_evidence_links_persistence(self, sqlite_session):
        """Test that hypotheses with evidence_links containing datetime fields persist correctly.

        Regression test for datetime serialization bug where model_dump() was used
        instead of model_dump(mode='json'), causing JSON serialization to fail when
        HypothesisEvidenceLink objects with analyzed_at datetime fields were persisted.

        This bug affected 6 repository files:
        - sqlite_case_repository.py (line 1300)
        - postgresql_hybrid_case_repository.py (line 1115)
        - database_case_repository.py
        - modules/case/infrastructure/case_repository.py
        - infrastructure/case_repository.py

        The crash occurred because json.dumps() cannot serialize datetime objects
        directly - they must be converted to ISO format strings via model_dump(mode='json').

        This test ensures:
        1. Hypotheses with populated evidence_links can be saved
        2. Each HypothesisEvidenceLink with analyzed_at datetime serializes correctly
        3. Datetime values are preserved through save/retrieve cycles
        4. Multiple evidence links per hypothesis work correctly
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            EvidenceStance,
            Hypothesis,
            HypothesisCategory,
            HypothesisEvidenceLink,
            HypothesisGenerationMode,
            HypothesisState,
            InquiryData,
            InvestigationProgress,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create test case
        case_id = f"case_{uuid4().hex[:12]}"

        # Create HypothesisEvidenceLink objects with datetime fields
        # This is what was failing before - the analyzed_at datetime couldn't be JSON serialized
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(minutes=10)

        evidence_link_1 = HypothesisEvidenceLink(
            hypothesis_id="hyp_0123456789ab",
            evidence_id="evidence_001",
            stance=EvidenceStance.SUPPORTS,
            reasoning="Log shows database connection pool exhausted at incident time",
            stance_confidence=0.9,
            analyzed_at=now,  # ← This datetime field caused the crash
        )

        evidence_link_2 = HypothesisEvidenceLink(
            hypothesis_id="hyp_0123456789ab",
            evidence_id="evidence_002",
            stance=EvidenceStance.REFUTES,
            reasoning="CPU metrics remained normal, ruling out CPU exhaustion",
            stance_confidence=0.85,
            analyzed_at=earlier,  # ← Different datetime value
        )

        # Create hypothesis with evidence_links populated
        hypothesis = Hypothesis(
            hypothesis_id="hyp_0123456789ab",
            statement="Database connection pool exhaustion caused the timeout errors",
            category=HypothesisCategory.DATABASE,
            state=HypothesisState.ACTIVE,
            likelihood=0.8,
            initial_likelihood=0.5,
            evidence_links=[
                evidence_link_1,
                evidence_link_2,
            ],  # ← This list with datetime-containing objects caused JSON serialization failure
            generated_at_turn=1,
            last_updated_turn=2,
            generation_mode=HypothesisGenerationMode.SYSTEMATIC,
            rationale="Connection timeout errors correlate with high database load",
        )

        # Create case with hypothesis
        # Use INQUIRY status to avoid INVESTIGATING validation requirements
        test_case = Case(
            case_id=case_id,
            user_id="test_user_evidence_links",
            enterprise_id="test_ent_evidence_links",
            title="Test Case for Evidence Links Serialization",
            description="Testing hypothesis evidence links serialization with datetime fields",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            hypotheses={hypothesis.hypothesis_id: hypothesis},  # Dict[str, Hypothesis]
            created_at=now,
            updated_at=now,
        )

        # This should NOT raise:
        # TypeError: Object of type datetime is not JSON serializable
        # (which occurred when using model_dump() instead of model_dump(mode='json'))
        #
        # THE KEY TEST: This save operation exercises the exact code path that was failing:
        # sqlite_case_repository.py line 1300-1303:
        #   "evidence_links": json.dumps({
        #       eid: link.model_dump(mode='json')  # FIXED - was model_dump()
        #       for eid, link in hypothesis.evidence_links.items()
        #   })
        #
        # Without mode='json', json.dumps() would fail with:
        # TypeError: Object of type datetime is not JSON serializable
        saved_case = await repo.save(test_case)

        # Verify case was saved successfully (the critical test)
        assert saved_case is not None
        assert saved_case.case_id == case_id

        # Verify the hypothesis with evidence_links was persisted
        assert len(saved_case.hypotheses) == 1
        saved_hypothesis = list(saved_case.hypotheses.values())[0]
        assert saved_hypothesis.hypothesis_id == "hyp_0123456789ab"

        # Verify evidence links were serialized and saved
        # (The fact we got here without a JSON serialization error proves the fix works)
        # evidence_links is now a List[HypothesisEvidenceLink] (was Dict[str, ...]).
        assert len(saved_hypothesis.evidence_links) == 2
        links_by_evidence_id = {
            link.evidence_id: link for link in saved_hypothesis.evidence_links
        }
        assert "evidence_001" in links_by_evidence_id
        assert "evidence_002" in links_by_evidence_id

        # Verify the HypothesisEvidenceLink objects with datetime fields are present
        link_1 = links_by_evidence_id["evidence_001"]
        assert isinstance(link_1, HypothesisEvidenceLink)
        assert link_1.evidence_id == "evidence_001"
        assert link_1.stance == EvidenceStance.SUPPORTS
        assert isinstance(
            link_1.analyzed_at, datetime
        )  # Datetime field survived serialization

        link_2 = links_by_evidence_id["evidence_002"]
        assert isinstance(link_2, HypothesisEvidenceLink)
        assert link_2.evidence_id == "evidence_002"
        assert link_2.stance == EvidenceStance.REFUTES
        assert isinstance(
            link_2.analyzed_at, datetime
        )  # Datetime field survived serialization

        # SUCCESS: If we reached here, the datetime serialization bug is fixed
        # The test would have crashed at the save() call above if model_dump(mode='json') wasn't used


@pytest.mark.asyncio
@pytest.mark.integration
class TestDialectDetection:
    """Test that SessionlessCaseRepository correctly detects SQLite dialect."""

    async def test_dialect_detection_selects_sqlite_repo(self, sqlite_session):
        """Test that get_repository_for_session returns SQLiteCaseRepository for SQLite."""
        from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
            get_repository_for_session,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = get_repository_for_session(sqlite_session)

        # Should return SQLiteCaseRepository for SQLite dialect
        assert isinstance(repo, SQLiteCaseRepository)


@pytest.mark.asyncio
@pytest.mark.integration
class TestUploadedFilePreprocessingRoundtrip:
    """Verify migration-010 preprocessing artifacts roundtrip through save/load.

    Pre-fix the SQLite repository's INSERT and SELECT both omitted the five
    columns added by migration 010 (``summary``, ``structural_index``,
    ``data_type``, ``coverage_start_ts``, ``coverage_end_ts``), so the
    preprocessing pipeline's output was set in memory and silently dropped
    on save / reloaded as None on the next turn.
    """

    async def test_preprocessing_columns_roundtrip(self, sqlite_session):
        """save() then get() must preserve all five preprocessing fields."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
            UploadedFile,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"
        coverage_start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        coverage_end = datetime(2026, 5, 1, 14, 30, 0, tzinfo=timezone.utc)
        structural_index = (
            '{"v":1,"file_extract":"ERROR: OOM at 14:03","search_map":"[search: OOM]"}'
        )

        case = Case(
            case_id=case_id,
            user_id="user_001",
            enterprise_id="00000000-0000-0000-0000-000000000001",
            title="Preprocessing roundtrip case",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_files=[
                UploadedFile(
                    file_id=file_id,
                    filename="app.log",
                    size_bytes=2048,
                    content_type="text/plain",
                    storage_ref="local://test/app.log",
                    upload_source="file_upload",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    uploaded_by="user_001",
                    summary="OOM error burst between 14:02 and 14:30 UTC",
                    structural_index=structural_index,
                    data_type="logs",
                    coverage_start_ts=coverage_start,
                    coverage_end_ts=coverage_end,
                )
            ],
        )

        await repo.save(case)
        retrieved = await repo.get(case_id)

        assert retrieved is not None
        assert len(retrieved.uploaded_files) == 1
        uf = retrieved.uploaded_files[0]
        assert uf.summary == "OOM error burst between 14:02 and 14:30 UTC"
        assert uf.structural_index == structural_index
        assert uf.data_type == "logs"
        # SQLite stores datetimes as ISO strings; Pydantic re-parses to datetime.
        assert uf.coverage_start_ts is not None
        assert uf.coverage_end_ts is not None
        assert uf.coverage_start_ts.replace(tzinfo=None) == coverage_start.replace(
            tzinfo=None
        )
        assert uf.coverage_end_ts.replace(tzinfo=None) == coverage_end.replace(
            tzinfo=None
        )

    async def test_coalesce_preserves_prior_extraction_on_null_reupsert(
        self, sqlite_session
    ):
        """Re-upserting with NULL preprocessing fields must not clobber the
        prior values — `_upsert_uploaded_files` uses COALESCE so a failed
        re-run cannot erase a good extraction.
        """
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
            UploadedFile,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"

        # First save: file with a populated structural_index.
        case = Case(
            case_id=case_id,
            user_id="user_001",
            enterprise_id="00000000-0000-0000-0000-000000000001",
            title="COALESCE upsert test",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_files=[
                UploadedFile(
                    file_id=file_id,
                    filename="app.log",
                    size_bytes=2048,
                    storage_ref="local://test/app.log",
                    upload_source="file_upload",
                    uploaded_at_turn=1,
                    uploaded_at=datetime.now(timezone.utc),
                    summary="initial summary",
                    structural_index='{"v":1,"file_extract":"initial"}',
                    data_type="logs",
                ),
            ],
        )
        await repo.save(case)

        # Second save: same file_id, but preprocessing fields NULL.
        reloaded = await repo.get(case_id)
        assert reloaded is not None
        # Mutate the in-memory file to simulate a failed re-extraction
        # (e.g. preprocessing pipeline raised before computing artifacts).
        reloaded.uploaded_files[0] = UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=2048,
            storage_ref="local://test/app.log",
            upload_source="file_upload",
            uploaded_at_turn=2,
            uploaded_at=datetime.now(timezone.utc),
            summary=None,
            structural_index=None,
            data_type=None,
        )
        await repo.save(reloaded)

        # Third load: prior preprocessing data must still be there.
        final = await repo.get(case_id)
        assert final is not None
        uf = final.uploaded_files[0]
        assert uf.summary == "initial summary"
        assert uf.structural_index == '{"v":1,"file_extract":"initial"}'
        assert uf.data_type == "logs"
        # Mutable fields (turn) still update normally.
        assert uf.uploaded_at_turn == 2


@pytest.mark.asyncio
@pytest.mark.integration
class TestScopedAddUploadedFile:
    """`add_uploaded_file` against the real SQLite repository.

    The unit tests for the upload-durability fix mock this method, so they
    prove the service CALLS it and nothing about whether it works.

    ⚠️ Every read-back here goes through a SEPARATE session, and that is the
    whole point. The first version of these tests read back through the same
    `sqlite_session` they wrote on, which sees the session's own uncommitted
    INSERT — so deleting `await self.db.commit()` from `add_uploaded_file` left
    all four green while uploads were again lost on rollback. The durability
    claim was unpinned by the tests meant to pin it. A second session sees only
    COMMITTED data, so that mutation is now red.
    """

    def _fresh_session(self, engine):
        """A session that shares the database file but not the transaction."""
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()

    def _case(self, case_id: str):
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )

        return Case(
            case_id=case_id,
            user_id="user_001",
            enterprise_id="00000000-0000-0000-0000-000000000001",
            title="Scoped upload commit",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _file(self, file_id: str, *, turn: int = 1, summary: str | None = "burst"):
        from faultmaven.modules.case.domain.models import UploadedFile

        return UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=2048,
            content_type="text/plain",
            content_hash="a" * 64,
            storage_ref="local://test/app.log",
            upload_source="file_upload",
            uploaded_at_turn=turn,
            uploaded_at=datetime.now(timezone.utc),
            uploaded_by="user_001",
            summary=summary,
            data_type="logs",
        )

    async def test_row_is_durable_without_an_aggregate_save(
        self, sqlite_session, sqlite_engine
    ):
        """Committed on its own, visible to a session that never saw the write.

        This is the mutation-sensitive one: drop the commit and the fresh
        session finds nothing.
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"
        case = self._case(case_id)
        await repo.save(case)

        await repo.add_uploaded_file(
            case_id, self._file(file_id), case.enterprise_id, case.organization_id
        )

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await SQLiteCaseRepository(other).get(case_id)

        assert reloaded is not None
        assert [f.file_id for f in reloaded.uploaded_files] == [
            file_id
        ], "the row was not COMMITTED — a separate session cannot see it"
        assert reloaded.uploaded_files[0].storage_ref == "local://test/app.log"
        assert reloaded.uploaded_files[0].summary == "burst"

    async def test_dedup_lookup_finds_the_scoped_row(
        self, sqlite_session, sqlite_engine
    ):
        """Retry-dedup depends on this: the committed row must be findable by
        content hash from a later, independent transaction — that is what stops
        a retried turn storing a second copy.
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"
        case = self._case(case_id)
        await repo.save(case)

        await repo.add_uploaded_file(
            case_id, self._file(file_id), case.enterprise_id, case.organization_id
        )

        async with self._fresh_session(sqlite_engine) as other:
            found = await SQLiteCaseRepository(
                other
            ).find_uploaded_file_by_content_hash(case_id, "a" * 64)
        assert found is not None and found.file_id == file_id

    async def test_later_aggregate_save_from_a_blind_snapshot_keeps_the_row(
        self, sqlite_session, sqlite_engine
    ):
        """The safety property the docstrings claim.

        A `save(case)` later in the same turn works from a Case object loaded
        BEFORE the scoped commit, so its `uploaded_files` does not contain the
        row. If the aggregate save mirror-deleted rows missing from its
        snapshot, that save would destroy the upload. It does not —
        `_upsert_uploaded_files` is purely additive.
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"
        case = self._case(case_id)
        await repo.save(case)

        blind_snapshot = await repo.get(case_id)
        assert blind_snapshot is not None
        assert blind_snapshot.uploaded_files == []

        await repo.add_uploaded_file(
            case_id, self._file(file_id), case.enterprise_id, case.organization_id
        )

        blind_snapshot.title = "updated mid-turn"
        await repo.save(blind_snapshot)

        async with self._fresh_session(sqlite_engine) as other:
            final = await SQLiteCaseRepository(other).get(case_id)

        assert final is not None
        assert final.title == "updated mid-turn"
        assert [f.file_id for f in final.uploaded_files] == [
            file_id
        ], "the aggregate save removed a row committed by add_uploaded_file"

    async def test_recommitting_the_same_file_id_is_idempotent(
        self, sqlite_session, sqlite_engine
    ):
        """A retried commit updates in place rather than duplicating.

        The re-commit passes ``summary=None`` deliberately. Passing the same
        value would make the COALESCE assertion below vacuous — it would pass
        just as well with `COALESCE(EXCLUDED.summary, uploaded_files.summary)`
        replaced by `EXCLUDED.summary`. NULL is the only input that exercises
        the branch, and it is also the real case: a re-commit after a failed
        re-extraction carries no artifacts.
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        file_id = f"file_{uuid4().hex[:12]}"
        case = self._case(case_id)
        await repo.save(case)

        await repo.add_uploaded_file(
            case_id, self._file(file_id), case.enterprise_id, case.organization_id
        )
        await repo.add_uploaded_file(
            case_id,
            self._file(file_id, turn=2, summary=None),
            case.enterprise_id,
            case.organization_id,
        )

        async with self._fresh_session(sqlite_engine) as other:
            final = await SQLiteCaseRepository(other).get(case_id)

        assert final is not None
        assert len(final.uploaded_files) == 1
        assert final.uploaded_files[0].uploaded_at_turn == 2
        # COALESCE protected the artifact against the NULL re-commit.
        assert final.uploaded_files[0].summary == "burst"
