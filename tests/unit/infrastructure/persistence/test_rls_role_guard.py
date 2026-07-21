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
    _raise_unless_maintenance_posture,
    assert_app_db_role_enforces_rls,
    assert_maintenance_db_role_posture,
)


class TestRaiseIfRlsExempt:
    def test_superuser_is_rejected(self):
        with pytest.raises(DeploymentCoherenceError, match="superuser=True"):
            _raise_if_rls_exempt(
                "app", is_superuser=True, has_bypassrls=False, owns_rls_table=False
            )

    def test_bypassrls_is_rejected(self):
        # A non-superuser, non-owner role with BYPASSRLS still bypasses RLS.
        with pytest.raises(DeploymentCoherenceError, match="bypassrls=True"):
            _raise_if_rls_exempt(
                "app", is_superuser=False, has_bypassrls=True, owns_rls_table=False
            )

    def test_table_owner_is_rejected(self):
        with pytest.raises(DeploymentCoherenceError, match="owns_rls_table=True"):
            _raise_if_rls_exempt(
                "app", is_superuser=False, has_bypassrls=False, owns_rls_table=True
            )

    def test_limited_role_passes(self):
        # Non-superuser, non-BYPASSRLS, non-owner -> no exception.
        _raise_if_rls_exempt(
            "app", is_superuser=False, has_bypassrls=False, owns_rls_table=False
        )


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


def _row(
    is_superuser=False,
    has_bypassrls=False,
    owns_rls_table=False,
    role_name="faultmaven_app",
):
    return SimpleNamespace(
        role_name=role_name,
        is_superuser=is_superuser,
        has_bypassrls=has_bypassrls,
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
async def test_postgres_bypassrls_role_raises():
    # Non-superuser, non-owner, but BYPASSRLS -> still exempt -> must fail closed.
    engine = _FakeEngine(row=_row(is_superuser=False, has_bypassrls=True))
    with pytest.raises(DeploymentCoherenceError):
        await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)


@pytest.mark.asyncio
async def test_postgres_limited_role_passes():
    engine = _FakeEngine(row=_row(is_superuser=False, owns_rls_table=False))
    await assert_app_db_role_enforces_rls(is_multi_tenant=True, engine=engine)


# =============================================================================
# Maintenance posture (audited cross-tenant path, ADR-010 / issue #629)
# =============================================================================


class TestRaiseUnlessMaintenancePosture:
    """The deliberate INVERSE of the app posture on exactly one axis:
    BYPASSRLS is REQUIRED; superuser and table ownership stay forbidden."""

    def test_bypassrls_non_super_non_owner_passes(self):
        _raise_unless_maintenance_posture(
            "maint", is_superuser=False, has_bypassrls=True, owns_rls_table=False
        )

    def test_missing_bypassrls_is_rejected(self):
        # The app role (RLS-enforced) must NOT run cross-tenant sweeps: its
        # partial single-org view is exactly the delete-other-tenants hazard.
        with pytest.raises(DeploymentCoherenceError, match="BYPASSRLS"):
            _raise_unless_maintenance_posture(
                "faultmaven_app",
                is_superuser=False,
                has_bypassrls=False,
                owns_rls_table=False,
            )

    def test_superuser_is_rejected_even_with_bypassrls(self):
        with pytest.raises(DeploymentCoherenceError, match="superuser"):
            _raise_unless_maintenance_posture(
                "postgres",
                is_superuser=True,
                has_bypassrls=True,
                owns_rls_table=False,
            )

    def test_owner_is_rejected_even_with_bypassrls(self):
        with pytest.raises(DeploymentCoherenceError, match="owns"):
            _raise_unless_maintenance_posture(
                "faultmaven",
                is_superuser=False,
                has_bypassrls=True,
                owns_rls_table=True,
            )


@pytest.mark.asyncio
async def test_maintenance_guard_passes_on_maintenance_role():
    engine = _FakeEngine(
        row=_row(has_bypassrls=True, role_name="faultmaven_maintenance")
    )
    await assert_maintenance_db_role_posture(engine=engine)


@pytest.mark.asyncio
async def test_maintenance_guard_rejects_app_role():
    # The regular RLS-enforced app role fails the maintenance probe.
    engine = _FakeEngine(row=_row())
    with pytest.raises(DeploymentCoherenceError, match="BYPASSRLS"):
        await assert_maintenance_db_role_posture(engine=engine)


@pytest.mark.asyncio
async def test_maintenance_guard_rejects_superuser():
    engine = _FakeEngine(row=_row(is_superuser=True, has_bypassrls=True))
    with pytest.raises(DeploymentCoherenceError):
        await assert_maintenance_db_role_posture(engine=engine)


@pytest.mark.asyncio
async def test_maintenance_guard_fails_closed_off_postgres():
    # Unlike the app guard (no-op on SQLite), the maintenance guard has no
    # legitimate non-PostgreSQL caller — fail closed, never silently pass.
    engine = _FakeEngine(dialect="sqlite")
    with pytest.raises(DeploymentCoherenceError, match="sqlite"):
        await assert_maintenance_db_role_posture(engine=engine)
