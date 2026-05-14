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

import json
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
    """Create SQLAlchemy async engine for SQLite."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{sqlite_db_path}",
        echo=False,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def sqlite_session(sqlite_engine):
    """Create SQLAlchemy async session for SQLite."""
    async_session_factory = sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        # Create minimal schema for testing
        await create_test_schema(session)
        yield session


async def create_test_schema(session: AsyncSession):
    """Create minimal schema required for case repository testing."""
    # Create cases table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            organization_id TEXT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            investigation_strategy TEXT DEFAULT 'post_mortem',
            status TEXT NOT NULL DEFAULT 'inquiry',
            current_turn INTEGER NOT NULL DEFAULT 0,
            turns_without_progress INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP,
            resolved_at TIMESTAMP,
            closed_at TIMESTAMP,
            -- Phase 6 Tier 1 column addition (storage redesign 2026-04).
            closure_reason TEXT,
            inquiry TEXT,
            problem_verification TEXT,
            working_conclusion TEXT,
            root_cause_conclusion TEXT,
            path_selection TEXT,
            escalation_state TEXT,
            documentation TEXT,
            progress TEXT,
            metadata TEXT,
            is_archived INTEGER NOT NULL DEFAULT 0,
            archived_at TIMESTAMP,
            -- OCC token (2026-04-24 hierarchy consolidation).
            version INTEGER NOT NULL DEFAULT 1
        )
    """))

    # Create evidence table (post-009: includes primary_purpose, analysis,
    # processing_mode, advances_milestones, collected_by). Schema mirrors
    # what migration 009 produces; see EvidenceModel for the canonical shape.
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            category TEXT,
            summary TEXT,
            preprocessed_content TEXT,
            content_ref TEXT,
            file_size BIGINT NOT NULL DEFAULT 0,
            filename TEXT,
            upload_timestamp TIMESTAMP,
            metadata TEXT,
            source_type TEXT,
            content_hash TEXT,
            collected_at_turn INTEGER,
            source_file_id TEXT,
            extract TEXT,
            -- Phase 6 Tier 1 column additions (storage redesign 2026-04).
            form TEXT NOT NULL DEFAULT 'text',
            is_primary INTEGER NOT NULL DEFAULT 0,
            content_type TEXT,
            reliability_score REAL,
            tags TEXT,
            vectorized INTEGER NOT NULL DEFAULT 0,
            coverage_start_ts TIMESTAMP,
            coverage_end_ts TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            -- Migration 009 columns.
            primary_purpose TEXT NOT NULL DEFAULT 'legacy',
            analysis TEXT,
            processing_mode TEXT,
            advances_milestones TEXT,
            collected_by TEXT NOT NULL DEFAULT 'system',
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create hypotheses table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            statement TEXT,
            status TEXT DEFAULT 'captured',
            likelihood REAL,
            initial_likelihood REAL,
            generated_at_turn INTEGER DEFAULT 0,
            last_updated_turn INTEGER DEFAULT 0,
            last_progress_at_turn INTEGER DEFAULT 0,
            iterations_without_progress INTEGER DEFAULT 0,
            category TEXT,
            generation_mode TEXT,
            rationale TEXT,
            retirement_reason TEXT,
            refutation_reason TEXT,
            evidence_links TEXT,
            tested_at TIMESTAMP,
            concluded_at TIMESTAMP,
            proposed_at TIMESTAMP,
            updated_at TIMESTAMP,
            metadata TEXT,
            created_by TEXT,
            updated_by TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create hypothesis_evidence junction table (replaces hypotheses.evidence_links JSON blob)
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS hypothesis_evidence (
            hypothesis_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            relationship_type TEXT NOT NULL,
            confidence REAL,
            linked_at_turn INTEGER,
            linked_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (hypothesis_id, evidence_id),
            FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE
        )
    """))

    # Create solutions table (post-009 schema: dropped created_by/updated_by;
    # renamed implemented_at -> applied_at, verification_timestamp -> verified_at;
    # added proposed_by/applied_by/verification_method/verification_evidence_id/effectiveness).
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS solutions (
            solution_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            solution_type TEXT DEFAULT 'other',
            title TEXT DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            status TEXT DEFAULT 'proposed',
            immediate_action TEXT,
            longterm_fix TEXT,
            implementation_steps TEXT,
            commands TEXT,
            risks TEXT,
            risk_level TEXT,
            estimated_effort TEXT,
            proposed_by TEXT NOT NULL DEFAULT 'agent',
            applied_by TEXT,
            verification_method TEXT,
            verification_evidence_id TEXT,
            effectiveness REAL,
            verification_result TEXT,
            verified_at TIMESTAMP,
            proposed_at TIMESTAMP,
            applied_at TIMESTAMP,
            updated_at TIMESTAMP,
            metadata TEXT,
            -- Phase 6 Tier 1 column addition (storage redesign 2026-04).
            hypothesis_id TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE,
            FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id) ON DELETE SET NULL,
            FOREIGN KEY (verification_evidence_id) REFERENCES evidence(evidence_id) ON DELETE SET NULL
        )
    """))

    # Create uploaded_files table (per case-schema.md §4.6, post-010).
    # The five trailing columns (summary, structural_index, data_type,
    # coverage_start_ts, coverage_end_ts) were added by migration 010 to
    # hold the file-level preprocessing artifacts that previously rode
    # on the auto-DOCUMENT Evidence row.
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            file_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            content_type TEXT,
            content_hash TEXT,
            storage_ref TEXT,
            upload_source TEXT NOT NULL DEFAULT 'file_upload',
            uploaded_at_turn INTEGER NOT NULL DEFAULT 0,
            uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            uploaded_by TEXT,
            metadata TEXT DEFAULT '{}',
            summary TEXT,
            structural_index TEXT,
            data_type TEXT,
            coverage_start_ts TIMESTAMP,
            coverage_end_ts TIMESTAMP,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create case_messages table (per case-schema.md §4.7)
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS case_messages (
            message_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            turn_number INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            token_count INTEGER,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create case_actions table (audit trail of case actions)
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS case_actions (
            transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            organization_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT,
            -- Migration 008 (2026-05): triggered_by NOT NULL.
            triggered_by TEXT NOT NULL,
            transitioned_at TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # evidence_artifacts table dropped in storage redesign 2026-04 phase 2
    # (standalone evidence path deletion). Evidence is case-tied only and lives
    # in the existing `evidence` table.

    # Create reports table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            is_current INTEGER DEFAULT 1,
            linked_to_closure INTEGER DEFAULT 0,
            title TEXT,
            content TEXT,
            format TEXT DEFAULT 'markdown',
            generation_status TEXT DEFAULT 'completed',
            generation_time_ms INTEGER,
            metadata TEXT,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            -- Phase 6 Tier 1 column addition (storage redesign 2026-04).
            generated_by TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    await session.commit()


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
            CaseStatus,
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
            organization_id="test_org_123",
            title="Test Case for SQLite Compatibility",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
            organization_id="test_org_456",
            title="Retrieval Test Case",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
                organization_id="search_org",
                title=title,
                status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
        organization_id = f"org_{uuid4().hex[:8]}"
        for i in range(3):
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id,
                organization_id=organization_id,
                title=f"List Test Case {i}",
                status=CaseStatus.INQUIRY,
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

    async def test_message_operations_sqlite_compatible(self, sqlite_session):
        """Test that message operations work with SQLite (no ::jsonb)."""
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
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
            organization_id="msg_org",
            title="Message Test Case",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
            organization_id="analytics_org",
            title="Analytics Test Case",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
            organization_id="delete_org",
            title="Delete Test Case",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
            DocumentationData,
            EvidenceStance,
            Hypothesis,
            HypothesisCategory,
            HypothesisEvidenceLink,
            HypothesisGenerationMode,
            HypothesisStatus,
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
            status=HypothesisStatus.ACTIVE,
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
            organization_id="test_org_evidence_links",
            title="Test Case for Evidence Links Serialization",
            description="Testing hypothesis evidence links serialization with datetime fields",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
            organization_id="00000000-0000-0000-0000-000000000001",
            title="Preprocessing roundtrip case",
            status=CaseStatus.INQUIRY,
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
            CaseStatus,
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
            organization_id="00000000-0000-0000-0000-000000000001",
            title="COALESCE upsert test",
            status=CaseStatus.INQUIRY,
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
