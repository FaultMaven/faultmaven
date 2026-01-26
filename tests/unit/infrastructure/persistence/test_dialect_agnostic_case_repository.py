"""Unit tests for database dialect-agnostic case repository.

Tests that PostgreSQLHybridCaseRepository properly detects database dialect
and generates appropriate SQL for both SQLite and PostgreSQL.
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import Dialect

from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    ConsultingData,
    InvestigationProgress,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)


@pytest.mark.asyncio
async def test_upsert_case_detects_sqlite_dialect():
    """Test that _upsert_case_record detects SQLite and generates compatible SQL."""
    # Arrange - Create mock session with SQLite dialect
    mock_session = AsyncMock(spec=AsyncSession)
    mock_bind = MagicMock()
    mock_dialect = MagicMock(spec=Dialect)
    mock_dialect.name = "sqlite"
    mock_bind.dialect = mock_dialect
    mock_session.bind = mock_bind
    mock_session.execute = AsyncMock()

    repository = PostgreSQLHybridCaseRepository(mock_session)

    # Create test case with valid UUIDs
    test_case = Case(
        case_id=f"case_{str(uuid4()).replace('-', '')[:12]}",
        user_id=str(uuid4()),
        organization_id=str(uuid4()),
        title="Test Case",
        status=CaseStatus.CONSULTING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        consulting=ConsultingData(
            initial_question="Test question",
            clarifications=[],
            user_context={},
        ),
        progress=InvestigationProgress(
            total_turns=0,
            investigation_depth=0,
            current_phase="consulting",
            phase_confidence=0.0,
        ),
    )

    # Act
    await repository._upsert_case_record(test_case)

    # Assert - Verify SQL does NOT contain PostgreSQL-specific ::jsonb casts
    mock_session.execute.assert_called_once()
    call_args = mock_session.execute.call_args
    sql_text = str(call_args[0][0])

    # SQLite version should NOT have ::jsonb type casts
    assert (
        "::jsonb" not in sql_text
    ), "SQLite SQL should not contain PostgreSQL ::jsonb type casts"
    assert (
        ":consulting, :problem_verification" in sql_text
        or "consulting, problem_verification" in sql_text
    )


@pytest.mark.asyncio
async def test_upsert_case_detects_postgresql_dialect():
    """Test that _upsert_case_record detects PostgreSQL and generates JSONB type casts."""
    # Arrange - Create mock session with PostgreSQL dialect
    mock_session = AsyncMock(spec=AsyncSession)
    mock_bind = MagicMock()
    mock_dialect = MagicMock(spec=Dialect)
    mock_dialect.name = "postgresql"
    mock_bind.dialect = mock_dialect
    mock_session.bind = mock_bind
    mock_session.execute = AsyncMock()

    repository = PostgreSQLHybridCaseRepository(mock_session)

    # Create test case with valid UUIDs
    test_case = Case(
        case_id=f"case_{str(uuid4()).replace('-', '')[:12]}",
        user_id=str(uuid4()),
        organization_id=str(uuid4()),
        title="Test Case",
        status=CaseStatus.CONSULTING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        consulting=ConsultingData(
            initial_question="Test question",
            clarifications=[],
            user_context={},
        ),
        progress=InvestigationProgress(
            total_turns=0,
            investigation_depth=0,
            current_phase="consulting",
            phase_confidence=0.0,
        ),
    )

    # Act
    await repository._upsert_case_record(test_case)

    # Assert - Verify SQL DOES contain PostgreSQL-specific ::jsonb casts
    mock_session.execute.assert_called_once()
    call_args = mock_session.execute.call_args
    sql_text = str(call_args[0][0])

    # PostgreSQL version SHOULD have ::jsonb type casts
    assert (
        "::jsonb" in sql_text
    ), "PostgreSQL SQL should contain ::jsonb type casts for optimal performance"


@pytest.mark.asyncio
async def test_upsert_case_defaults_to_sqlite_when_no_bind():
    """Test that _upsert_case_record defaults to SQLite SQL when session.bind is None."""
    # Arrange - Create mock session with no bind
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.bind = None
    mock_session.execute = AsyncMock()

    repository = PostgreSQLHybridCaseRepository(mock_session)

    # Create test case with valid UUIDs
    test_case = Case(
        case_id=f"case_{str(uuid4()).replace('-', '')[:12]}",
        user_id=str(uuid4()),
        organization_id=str(uuid4()),
        title="Test Case",
        status=CaseStatus.CONSULTING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        consulting=ConsultingData(
            initial_question="Test question",
            clarifications=[],
            user_context={},
        ),
        progress=InvestigationProgress(
            total_turns=0,
            investigation_depth=0,
            current_phase="consulting",
            phase_confidence=0.0,
        ),
    )

    # Act
    await repository._upsert_case_record(test_case)

    # Assert - Should default to SQLite (no ::jsonb)
    mock_session.execute.assert_called_once()
    call_args = mock_session.execute.call_args
    sql_text = str(call_args[0][0])

    # Default should be SQLite (no type casts)
    assert (
        "::jsonb" not in sql_text
    ), "Should default to SQLite SQL when no bind available"
