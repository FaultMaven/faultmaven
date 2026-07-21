"""Unit tests for the RLS role guard (ADR-010 P2d).

The real superuser-vs-limited-role behaviour is exercised against PostgreSQL in
``tests/integration/test_rls_tenant_isolation.py``. These tests cover the
decision logic and the multi/dialect gating without a database, so they run in
the normal (SQLite) CI unit suite.
"""

from types import SimpleNamespace

import pytest

from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.infrastructure.persistence.rls_role_guard import (
    _raise_if_rls_exempt,
    assert_app_db_role_enforces_rls,
)


class TestRaiseIfRlsExempt:
    def test_superuser_is_rejected(self):
        with pytest.raises(DeploymentCoherenceError, match="superuser=True"):
            _raise_if_rls_exempt("app", is_superuser=True, owns_rls_table=False)

    def test_table_owner_is_rejected(self):
        with pytest.raises(DeploymentCoherenceError, match="owns_rls_table=True"):
            _raise_if_rls_exempt("app", is_superuser=False, owns_rls_table=True)

    def test_limited_role_passes(self):
        # Non-superuser, non-owner -> no exception.
        _raise_if_rls_exempt("app", is_superuser=False, owns_rls_table=False)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _query):
        return _FakeResult(self._row)


class _FakeEngine:
    """Minimal async-engine stand-in: ``async with engine.connect() as conn``."""

    def __init__(self, row=None, dialect="postgresql"):
        self._row = row
        self.dialect = SimpleNamespace(name=dialect)

    def connect(self):
        return _FakeConn(self._row)


def _row(is_superuser=False, owns_rls_table=False, role_name="faultmaven_app"):
    return SimpleNamespace(
        role_name=role_name,
        is_superuser=is_superuser,
        owns_rls_table=owns_rls_table,
    )


@pytest.mark.asyncio
async def test_single_tenant_is_noop_without_touching_db():
    # is_multi_tenant=False returns before any engine access (engine stays None).
    await assert_app_db_role_enforces_rls(is_multi_tenant=False)


@pytest.mark.asyncio
async def test_sqlite_engine_is_noop():
    # SQLite (Standalone) has no RLS: return before querying, even under multi.
    engine = _FakeEngine(dialect="sqlite")
    await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)


@pytest.mark.asyncio
async def test_postgres_superuser_role_raises():
    engine = _FakeEngine(row=_row(is_superuser=True))
    with pytest.raises(DeploymentCoherenceError):
        await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)


@pytest.mark.asyncio
async def test_postgres_limited_role_passes():
    engine = _FakeEngine(row=_row(is_superuser=False, owns_rls_table=False))
    await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)
