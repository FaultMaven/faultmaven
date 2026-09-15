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
from sqlalchemy import text
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

    async def test_list_creation_date_bounds_against_real_sqlite(self, sqlite_session):
        """Real SQLite: the creation-date bounds actually filter, and inclusively.

        This is the test the in-memory one cannot stand in for. The bounds go
        into raw SQL as bound parameters and are compared against a column the
        driver wrote — so what is really being asserted is that the value on the
        way IN and the value on the way OUT are rendered in the same encoding.
        A Python list comprehension over datetimes would pass while the real
        query silently matched nothing, which is the whole failure this feature
        exists to stop repeating.
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

        days = [datetime(2026, 9, d, 12, 0, tzinfo=timezone.utc) for d in (10, 11, 12)]
        for i, created in enumerate(days):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                enterprise_id=enterprise_id,
                title=f"Day {10 + i}",
                state=CaseState.INQUIRY,
                inquiry=InquiryData(),
                documentation=DocumentationData(),
                progress=InvestigationProgress(),
                created_at=created,
                updated_at=created,
            )
            object.__setattr__(case, "current_turn", 1)
            await repo.save(case)

        # Sanity: unbounded sees all three, so a later zero is a filter result
        # and not an empty table.
        _, total_all = await repo.list(user_id=user_id)
        assert total_all == 3

        # Lower bound INCLUDES the case created exactly on it.
        rows, total = await repo.list(user_id=user_id, created_after=days[1])
        assert total == 2
        assert {c.title for c in rows} == {"Day 11", "Day 12"}

        # Upper bound EXCLUDES the case created exactly on it — the window is
        # `[after, before)`.
        rows, total = await repo.list(user_id=user_id, created_before=days[1])
        assert total == 1
        assert {c.title for c in rows} == {"Day 10"}

        # `[x, x)` contains nothing, which is why a client selecting one day
        # sends the FOLLOWING day as the upper end rather than the same one.
        _, total = await repo.list(
            user_id=user_id, created_after=days[1], created_before=days[1]
        )
        assert total == 0

        # A window that excludes everything reports zero, not everything: a
        # dropped predicate would return 3 here and look like a working filter
        # on every other assertion above.
        _, total_none = await repo.list(
            user_id=user_id,
            created_after=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )
        assert total_none == 0

        # The bound constrains the COUNT as well as the page (pagination
        # soundness): a page of 1 inside a 2-case window reports 2, not 3.
        page, total = await repo.list(
            user_id=user_id, created_after=days[1], limit=1, offset=0
        )
        assert len(page) == 1
        assert total == 2

    async def test_a_bound_answers_alike_whatever_offset_it_carries(
        self, sqlite_session
    ):
        """One instant, two spellings, one answer — on the store where it went wrong.

        THIS IS THE TEST THE FIRST VERSION DID NOT HAVE, and the gap was not
        subtle once seen: `save` writes `created_at` through sqlite3's default
        datetime adapter, so the column holds the TEXT
        `'2026-09-10 23:00:00+00:00'` and `created_at >= :created_after` is a
        LEXICOGRAPHIC compare that knows nothing about the offset suffix it is
        reading. A bound of `2026-09-11T00:00:00+05:30` — the same moment as
        `2026-09-10T18:30:00Z` — sorted after the stored row and excluded it.

        Measured, not theorised: before the fix this returned 0 and the UTC
        spelling returned 1. And the route's own description invites the losing
        spelling, by telling clients to send the instants THEIR user means.
        Every other test in this file is written in UTC, which is exactly why
        the whole suite stayed green over it.
        """
        from datetime import timedelta

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
        # 23:00 UTC — late enough that a +05:30 reading lands on the next day.
        created = datetime(2026, 9, 10, 23, 0, tzinfo=timezone.utc)
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=user_id,
            enterprise_id=f"ent_{uuid4().hex[:8]}",
            title="Late evening",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=created,
            updated_at=created,
        )
        object.__setattr__(case, "current_turn", 1)
        await repo.save(case)

        utc_form = datetime(2026, 9, 10, 18, 30, tzinfo=timezone.utc)
        ist_form = datetime(
            2026, 9, 11, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
        assert utc_form == ist_form  # the same moment, written two ways

        _, via_utc = await repo.list(user_id=user_id, created_after=utc_form)
        _, via_ist = await repo.list(user_id=user_id, created_after=ist_form)
        assert via_utc == 1
        assert via_ist == 1, (
            "the same instant written with a non-UTC offset must match the same "
            "rows — a lexicographic TEXT compare against the stored value does not"
        )

        # The upper bound too, and in the direction that EXCLUDES: a window
        # ending at this instant must contain nothing, in either spelling.
        _, before_utc = await repo.list(user_id=user_id, created_before=utc_form)
        _, before_ist = await repo.list(user_id=user_id, created_before=ist_form)
        assert before_utc == 0
        assert before_ist == 0

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
                # turn_number is required: the repository will not invent which
                # turn a message belongs to (#1418).
                "turn_number": 1,
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


@pytest.mark.asyncio
@pytest.mark.integration
class TestKBContextRoundTrip:
    """``case.kb_context`` must survive save → get, or the KB push is inert.

    The push channel (fm#1360) writes matched runbooks to ``case.kb_context``
    from ``MilestoneEngine._prefetch_kb_context``. Both of its triggers fire
    during RESPONSE APPLICATION — after this turn's prompt was already built —
    so the only prompt the pre-fetched runbooks can ever reach belongs to a
    LATER turn, which loads the case back from this repository. A field dropped
    at save is therefore a field the model never sees, on any turn, ever.

    Read back through a SEPARATE session for the same reason the upload tests
    above are: the writing session can see its own uncommitted state, so a
    same-session read would stay green against a repository that never
    persisted anything.
    """

    def _fresh_session(self, engine):
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
            title="KB push round trip",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    async def test_prefetched_runbooks_survive_the_turn_boundary(
        self, sqlite_session, sqlite_engine
    ):
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        case_id = f"case_{uuid4().hex[:12]}"
        case = self._case(case_id)
        case.kb_context = [
            {
                "title": "ENOSPC triage on a full root volume",
                "summary": "Look for deleted-but-open descriptors with lsof +L1",
                "score": 0.91,
                "type": "runbook",
                "parent_document_id": "rb_enospc_triage",
                "trigger": "symptom",
            }
        ]
        await SQLiteCaseRepository(sqlite_session).save(case)

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await SQLiteCaseRepository(other).get(case_id)

        assert reloaded is not None
        assert reloaded.kb_context, (
            "the pre-fetched runbooks were dropped at save; the push can never "
            "reach a prompt"
        )
        entry = reloaded.kb_context[0]
        # Field by field: a blob that round-trips as ``[{}]`` would satisfy a
        # truthiness check while carrying nothing the prompt or the citation
        # list can use.
        assert entry["parent_document_id"] == "rb_enospc_triage"
        assert entry["title"] == "ENOSPC triage on a full root volume"
        assert entry["score"] == 0.91
        assert entry["trigger"] == "symptom"

    async def test_a_case_with_no_prefetch_reloads_as_none(
        self, sqlite_session, sqlite_engine
    ):
        """The empty case, so the writer cannot pass by storing a placeholder."""
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        case_id = f"case_{uuid4().hex[:12]}"
        await SQLiteCaseRepository(sqlite_session).save(self._case(case_id))

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await SQLiteCaseRepository(other).get(case_id)

        assert reloaded is not None
        assert not reloaded.kb_context


@pytest.mark.asyncio
@pytest.mark.integration
class TestMessageRowNormalisation:
    """What the repository completes, and what it refuses to invent (#1418).

    The defect: ``case_messages`` has two writers, and handed a dict with no
    ``message_id`` the aggregate save did ``continue`` — reported success and
    dropped the transcript line, while ``add_message`` minted an id and wrote
    it. The silence was the bug.

    The rule the fix installs is **stamp only what you witnessed**. A writer
    supplies a value only when its own call is the event that produced it:

    - ``message_id`` is minted by both. It is opaque and synthetic on every
      path, so inventing it asserts nothing that can be wrong.
    - ``created_at`` is stamped by ``add_message`` (that call IS the write, so
      "now" is the fact) and REFUSED by the aggregate save, which replays a
      list assembled at moments it never saw. Guessing there was measured
      placing a row appended FIRST after one appended SECOND.
    - ``turn_number`` is invented by neither. It used to default to ``0`` in
      one writer and to the row's INDEX IN THE LIST in the other.
    - ``content`` is checked up front so a blank one is named, instead of
      reaching the CHECK constraint and aborting the whole aggregate save.

    Read back through a SEPARATE session throughout: the writing session sees
    its own uncommitted state, so a same-session read stays green against a
    repository that never persisted anything.
    """

    def _fresh_session(self, engine):
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
            title="message row normalisation",
            state=CaseState.INQUIRY,
            inquiry=InquiryData(),
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _row(self, content, turn=1, role="user", **over):
        """A complete row, as every production writer builds one."""
        row = {
            "role": role,
            "content": content,
            "turn_number": turn,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(over)
        return row

    def _repo(self, session):
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        return SQLiteCaseRepository(session)

    # ---------- what it completes ----------

    async def test_an_id_less_row_survives_save(self, sqlite_session, sqlite_engine):
        """The regression itself: no id, but a real created_at and turn."""
        case_id = f"case_{uuid4().hex[:12]}"
        case = self._case(case_id)
        case.messages.append(self._row("disk full on /var"))
        await self._repo(sqlite_session).save(case)

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await self._repo(other).get(case_id)

        assert reloaded is not None
        assert [m["content"] for m in reloaded.messages] == ["disk full on /var"]
        assert reloaded.messages[0]["message_id"]

    async def test_the_minted_id_is_written_back_to_the_caller(
        self, sqlite_session, sqlite_engine
    ):
        """The id is the ``ON CONFLICT`` target, so the caller's list must
        carry it — otherwise the next save mints a second one and INSERTs a
        duplicate instead of conflicting onto the existing row."""
        repo = self._repo(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        case = self._case(case_id)
        case.messages.append(self._row("same line"))

        await repo.save(case)
        minted = case.messages[0]["message_id"]
        assert minted

        await repo.save(case)

        async with self._fresh_session(sqlite_engine) as other:
            rows = await other.execute(
                text("SELECT message_id FROM case_messages WHERE case_id = :c"),
                {"c": case_id},
            )
            ids = [r[0] for r in rows.fetchall()]
        assert ids == [minted], f"expected one row conflicting onto itself, got {ids}"

    async def test_a_row_that_brings_its_own_id_keeps_it(
        self, sqlite_session, sqlite_engine
    ):
        """Positive control: completing must not overwrite what was supplied."""
        case_id = f"case_{uuid4().hex[:12]}"
        supplied = f"msg_{uuid4().hex[:12]}"
        stamp = "2026-06-13T10:15:30.123456+00:00"
        case = self._case(case_id)
        case.messages.append(
            self._row("brought its own", message_id=supplied, created_at=stamp)
        )
        await self._repo(sqlite_session).save(case)

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await self._repo(other).get(case_id)

        assert reloaded is not None
        assert [m["message_id"] for m in reloaded.messages] == [supplied]
        assert reloaded.messages[0]["created_at"] == stamp

    @pytest.mark.parametrize("falsy", [None, ""], ids=["none", "empty-string"])
    async def test_both_writers_agree_on_a_falsy_id(
        self, sqlite_session, sqlite_engine, falsy
    ):
        """Falsy is absent, for both spellings and both writers.

        Testing key-PRESENCE instead would leave them disagreeing: ``None``
        made ``add_message`` raise NOT NULL while the save minted, and ``""``
        wrote a row whose PRIMARY KEY was the empty string — which can never be
        conflicted onto, so every later save re-minted and INSERTed the line
        again, unboundedly.
        """
        repo = self._repo(sqlite_session)

        case_a = self._case(f"case_{uuid4().hex[:12]}")
        await repo.save(case_a)
        assert await repo.add_message(case_a.case_id, self._row("A", message_id=falsy))

        case_b = self._case(f"case_{uuid4().hex[:12]}")
        case_b.messages.append(self._row("B", message_id=falsy))
        await repo.save(case_b)
        await repo.save(case_b)  # a stored falsy id would re-mint and duplicate

        async with self._fresh_session(sqlite_engine) as other:
            other_repo = self._repo(other)
            a = await other_repo.get(case_a.case_id)
            b = await other_repo.get(case_b.case_id)

        assert [m["content"] for m in a.messages] == ["A"]
        assert [m["content"] for m in b.messages] == ["B"]
        assert a.messages[0]["message_id"] and b.messages[0]["message_id"]

    # ---------- what it refuses to invent ----------

    async def test_the_save_refuses_a_row_with_no_created_at(self, sqlite_session):
        """The inversion, refused instead of guessed.

        Stamping "now" here put a row appended FIRST after one appended
        SECOND, because the second carried its own append-time stamp. The
        repository cannot know when a message happened, so it says so.
        """
        case = self._case(f"case_{uuid4().hex[:12]}")
        case.messages.append(self._row("1. appended FIRST", created_at=None))
        case.messages.append(self._row("2. appended SECOND"))

        with pytest.raises(Exception, match="cannot know when a message was created"):
            await self._repo(sqlite_session).save(case)

    async def test_the_save_refuses_a_row_with_no_turn_number(self, sqlite_session):
        """``turn_number`` used to be the row's INDEX IN THE LIST here and
        ``0`` in ``add_message``. Anchors and ``ix_case_messages_case_turn``
        key on it, so an invented value is worse than a refusal."""
        case = self._case(f"case_{uuid4().hex[:12]}")
        case.messages.append(self._row("no turn", turn=None))

        with pytest.raises(Exception, match="does not know which turn"):
            await self._repo(sqlite_session).save(case)

    async def test_a_blank_content_row_is_named_not_left_to_the_constraint(
        self, sqlite_session, sqlite_engine
    ):
        """Blank content aborts the aggregate save either way — this makes it
        diagnosable, and keeps the abort clean by validating the whole list
        before any SQL runs."""
        case_id = f"case_{uuid4().hex[:12]}"
        case = self._case(case_id)
        case.messages.append(self._row("a real line"))
        case.messages.append(self._row("   "))

        with pytest.raises(Exception, match="content is blank"):
            await self._repo(sqlite_session).save(case)

        # Nothing partial: the case is not half-written either.
        async with self._fresh_session(sqlite_engine) as other:
            assert await self._repo(other).get(case_id) is None

    # ---------- what it canonicalises ----------

    async def test_a_datetime_timestamp_sorts_in_place_not_first(
        self, sqlite_session, sqlite_engine
    ):
        """The alias must be canonicalised, not merely honoured.

        SQLite compares this column as TEXT, and ``str(datetime)`` uses a SPACE
        separator — ``'2026-09-15 04:47'`` sorts before ``'2026-09-15T04:47'``
        because ``' '`` (0x20) precedes ``'T'`` (0x54). A caller passing
        ``timestamp`` as a ``datetime`` (which
        tests/integration/test_case_repository_integration.py does) therefore
        put its row at the FRONT of the transcript. Read-side repair cannot fix
        it: ``_load_messages`` normalises the separator, but ORDER BY has
        already run.
        """
        repo = self._repo(sqlite_session)
        case_id = f"case_{uuid4().hex[:12]}"
        case = self._case(case_id)
        case.messages.append(self._row("1. live"))
        case.messages.append(self._row("2. live"))
        await repo.save(case)

        assert await repo.add_message(
            case_id,
            {
                "role": "user",
                "content": "3. alias-datetime LAST",
                "turn_number": 2,
                "timestamp": datetime.now(timezone.utc),
            },
        )

        async with self._fresh_session(sqlite_engine) as other:
            reloaded = await self._repo(other).get(case_id)

        assert [m["content"] for m in reloaded.messages] == [
            "1. live",
            "2. live",
            "3. alias-datetime LAST",
        ]

    async def test_the_timestamp_alias_is_honoured_by_both_writers(
        self, sqlite_session, sqlite_engine
    ):
        """``timestamp`` used to be read only by PostgreSQL's ``add_message``,
        so the same dict was stamped at its supplied time by one writer and at
        ``now()`` by the other, and SQLite honoured it nowhere."""
        stamp = "2020-01-02T03:04:05+00:00"
        repo = self._repo(sqlite_session)

        case_a = self._case(f"case_{uuid4().hex[:12]}")
        await repo.save(case_a)
        assert await repo.add_message(
            case_a.case_id,
            {"role": "user", "content": "A", "turn_number": 1, "timestamp": stamp},
        )

        case_b = self._case(f"case_{uuid4().hex[:12]}")
        case_b.messages.append(
            {"role": "user", "content": "B", "turn_number": 1, "timestamp": stamp}
        )
        await repo.save(case_b)

        async with self._fresh_session(sqlite_engine) as other:
            other_repo = self._repo(other)
            a = await other_repo.get(case_a.case_id)
            b = await other_repo.get(case_b.case_id)

        assert a.messages[0]["created_at"] == stamp
        assert b.messages[0]["created_at"] == stamp

    # ---------- what the two writers must answer identically ----------

    async def test_add_message_does_not_stamp_the_caller_dict(self, sqlite_session):
        """``add_message`` completes a COPY: it does a plain INSERT with no
        ``ON CONFLICT``, so nothing needs writing back, and stamping the
        caller's dict would carry the first call's id into a reused template
        dict, where it hits the primary key with no recovery."""
        repo = self._repo(sqlite_session)
        case = self._case(f"case_{uuid4().hex[:12]}")
        await repo.save(case)

        template = {"role": "user", "content": "reused template", "turn_number": 1}
        assert await repo.add_message(case.case_id, template)
        assert "message_id" not in template
        assert "created_at" not in template
        assert await repo.add_message(case.case_id, template)

        rows = await sqlite_session.execute(
            text("SELECT message_id FROM case_messages WHERE case_id = :c"),
            {"c": case.case_id},
        )
        ids = [r[0] for r in rows.fetchall()]
        assert len(ids) == 2 and len(set(ids)) == 2

    async def test_both_writers_refuse_a_missing_turn_number(self, sqlite_session):
        """The third invented field, and the one with real consequences: one
        writer defaulted it to ``0``, the other to the row's list index, so the
        same turnless dict landed on two different turns depending on which
        writer took it."""
        repo = self._repo(sqlite_session)
        case = self._case(f"case_{uuid4().hex[:12]}")
        await repo.save(case)

        turnless = {
            "role": "user",
            "content": "no turn",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with pytest.raises(Exception, match="does not know which turn"):
            await repo.add_message(case.case_id, dict(turnless))

        case.messages.append(dict(turnless))
        with pytest.raises(Exception, match="does not know which turn"):
            await repo.save(case)


@pytest.mark.asyncio
@pytest.mark.integration
class TestSearchStateReachesTheWhereClause:
    """``search(state=...)`` is a SQL predicate, not a Python post-filter (#1416).

    ``CaseSearchRequest.state`` was declared and published for as long as the
    endpoint has existed, and ``ICaseRepository.search`` had no parameter for it
    to reach — so ``POST /cases/search`` answered 200 with every state. The
    companion unit test
    (``tests/unit/modules/case/test_search_applies_declared_state_1416.py``)
    makes the same assertions through ``CaseService`` against the in-memory
    repository; this one makes them against real SQL, where the ``LIMIT`` is
    applied by the database and a predicate outside the ``WHERE`` clause is
    therefore a predicate applied to an already-shortened list.
    """

    TOKEN = "widget"
    OWNER = "sqlite_state_owner"

    def _case(self, index: int, *, title: str, state):
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseState,
            DocumentationData,
            InquiryData,
            InvestigationProgress,
        )

        inquiry = InquiryData()
        if state is CaseState.INVESTIGATING:
            # Case's own validators: INVESTIGATING needs a confirmed problem
            # statement and a commitment to investigate.
            inquiry = InquiryData(
                proposed_problem_statement="seeded problem statement",
                problem_statement_confirmed=True,
                decided_to_investigate=True,
            )
        now = datetime.now(timezone.utc)
        return Case(
            case_id=f"case_{index:012d}",
            user_id=self.OWNER,
            enterprise_id="sqlite_state_ent",
            title=title,
            description="seeded for the search-state predicate",
            state=state,
            inquiry=inquiry,
            documentation=DocumentationData(),
            progress=InvestigationProgress(),
            created_at=now,
            updated_at=now,
        )

    async def _seed(self, session):
        """Four cases, all matching ``widget``, two per state.

        ``updated_at`` is stamped AFTERWARDS with an explicit UPDATE, because
        ``save`` overwrites it with ``now()`` — and the ordering is the whole
        point: ``search`` orders by ``updated_at DESC``, so the two INQUIRY rows
        are made the newest. A ``LIMIT 2`` therefore selects two INQUIRY rows,
        and a search for INVESTIGATING can only find anything if the state
        reached the WHERE clause.
        """
        from sqlalchemy import text

        from faultmaven.modules.case.domain.models import CaseState
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = SQLiteCaseRepository(session)
        seeds = [
            (1, "alpha widget", CaseState.INQUIRY),
            (2, "beta widget", CaseState.INQUIRY),
            (3, "gamma widget", CaseState.INVESTIGATING),
            (4, "delta widget", CaseState.INVESTIGATING),
        ]
        for index, title, state in seeds:
            await repo.save(self._case(index, title=title, state=state))

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, _, _ in seeds:
            await session.execute(
                # ``created_at`` moves with it: Case refuses a row whose
                # creation is later than its last update.
                text(
                    "UPDATE cases SET updated_at = :ts, created_at = :ts "
                    "WHERE case_id = :cid"
                ),
                {
                    # Newest first: case 1 (INQUIRY) is the newest, case 4
                    # (INVESTIGATING) the oldest.
                    "ts": base - timedelta(days=index),
                    "cid": f"case_{index:012d}",
                },
            )
        await session.commit()
        return repo

    async def test_state_narrows_the_result(self, sqlite_session):
        from faultmaven.modules.case.domain.models import CaseState

        repo = await self._seed(sqlite_session)

        everything, _ = await repo.search(self.TOKEN, user_id=self.OWNER)
        investigating, _ = await repo.search(
            self.TOKEN, user_id=self.OWNER, state=CaseState.INVESTIGATING
        )
        inquiry, _ = await repo.search(
            self.TOKEN, user_id=self.OWNER, state=CaseState.INQUIRY
        )

        assert {c.case_id for c in everything} == {
            f"case_{i:012d}" for i in (1, 2, 3, 4)
        }
        assert {c.case_id for c in investigating} == {
            "case_000000000003",
            "case_000000000004",
        }
        assert {c.case_id for c in inquiry} == {
            "case_000000000001",
            "case_000000000002",
        }

    async def test_the_ordering_this_test_relies_on(self, sqlite_session):
        """Captured, not assumed: the two rows a ``LIMIT 2`` takes are INQUIRY.

        Without this, the test below could keep passing while proving nothing —
        if the seeded ordering ever changed so that an INVESTIGATING row landed
        in the top two, a post-LIMIT filter would find it and look correct.
        """
        from faultmaven.modules.case.domain.models import CaseState

        repo = await self._seed(sqlite_session)

        top_two, _ = await repo.search(self.TOKEN, user_id=self.OWNER, limit=2)

        assert [c.state for c in top_two] == [CaseState.INQUIRY, CaseState.INQUIRY]

    async def test_state_survives_a_limit_the_other_state_would_fill(
        self, sqlite_session
    ):
        """``LIMIT 2`` on a corpus whose two newest rows are both INQUIRY.

        In the WHERE clause this returns the two INVESTIGATING cases. Applied
        after the query it returns nothing — SQL has already handed back two
        INQUIRY rows and the filter deletes both, so the endpoint answers 200
        with an empty list about a corpus that holds two matches.
        """
        from faultmaven.modules.case.domain.models import CaseState

        repo = await self._seed(sqlite_session)

        result, _ = await repo.search(
            self.TOKEN, user_id=self.OWNER, state=CaseState.INVESTIGATING, limit=2
        )

        assert {c.case_id for c in result} == {
            "case_000000000003",
            "case_000000000004",
        }

    async def test_state_ands_with_the_owner_scope(self, sqlite_session):
        """A new predicate composes with the existing ones; it does not replace
        them. A stranger's INVESTIGATING case stays invisible."""
        from faultmaven.modules.case.domain.models import CaseState
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = await self._seed(sqlite_session)
        stranger = self._case(5, title="epsilon widget", state=CaseState.INVESTIGATING)
        stranger.user_id = "a-stranger"
        await SQLiteCaseRepository(sqlite_session).save(stranger)

        result, _ = await repo.search(
            self.TOKEN, user_id=self.OWNER, state=CaseState.INVESTIGATING
        )

        assert {c.case_id for c in result} == {
            "case_000000000003",
            "case_000000000004",
        }

    async def test_state_does_not_replace_the_text_predicate(self, sqlite_session):
        from faultmaven.modules.case.domain.models import CaseState

        repo = await self._seed(sqlite_session)

        result, _ = await repo.search(
            "gadget", user_id=self.OWNER, state=CaseState.INVESTIGATING
        )

        assert result == []

    async def test_the_total_is_the_match_count_not_the_page_length(
        self, sqlite_session
    ):
        """``search`` returns ``(page, total_count)`` and the total is a TOTAL.

        Discriminating on purpose: four matches under ``limit=2``. This
        repository returned ``len(cases)`` — the page length wearing the name of
        a total — until this change, which is indistinguishable from a true
        count on any corpus smaller than the limit. Nothing reads the value
        today (``search_cases`` discards it, and the route is
        ``response_model=List[CaseSummary]``), which is exactly how it stayed
        wrong.
        """
        repo = await self._seed(sqlite_session)

        page, total = await repo.search(self.TOKEN, user_id=self.OWNER, limit=2)

        assert len(page) == 2
        assert total == 4

    async def test_the_total_moves_with_the_state_predicate(self, sqlite_session):
        """The count comes from the same WHERE clause as the page."""
        from faultmaven.modules.case.domain.models import CaseState

        repo = await self._seed(sqlite_session)

        page, total = await repo.search(
            self.TOKEN, user_id=self.OWNER, state=CaseState.INVESTIGATING
        )

        assert len(page) == 2
        assert total == 2
