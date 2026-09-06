"""Case→runbook conversion writes under PostgreSQL RLS (#1143).

The chat-triggered case→runbook flow persists three RLS-tenanted rows — the
synthetic ``uploaded_files`` conversion source, the ``conversion_jobs`` row, and
its ``conversion_drafts``. The policy from migration 018 is ``FOR ALL`` with the
USING expression doubling as the WITH CHECK, so a row stamped with an
``organization_id`` other than the session's ``app.current_enterprise_id`` is
**refused**, not merely hidden.

The bug: with no org supplied, the service stamped a hardcoded single-tenant
sentinel. Under ``TENANT_PROVIDER=multi`` that is nobody's tenant, so every
tenant's conversion died on
``InsufficientPrivilegeError: new row violates row-level security policy for
table "uploaded_files"`` and the user was told "Runbook generation failed, so no
draft was created."

Why this file has to exist at all: SQLite has no RLS, so no standalone test can
fail on it — which is exactly why every rehearsal of this flow passed. The unit
tests beside it (``tests/unit/modules/knowledge/test_conversion_service.py``,
``TestPersistJobOrgStamp``) pin the *stamped value* and do bite on SQLite; this
one pins the *posture* — that the write actually lands against a database that
enforces the policy.

CRITICAL: a PostgreSQL **superuser** and a **table owner** BYPASS RLS, so a test
connecting as the migration role would prove nothing. This creates its own
non-superuser, non-owner role and writes as it. See
``tests/integration/test_rls_tenant_isolation.py`` for the same technique.

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv/bin/alembic upgrade head
    .venv/bin/pytest tests/integration/test_conversion_rls_org_stamp.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.tenant_context import (
    _current_enterprise_id,
    get_current_enterprise_id,
)
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    DEFAULT_ORGANIZATION_ID,
    ConversionService,
)
from tests.utils import seed_organizations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

# Unique per worker process so parallel runs don't collide on the role name.
_LIMITED_ROLE = f"fm_conv_rls_{uuid4().hex[:8]}"
_LIMITED_PW = "fm_conv_rls_pw"
_DROP_ROLE_SQL = f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_LIMITED_ROLE}') THEN
    DROP OWNED BY {_LIMITED_ROLE};
    DROP ROLE {_LIMITED_ROLE};
  END IF;
END $$;
"""


@pytest.fixture
async def superuser_engine():
    """Engine as the migration/superuser role (owns the tables — bypasses RLS)."""
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    yield engine
    await engine.dispose()


@pytest.fixture
async def tenant_org(superuser_engine):
    """One tenant organization, seeded as superuser (which bypasses RLS)."""
    org_id = f"org_guest_{uuid4().hex[:8]}"
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        await seed_organizations(session, [org_id])
    yield org_id
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE organization_id = :o"), {"o": org_id}
        )


@pytest.fixture
async def written_conversions(superuser_engine):
    """Collects conversion ids to delete, and deletes them in teardown.

    Teardown rather than the test body on purpose: the rows are FK parents of
    nothing but FK *children* of ``organizations``, which the ``tenant_org``
    fixture drops. An assertion failure mid-test would otherwise leak them, and
    that DELETE would then raise ForeignKeyViolation — stacking a teardown error
    on top of the real failure and leaving the database dirty for the next run.
    """
    ids: list[str] = []
    yield ids
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        for conversion_id in ids:
            job = await session.get(ConversionJobModel, conversion_id)
            await session.execute(
                text("DELETE FROM conversion_drafts WHERE conversion_id = :c"),
                {"c": conversion_id},
            )
            await session.execute(
                text("DELETE FROM conversion_jobs WHERE id = :c"), {"c": conversion_id}
            )
            if job is not None:
                await session.execute(
                    text("DELETE FROM uploaded_files WHERE file_id = :f"),
                    {"f": job.source_file_id},
                )
        await session.commit()


@pytest.fixture
async def tenant_session_factory(superuser_engine):
    """Session factory over a non-superuser role, scoped exactly like production.

    The ``begin`` listener is the same one ``infrastructure/persistence/database``
    installs: it samples the tenant contextvar once per transaction and binds it
    to ``app.current_enterprise_id``. Reproducing it here is what makes the test a test
    of the real posture rather than of a hand-set GUC.
    """
    async with superuser_engine.begin() as conn:
        dbname = (await conn.exec_driver_sql("SELECT current_database()")).scalar()
        await conn.exec_driver_sql(_DROP_ROLE_SQL)
        await conn.exec_driver_sql(
            f"CREATE ROLE {_LIMITED_ROLE} LOGIN PASSWORD '{_LIMITED_PW}' NOSUPERUSER"
        )
        await conn.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{dbname}" TO {_LIMITED_ROLE}'
        )
        await conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {_LIMITED_ROLE}")
        await conn.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {_LIMITED_ROLE}"
        )

    limited_url = make_url(os.environ["DATABASE_URL"]).set(
        username=_LIMITED_ROLE, password=_LIMITED_PW
    )
    engine = create_async_engine(limited_url, future=True)

    @event.listens_for(engine.sync_engine, "begin")
    def _scope_tenant_per_transaction(conn):
        conn.execute(
            text(
                "SELECT set_config('app.current_enterprise_id', :enterprise_id, true)"
            ),
            {"enterprise_id": get_current_enterprise_id()},
        )

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()
    async with superuser_engine.begin() as conn:
        await conn.exec_driver_sql(_DROP_ROLE_SQL)


