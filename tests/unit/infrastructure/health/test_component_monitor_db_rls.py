"""Unit tests for the DB health check's RLS-bypass detection.

The database component must report DEGRADED when the connected PostgreSQL role
would BYPASS Row-Level Security (superuser / BYPASSRLS / owns tenanted tables),
because that silently defeats tenant isolation. On SQLite (single-tenant) RLS
does not apply, so a live connection is HEALTHY.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor,
    HealthStatus,
)


class _FakeSession:
    """Minimal async session: SELECT 1 connectivity + one catalog row."""

    def __init__(self, dialect: str, row: Optional[dict], raise_exc: bool = False):
        self._dialect = dialect
        self._row = row
        self._raise = raise_exc
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, *_args, **_kwargs):
        if self._raise:
            raise RuntimeError("connection refused")
        result = MagicMock()
        result.mappings.return_value.fetchone.return_value = self._row
        return result

    def get_bind(self):
        bind = MagicMock()
        bind.dialect.name = self._dialect
        return bind


class _ACM:
    def __init__(self, session: Any):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_a):
        return False


def _patch_session(monkeypatch, session: Any) -> None:
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_db_session",
        lambda *a, **k: _ACM(session),
    )


def _pg_row(*, superuser=False, bypassrls=False, owns=False, role="faultmaven_app"):
    return {
        "role": role,
        "is_superuser": superuser,
        "has_bypassrls": bypassrls,
        "owns_tenanted_tables": owns,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlite_is_healthy_rls_not_applicable(monkeypatch):
    _patch_session(monkeypatch, _FakeSession("sqlite", row=None))
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["rls_applicable"] is False
    assert out["metadata"]["database_type"] == "sqlite"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_postgres_limited_role_is_healthy(monkeypatch):
    _patch_session(monkeypatch, _FakeSession("postgresql", _pg_row()))
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert out["metadata"]["rls_bypassed"] is False
    assert out["metadata"]["rls_bypass_reasons"] == []
    assert out["metadata"]["db_role"] == "faultmaven_app"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_postgres_superuser_is_degraded(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession("postgresql", _pg_row(superuser=True, role="postgres")),
    )
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.DEGRADED
    assert "superuser" in out["metadata"]["rls_bypass_reasons"]
    assert "BYPASSES Row-Level Security" in out["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_postgres_table_owner_is_degraded(monkeypatch):
    _patch_session(
        monkeypatch, _FakeSession("postgresql", _pg_row(owns=True, role="faultmaven"))
    )
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.DEGRADED
    assert "owns_tenanted_tables" in out["metadata"]["rls_bypass_reasons"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_postgres_bypassrls_is_degraded(monkeypatch):
    _patch_session(monkeypatch, _FakeSession("postgresql", _pg_row(bypassrls=True)))
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.DEGRADED
    assert "bypassrls" in out["metadata"]["rls_bypass_reasons"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connection_failure_is_unhealthy(monkeypatch):
    _patch_session(monkeypatch, _FakeSession("postgresql", row=None, raise_exc=True))
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.UNHEALTHY
    assert "error" in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rls_check_inconclusive_stays_healthy(monkeypatch):
    """If connectivity works but the catalog query yields no row, don't cry wolf."""
    _patch_session(monkeypatch, _FakeSession("postgresql", row=None))
    out = await ComponentHealthMonitor()._check_database_health()
    assert out["status"] is HealthStatus.HEALTHY
    assert "rls_check_error" in out["metadata"]
