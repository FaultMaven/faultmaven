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
import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP,
            resolved_at TIMESTAMP,
            closed_at TIMESTAMP,
            consulting TEXT,
            problem_verification TEXT,
            working_conclusion TEXT,
            root_cause_conclusion TEXT,
            path_selection TEXT,
            degraded_mode TEXT,
            escalation_state TEXT,
            documentation TEXT,
            progress TEXT,
            metadata TEXT
        )
    """))

    # Create evidence table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            category TEXT,
            summary TEXT,
            preprocessed_content TEXT,
            content_ref TEXT,
            file_size INTEGER,
            filename TEXT,
            upload_timestamp TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create hypotheses table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'proposed',
            confidence_score REAL,
            supporting_evidence_ids TEXT,
            validation_result TEXT,
            validation_timestamp TIMESTAMP,
            proposed_at TIMESTAMP,
            updated_at TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create solutions table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS solutions (
            solution_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'proposed',
            implementation_steps TEXT,
            risk_level TEXT,
            estimated_effort TEXT,
            verification_result TEXT,
            verification_timestamp TIMESTAMP,
            proposed_at TIMESTAMP,
            implemented_at TIMESTAMP,
            updated_at TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create uploaded_files table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            file_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            filename TEXT,
            size_bytes INTEGER,
            data_type TEXT,
            uploaded_at_turn INTEGER,
            uploaded_at TIMESTAMP,
            source_type TEXT,
            content_ref TEXT,
            preprocessing_summary TEXT,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create case_messages table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS case_messages (
            message_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create case_status_transitions table
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS case_status_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT,
            transitioned_at TIMESTAMP,
            metadata TEXT,
            FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE
        )
    """))

    # Create evidence_artifacts table (used by _load_evidence_for_case)
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS evidence_artifacts (
            evidence_id TEXT PRIMARY KEY,
            case_id TEXT,
            user_id TEXT,
            organization_id TEXT,
            original_filename TEXT,
            stored_filename TEXT,
            file_path TEXT,
            evidence_type TEXT,
            mime_type TEXT,
            file_size INTEGER,
            storage_backend TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            metadata TEXT,
            description TEXT,
            is_primary INTEGER DEFAULT 0,
            tags TEXT
        )
    """))

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
            [SQL: INSERT INTO cases (...) VALUES (..., :consulting::jsonb, ...)]
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        # Create repository with real SQLite session
        repo = SQLiteCaseRepository(sqlite_session)

        # Create test case
        case_id = f"case_{uuid4().hex[:12]}"
        test_case = Case(
            case_id=case_id,
            user_id="test_user_123",
            title="Test Case for SQLite Compatibility",
            status=CaseStatus.OPEN,
            consulting=ConsultingData(initial_description="Test description"),
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
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create and save case
        case_id = f"case_{uuid4().hex[:12]}"
        test_case = Case(
            case_id=case_id,
            user_id="test_user_456",
            title="Retrieval Test Case",
            status=CaseStatus.OPEN,
            consulting=ConsultingData(initial_description="Description for retrieval"),
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
        assert retrieved.consulting.initial_description == "Description for retrieval"

    async def test_case_search_sqlite_compatible(self, sqlite_session):
        """Test that search works with SQLite LIKE (no to_tsvector/ts_rank).

        PostgreSQL uses to_tsvector/ts_rank for full-text search.
        SQLite uses LIKE pattern matching instead.
        """
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
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
                case_id=f"search_case_{i}_{uuid4().hex[:8]}",
                user_id="search_user",
                title=title,
                status=CaseStatus.OPEN,
                consulting=ConsultingData(),
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
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create test cases
        user_id = f"list_user_{uuid4().hex[:8]}"
        for i in range(3):
            case = Case(
                case_id=f"list_case_{i}_{uuid4().hex[:8]}",
                user_id=user_id,
                title=f"List Test Case {i}",
                status=CaseStatus.OPEN,
                consulting=ConsultingData(),
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
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create case first
        case_id = f"msg_case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="msg_user",
            title="Message Test Case",
            status=CaseStatus.OPEN,
            consulting=ConsultingData(),
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
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create case
        case_id = f"analytics_case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="analytics_user",
            title="Analytics Test Case",
            status=CaseStatus.OPEN,
            consulting=ConsultingData(),
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
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )
        from faultmaven.modules.case.domain.models import (
            Case,
            CaseStatus,
            ConsultingData,
            DocumentationData,
            InvestigationProgress,
        )

        repo = SQLiteCaseRepository(sqlite_session)

        # Create and save case
        case_id = f"delete_case_{uuid4().hex[:12]}"
        case = Case(
            case_id=case_id,
            user_id="delete_user",
            title="Delete Test Case",
            status=CaseStatus.OPEN,
            consulting=ConsultingData(),
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


@pytest.mark.asyncio
@pytest.mark.integration
class TestDialectDetection:
    """Test that SessionlessCaseRepository correctly detects SQLite dialect."""

    async def test_dialect_detection_selects_sqlite_repo(self, sqlite_session):
        """Test that _get_repository_for_session returns SQLiteCaseRepository for SQLite."""
        from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
            _get_repository_for_session,
        )
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        repo = _get_repository_for_session(sqlite_session)

        # Should return SQLiteCaseRepository for SQLite dialect
        assert isinstance(repo, SQLiteCaseRepository)