def _service(session_factory) -> ConversionService:
    from unittest.mock import AsyncMock, MagicMock

    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    return ConversionService(
        llm_router=AsyncMock(),
        settings=settings,
        db_session_factory=session_factory,
    )


async def _persist(service, conversion_id: str, organization_id, tmp_path) -> None:
    """Run the real ``_persist_job`` with the case-conversion argument shape."""
    draft_path = tmp_path / f"{conversion_id}.md"
    draft_path.write_text("# runbook", encoding="utf-8")
    await service._persist_job(
        conversion_id=conversion_id,
        user_id=None,  # users are not seeded here; the FK is ON DELETE SET NULL
        organization_id=organization_id,
        scope="personal",
        team_id=None,
        status=ConversionStatus.COMPLETED,
        source_file=SourceFileInfo(
            filename="Case case_1765256eccdd",
            size_bytes=3289,
            content_type="application/x-faultmaven-case",
            retained_path=None,
        ),
        analysis=AnalysisResult(
            is_actionable=True,
            failure_modes=[],
            source_assessment=SourceAssessment(
                content_type="resolved_case",
                actionability_rating="high",
                missing_information=[],
            ),
        ),
        drafts=[
            ConversionDraft(
                draft_id=f"draft_{uuid4().hex[:12]}",
                runbook_id="rb-pool-timeout",
                title="Pool timeout",
                scope="personal",
                status=DraftStatus.DRAFT,
                validation=ValidationResult(passed=True, errors=[], warnings=[]),
                quality_score=QualityScore(
                    overall=80.0,
                    grade="B",
                    completeness=80.0,
                    clarity=80.0,
                    actionability=80.0,
                    comprehensiveness=80.0,
                ),
                file_path=str(draft_path),
                content_preview="preview",
            )
        ],
        created_at=datetime.now(timezone.utc),
        source_type="case",
        case_id=None,  # no case row is seeded; the FK would not resolve
    )


@pytest.mark.asyncio
async def test_conversion_persists_under_tenant_rls(
    tenant_session_factory, tenant_org, superuser_engine, written_conversions, tmp_path
):
    """The write the user's click performs succeeds for a real tenant.

    No explicit org is passed — reproducing the chat path before #1143 gave it
    one — so this also proves the fallback resolves to the bound tenant rather
    than to the sentinel.
    """
    conversion_id = f"conv_{uuid4().hex[:12]}"
    # Registered BEFORE the write, so a partial write is cleaned up too.
    written_conversions.append(conversion_id)
    service = _service(tenant_session_factory)

    token = _current_enterprise_id.set(tenant_org)
    try:
        await _persist(service, conversion_id, None, tmp_path)
    finally:
        _current_enterprise_id.reset(token)

    # Read back as superuser so the assertion measures what was WRITTEN rather
    # than what the writing tenant is allowed to see.
    maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with maker() as session:
        job = await session.get(ConversionJobModel, conversion_id)
        assert job is not None, "the conversion job was not committed"
        upload = await session.get(UploadedFileModel, job.source_file_id)
        drafts = (
            (
                await session.execute(
                    select(ConversionDraftModel).where(
                        ConversionDraftModel.conversion_id == conversion_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {job.organization_id, upload.organization_id} | {
            d.organization_id for d in drafts
        } == {tenant_org}
        assert upload.upload_source == "conversion_source"
        # The conversion source is deliberately case-less: its case_id FK is
        # ON DELETE CASCADE while conversion_jobs.source_file_id is RESTRICT,
        # so linking it would make deleting a converted case fail. #1143 read
        # this as a defaults leak; it is load-bearing.
        assert upload.case_id is None


@pytest.mark.asyncio
@pytest.mark.security
async def test_sentinel_org_stamp_is_refused_under_tenant_rls(
    tenant_session_factory, tenant_org, tmp_path
):
    """The pre-fix stamp still fails — the posture is real, not decorative.

    Without this, a regression that reintroduced the sentinel could be masked by
    a test environment where RLS silently does not apply (a superuser or
    table-owner connection). Reproducing the original
    ``InsufficientPrivilegeError`` proves the fixture bites.
    """
    service = _service(tenant_session_factory)

    token = _current_enterprise_id.set(tenant_org)
    try:
        with pytest.raises(DBAPIError) as exc:
            await _persist(
                service, f"conv_{uuid4().hex[:12]}", DEFAULT_ORGANIZATION_ID, tmp_path
            )
    finally:
        _current_enterprise_id.reset(token)

    assert "row-level security" in str(exc.value).lower()
