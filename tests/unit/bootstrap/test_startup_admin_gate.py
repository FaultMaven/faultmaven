"""Default-admin bootstrap is single-tenant-only (ADR-010 P2e).

The default admin is the Standalone passwordless account scoped to the default
organization. Under multi-tenant there is no default org and identities come
from the IdP — bootstrap must not even attempt to create it (it would plant a
passwordless admin in any Standalone org row present in the database).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.bootstrap.startup import bootstrap_application
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

_DATA_INIT = "faultmaven.bootstrap.data_init.initialize_data_layer"
_ADMIN = "faultmaven.bootstrap.data_init.ensure_default_admin_exists"


def _single_tenant_provider() -> MagicMock:
    provider = MagicMock(spec=SingleTenantProvider)
    provider.ensure_default_enterprise_exists = AsyncMock(return_value=None)
    provider.ensure_default_organization_exists = AsyncMock(
        return_value=SimpleNamespace(
            name="Standalone",
            organization_id="org-1",
            plan_tier=SimpleNamespace(value="free"),
        )
    )
    provider.ensure_default_team_exists = AsyncMock(return_value=None)
    return provider


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_multi_tenant_skips_default_admin_bootstrap():
    container = SimpleNamespace(tenant_provider=object())  # not SingleTenantProvider

    with patch(_DATA_INIT, new=AsyncMock()) as data_init:
        with patch(_ADMIN, new=AsyncMock()) as admin:
            await bootstrap_application(container)

    data_init.assert_awaited_once()
    admin.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_runs_default_admin_bootstrap():
    container = SimpleNamespace(tenant_provider=_single_tenant_provider())

    with patch(_DATA_INIT, new=AsyncMock()):
        with patch(_ADMIN, new=AsyncMock(return_value=None)) as admin:
            await bootstrap_application(container)

    admin.assert_awaited_once_with(container)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_tenant_provider_keeps_admin_bootstrap():
    """No provider (degraded dev container) preserves the pre-existing
    behavior: tenant bootstrap is skipped, admin bootstrap still runs."""
    container = SimpleNamespace(tenant_provider=None)

    with patch(_DATA_INIT, new=AsyncMock()):
        with patch(_ADMIN, new=AsyncMock(return_value=None)) as admin:
            await bootstrap_application(container)

    admin.assert_awaited_once_with(container)
