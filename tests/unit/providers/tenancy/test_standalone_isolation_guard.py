"""Standalone must ignore any injected tenant (ADR-010, re-keyed by ADR-017 D8).

A single-tenant deployment has exactly one enterprise. The guard is that a
client-supplied tenant id changes nothing — not that it is validated, but that
it is **discarded before any lookup**, so a forged claim can never re-scope a
Standalone deployment and can never even be used to probe for a row.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.models.interfaces_user import Enterprise, EnterprisePlanTier
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

#: A plausible "attacker / other tenant" enterprise id.
FOREIGN_ENTERPRISE_ID = "deadbeef-dead-dead-dead-deaddeafbeef"


@pytest.fixture
def default_enterprise() -> Enterprise:
    return Enterprise(
        enterprise_id=SingleTenantProvider.DEFAULT_ENTERPRISE_ID,
        slug=SingleTenantProvider.DEFAULT_ENTERPRISE_SLUG,
        name=SingleTenantProvider.DEFAULT_ENTERPRISE_NAME,
        plan_tier=EnterprisePlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def provider(default_enterprise):
    repo = AsyncMock()
    repo.get_enterprise.return_value = default_enterprise
    return SingleTenantProvider(enterprise_repository=repo)


@pytest.fixture
def user() -> User:
    return User(
        user_id="user_123",
        email="owner@example.com",
        hashed_password="x",
        full_name="Local Owner",
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "injected",
    [
        FOREIGN_ENTERPRISE_ID,
        "",
        "   ",
        "another-enterprise",
        SingleTenantProvider.DEFAULT_ENTERPRISE_ID,
    ],
)
async def test_injected_enterprise_id_is_ignored(provider, user, injected):
    """Any client-supplied enterprise id is ignored -> forced to the default."""
    enterprise = await provider.get_current_enterprise(
        current_user=user, enterprise_id=injected
    )
    assert enterprise.enterprise_id == SingleTenantProvider.DEFAULT_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_foreign_enterprise_id_never_reaches_the_repository(provider, user):
    """The injected id must never even be used to query the database.

    Stronger than "the answer is the default": a provider that looked the
    foreign id up and *then* substituted the default would return the same
    object while acting as an existence oracle for another tenant's row.
    """
    await provider.get_current_enterprise(
        current_user=user, enterprise_id=FOREIGN_ENTERPRISE_ID
    )
    called_ids = [
        (c.args[0] if c.args else c.kwargs.get("enterprise_id"))
        for c in provider.enterprise_repository.get_enterprise.call_args_list
    ]
    assert called_ids == [SingleTenantProvider.DEFAULT_ENTERPRISE_ID]
    assert FOREIGN_ENTERPRISE_ID not in called_ids


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_single_tenant_mode_is_not_multi_tenant(provider):
    assert await provider.is_multi_tenant() is False
