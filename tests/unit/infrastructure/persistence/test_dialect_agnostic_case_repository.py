"""Unit tests for database dialect-agnostic case repository.

Tests that PostgreSQLHybridCaseRepository properly detects database dialect
and generates appropriate SQL for both SQLite and PostgreSQL.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)


def _make_mock_session_with_dialect(dialect_name: str | None) -> AsyncMock:
    """Build an AsyncMock session whose execute() returns a rowcount=1
    result on the first call — simulating a successful OCC UPDATE, so
    `_upsert_case_record` takes the happy path and issues exactly one
    execute() (the UPDATE). That's the only call we need to inspect for
    dialect-specific SQL.
    """
    mock_session = AsyncMock(spec=AsyncSession)

    if dialect_name is None:
        mock_session.bind = None
    else:
        mock_bind = MagicMock()
        mock_dialect = MagicMock(spec=Dialect)
        mock_dialect.name = dialect_name
        mock_bind.dialect = mock_dialect
        mock_session.bind = mock_bind

    # AsyncMock.execute default returns AsyncMock; explicitly set
    # `.rowcount` to 1 on the returned object so the OCC branch takes
    # the "UPDATE succeeded" path without hitting the probe/INSERT.
    execute_result = MagicMock()
    execute_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=execute_result)
    return mock_session


def _make_test_case() -> Case:
    return Case(
        case_id=f"case_{str(uuid4()).replace('-', '')[:12]}",
        user_id=str(uuid4()),
        organization_id=str(uuid4()),
        title="Test Case",
        state=CaseState.INQUIRY,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        inquiry=InquiryData(
            initial_question="Test question",
            clarifications=[],
            user_context={},
        ),
        progress=InvestigationProgress(
            total_turns=0,
            investigation_depth=0,
            current_phase="inquiry",
            phase_confidence=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_upsert_case_detects_sqlite_dialect():
    """_upsert_case_record detects SQLite and generates compatible SQL.

    With the mocked session's UPDATE returning rowcount=1 (simulating a
    happy OCC update), the repository issues exactly one execute call
    whose SQL we can inspect for dialect-specific clauses.
    """
    mock_session = _make_mock_session_with_dialect("sqlite")
    repository = PostgreSQLHybridCaseRepository(mock_session)

    await repository._upsert_case_record(_make_test_case())

    mock_session.execute.assert_called_once()
    sql_text = str(mock_session.execute.call_args[0][0])

    # SQLite branch must not emit PostgreSQL-specific ::jsonb casts.
    assert (
        "::jsonb" not in sql_text
    ), "SQLite SQL should not contain PostgreSQL ::jsonb type casts"
    # Version predicate is the OCC hook — must be present on every
    # UPDATE regardless of dialect.
    assert "version = :expected_version" in sql_text


@pytest.mark.asyncio
async def test_upsert_case_detects_postgresql_dialect():
    """_upsert_case_record emits ::jsonb casts on PostgreSQL."""
    mock_session = _make_mock_session_with_dialect("postgresql")
    repository = PostgreSQLHybridCaseRepository(mock_session)

    await repository._upsert_case_record(_make_test_case())

    mock_session.execute.assert_called_once()
    sql_text = str(mock_session.execute.call_args[0][0])

    assert (
        "::jsonb" in sql_text
    ), "PostgreSQL SQL should contain ::jsonb type casts for optimal performance"
    assert "version = :expected_version" in sql_text


@pytest.mark.asyncio
async def test_upsert_case_defaults_to_sqlite_when_no_bind():
    """_upsert_case_record falls back to SQLite SQL when session.bind is None."""
    mock_session = _make_mock_session_with_dialect(None)
    repository = PostgreSQLHybridCaseRepository(mock_session)

    await repository._upsert_case_record(_make_test_case())

    mock_session.execute.assert_called_once()
    sql_text = str(mock_session.execute.call_args[0][0])

    assert (
        "::jsonb" not in sql_text
    ), "Should default to SQLite SQL when no bind available"
