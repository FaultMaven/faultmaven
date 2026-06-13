"""Round-trip the PostgreSQL case repository against a REAL PostgreSQL.

This is the test that would have caught the ``:name::jsonb`` bind-drop bug
(#441): it executes ``PostgreSQLHybridCaseRepository``'s actual INSERT/UPDATE
SQL — every JSONB and timestamptz cast — against a live PostgreSQL and reads
the rows back. The SQLite and mocked-session suites cannot catch that class
because this repository runs ONLY on PostgreSQL (SQLite uses a different
repo) and the defect lives in how SQLAlchemy/asyncpg bind the casts, which a
mocked session never exercises.

It is skipped unless ``DATABASE_URL`` points at PostgreSQL, so the default
SQLite dev/CI flow is unaffected. CI provides a ``postgres`` service and runs
``alembic upgrade head`` before this suite (see the ``test-postgres`` job in
.github/workflows/ci-cd.yml).

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv/bin/alembic upgrade head
    .venv/bin/pytest tests/integration/test_postgresql_repository_roundtrip.py -v
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
from faultmaven.modules.case.domain.owned_models.report import (
    CaseReport,
    ReportStatus,
    ReportType,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from tests.utils import seed_organizations, seed_users

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


@pytest.fixture
async def pg_engine():
    """Async engine bound to the PostgreSQL under test.

    Assumes the schema already exists (CI runs ``alembic upgrade head``
    first). Verifies the dialect is actually PostgreSQL so this never
    silently runs against the wrong backend.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql", (
        "This suite must run against PostgreSQL; "
        f"got dialect={engine.dialect.name!r}"
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def pg_repo(pg_engine):
    """A PostgreSQLHybridCaseRepository on a real PG session, with the
    case's FK prerequisites (enterprise/org/user) seeded."""
    Session = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with Session() as session:
        # Sanity: the factory-style detection must agree this is PG, or the
        # cast helper would silently emit SQLite-style bare placeholders.
        repo = PostgreSQLHybridCaseRepository(session)
        assert repo._is_pg is True
        yield repo


def _make_case(org_id: str, user_id: str) -> Case:
    """A case whose JSONB columns are all non-trivially populated, so a
    dropped/again-broken cast surfaces as a real read-back mismatch."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id=org_id,
        title="PG round-trip case",
        description="Exercises JSONB + timestamptz casts on real PostgreSQL",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="DB connections time out under load",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )


@pytest.mark.asyncio
async def test_case_save_roundtrip_with_jsonb_columns(pg_repo):
    """save() executes the cases UPDATE/INSERT (8 JSONB casts) plus
    evidence/uploaded_files/messages inserts (more JSONB casts) on real PG,
    then get() reads them back. The ``:name::jsonb`` bug made every one of
    these writes raise 'syntax error at or near ":"'."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])

    case = _make_case(org_id, user_id)
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=2048,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_by=user_id,
            upload_source="file_upload",
            summary="timeouts",
            structural_index="ERROR: timeout",
            data_type="logs",
        )
    )
    case.evidence.append(
        Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="connection timeouts in the log",
            extract="ERROR: timeout after 30s",
            source_type=EvidenceSourceType.LOGS,
            source_file_id=file_id,
            collected_by=user_id,
            collected_at_turn=1,
        )
    )

    saved = await pg_repo.save(case)
    assert saved.version == 1

    fetched = await pg_repo.get(case.case_id)
    assert fetched is not None
    # JSONB round-tripped from the `inquiry` column, not stored as a literal
    # ':inquiry::jsonb' string (which is exactly what the bug produced).
    assert fetched.inquiry.problem_statement_confirmed is True
    assert fetched.inquiry.proposed_problem_statement == (
        "DB connections time out under load"
    )
    assert len(fetched.evidence) == 1
    assert fetched.evidence[0].source_file_id == file_id


@pytest.mark.asyncio
async def test_add_message_roundtrip(pg_repo):
    """add_message() exercises the case_messages metadata JSONB cast."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    ok = await pg_repo.add_message(
        case.case_id,
        {
            "message_id": f"msg_{uuid4().hex[:12]}",
            "turn_number": 1,
            "role": "user",
            "content": "the API returns 500",
            "metadata": {"client": "copilot"},
        },
    )
    assert ok is True
    messages = await pg_repo.get_messages(case.case_id)
    assert any(m.get("content") == "the API returns 500" for m in messages)


@pytest.mark.asyncio
async def test_add_report_roundtrip_with_timestamptz(pg_repo):
    """add_report() exercises the reports metadata (JSONB) AND
    generated_at/updated_at (TIMESTAMPTZ) casts — the timestamptz arm of the
    same bug class."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    report = CaseReport(
        report_id=f"report_{uuid4().hex[:12]}",
        case_id=case.case_id,
        report_type=ReportType.RESOLUTION_SUMMARY,
        title="Resolution summary",
        content="# Summary\n\nFixed the pool size.",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2026-06-12T10:30:00Z",
        generation_time_ms=1500,
        is_current=True,
        version=1,
        linked_to_closure=False,
    )
    saved = await pg_repo.add_report(report)
    assert saved.report_id == report.report_id

    fetched = await pg_repo.get_report(report.report_id)
    assert fetched is not None
    assert fetched.title == "Resolution summary"


@pytest.mark.asyncio
async def test_create_checkpoint_roundtrip_with_timestamptz(pg_repo):
    """create_checkpoint() exercises case_snapshot + metadata (JSONB) AND
    created_at (TIMESTAMPTZ) casts."""
    session = pg_repo.db
    org_id = f"org_{uuid4().hex[:8]}"
    user_id = f"user_{uuid4().hex[:8]}"
    await seed_organizations(session, [org_id])
    await seed_users(session, [user_id])
    case = _make_case(org_id, user_id)
    await pg_repo.save(case)

    from datetime import datetime, timezone

    checkpoint = CaseCheckpoint(
        checkpoint_id=f"{case.case_id}:turn:1",
        case_id=case.case_id,
        turn_number=1,
        case_snapshot={"state": "investigating", "turn": 1},
        snapshot_hash="0" * 64,
        trigger="turn_complete",
        created_at=datetime.now(timezone.utc),
        metadata={"reason": "test"},
    )
    saved = await pg_repo.create_checkpoint(checkpoint)
    assert saved.checkpoint_id == checkpoint.checkpoint_id

    fetched = await pg_repo.get_checkpoint(checkpoint.checkpoint_id)
    assert fetched is not None
    assert fetched.case_snapshot == {"state": "investigating", "turn": 1}
