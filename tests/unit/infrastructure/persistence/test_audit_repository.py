"""Unit tests for PostgreSQLAuditRepository (ADR-015 PR 7).

Exercises the audit repository against a real in-memory SQLite engine (via the
ORM's Base.metadata.create_all), mirroring test_team_repository. Pins the
guarantees the SSO JIT audit trail relies on: full-fidelity round-trip of an
event (enums, details JSON, transport metadata, success flag), tenant-org
stamping when the caller supplies no organization (the RLS WITH CHECK
precondition), newest-first pagination, and bounded transport fields.

FK enforcement is left OFF (SQLite default), so rows can be inserted without
seeding parent user/org rows — these tests assert repository behavior, not FK
integrity.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.infrastructure.persistence.audit_repository import (
    PostgreSQLAuditRepository,
)
from faultmaven.infrastructure.persistence.models import Base, UserAuditLogModel
from faultmaven.infrastructure.persistence.sessionless_audit_repository import (
    SessionlessAuditRepository,
)
from faultmaven.models.interfaces_user import AuditCategory, AuditEventType


@pytest.fixture(scope="function")
async def engine():
    """In-memory SQLite engine with the full ORM schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    """One AsyncSession per test; expire_on_commit=False so we can read attrs."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def repo(session):
    """PostgreSQLAuditRepository bound to the test session."""
    return PostgreSQLAuditRepository(session)


@pytest.mark.unit
async def test_log_event_round_trips_all_fields(repo):
    ok = await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.ACCOUNT_CREATED,
        event_category=AuditCategory.AUTHENTICATION,
        resource_type="user",
        resource_id="u-1",
        details={"provider": "workos", "method": "sso_jit"},
        ip_address="203.0.113.7",
        user_agent="Mozilla/5.0 (test)",
        session_id="sess-abc",
        organization_id="org-x",
        success=True,
    )
    assert ok is True

    entries = await repo.get_user_audit_log("u-1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.user_id == "u-1"
    assert entry.event_type is AuditEventType.ACCOUNT_CREATED
    assert entry.event_category is AuditCategory.AUTHENTICATION
    assert entry.resource_type == "user"
    assert entry.resource_id == "u-1"
    assert entry.details == {"provider": "workos", "method": "sso_jit"}
    assert entry.ip_address == "203.0.113.7"
    assert entry.user_agent == "Mozilla/5.0 (test)"
    assert entry.session_id == "sess-abc"
    assert entry.organization_id == "org-x"
    assert entry.success is True
    assert entry.event_at is not None
    assert entry.audit_id > 0


@pytest.mark.unit
async def test_log_event_stamps_tenant_org_when_none_given(repo):
    """A missing organization defaults to the tenant-context org — the same
    value the engine binds as ``app.current_org_id``, so the INSERT satisfies
    the RLS policy's WITH CHECK instead of writing a NULL that RLS rejects."""
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.ACCOUNT_CREATED,
        event_category=AuditCategory.AUTHENTICATION,
    )
    entries = await repo.get_user_audit_log("u-1")
    assert entries[0].organization_id == STANDALONE_ORG_ID


@pytest.mark.unit
async def test_log_event_minimal_call_defaults(repo):
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.LOGIN,
        event_category=AuditCategory.AUTHENTICATION,
    )
    entry = (await repo.get_user_audit_log("u-1"))[0]
    assert entry.details is None
    assert entry.ip_address is None
    assert entry.user_agent is None
    assert entry.session_id is None
    assert entry.success is True


@pytest.mark.unit
async def test_log_event_records_failure_entries(repo):
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.LOGIN_FAILED,
        event_category=AuditCategory.AUTHENTICATION,
        success=False,
    )
    entry = (await repo.get_user_audit_log("u-1"))[0]
    assert entry.event_type is AuditEventType.LOGIN_FAILED
    assert entry.success is False


@pytest.mark.unit
async def test_oversized_transport_fields_are_truncated_not_fatal(repo):
    """A hostile User-Agent (or malformed address) must degrade to a truncated
    audit field, never fail the INSERT — and with it, the audited action."""
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.ACCOUNT_CREATED,
        event_category=AuditCategory.AUTHENTICATION,
        ip_address="9" * 100,
        user_agent="A" * 5000,
        session_id="s" * 200,
    )
    entry = (await repo.get_user_audit_log("u-1"))[0]
    assert len(entry.ip_address) == 45
    assert len(entry.user_agent) == 512
    assert len(entry.session_id) == 64


@pytest.mark.unit
async def test_user_log_is_newest_first_and_paginated(repo):
    for i in range(5):
        await repo.log_event(
            user_id="u-1",
            event_type=AuditEventType.LOGIN,
            event_category=AuditCategory.AUTHENTICATION,
            details={"n": i},
        )
    page = await repo.get_user_audit_log("u-1", limit=2, offset=0)
    assert [e.details["n"] for e in page] == [4, 3]
    page = await repo.get_user_audit_log("u-1", limit=2, offset=2)
    assert [e.details["n"] for e in page] == [2, 1]


@pytest.mark.unit
async def test_org_log_filters_by_organization(repo):
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.LOGIN,
        event_category=AuditCategory.AUTHENTICATION,
        organization_id="org-a",
    )
    await repo.log_event(
        user_id="u-2",
        event_type=AuditEventType.LOGIN,
        event_category=AuditCategory.AUTHENTICATION,
        organization_id="org-b",
    )
    entries = await repo.get_organization_audit_log("org-a")
    assert [e.user_id for e in entries] == ["u-1"]


@pytest.mark.unit
async def test_corrupt_details_blob_does_not_break_reads(repo, session):
    """A hand-mangled details blob must not make the audit trail unreadable."""
    session.add(
        UserAuditLogModel(
            user_id="u-1",
            organization_id="org-a",
            event_type=AuditEventType.LOGIN.value,
            event_category=AuditCategory.AUTHENTICATION.value,
            details="{not json",
            success=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    entry = (await repo.get_user_audit_log("u-1"))[0]
    assert entry.details == {"_unparsed": "{not json"}


@pytest.mark.unit
async def test_sessionless_wrapper_round_trips_through_fresh_sessions(
    engine, monkeypatch
):
    """The production-wired SessionlessAuditRepository opens one session per
    operation via get_db_session — a write in one session must be visible to a
    read in the next (per-op commit/close, mirroring the lifecycle-hygiene
    posture of SessionlessUserRepository)."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def fake_get_db_session(database_url=None):
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # The wrapper binds get_db_session at import time — patch its module ref.
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.sessionless_audit_repository."
        "get_db_session",
        fake_get_db_session,
    )

    repo = SessionlessAuditRepository()
    assert await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.ACCOUNT_CREATED,
        event_category=AuditCategory.AUTHENTICATION,
        details={"provider": "workos"},
        organization_id="org-a",
    )
    entries = await repo.get_user_audit_log("u-1")
    assert len(entries) == 1
    assert entries[0].details == {"provider": "workos"}
    assert (await repo.get_organization_audit_log("org-a"))[0].user_id == "u-1"


@pytest.mark.unit
async def test_details_serialize_non_json_values_via_str(repo):
    """Non-JSON-native values (datetimes) serialize via default=str rather
    than failing the write."""
    stamp = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    await repo.log_event(
        user_id="u-1",
        event_type=AuditEventType.LOGIN,
        event_category=AuditCategory.AUTHENTICATION,
        details={"at": stamp},
    )
    entry = (await repo.get_user_audit_log("u-1"))[0]
    assert entry.details == {"at": str(stamp)}
    # And the stored blob is genuine JSON.
    json.dumps(entry.details)
