"""Report-route organization scoping (ADR-010 P2c).

``validate_enterprise_access`` must resolve the current organization from the
request-bound tenant context (``config.tenant_context``) and pass it to
``TenantProvider.get_current_enterprise``. Before P2c it called the provider
with no ``organization_id``, so under ``TENANT_PROVIDER=multi`` the
``MultiTenantProvider`` raised "organization_id is required" and every report
endpoint failed closed with a blanket 403.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.report.api.routes import validate_enterprise_access
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider

CTX_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
OTHER_ENTERPRISE = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _current_user(enterprise_id=CTX_ENTERPRISE):
    """An account ANCHORED to the bound enterprise (ADR-017 D3).

    The anchor is the membership under multi-tenant — one column,
    ``users.enterprise_id`` — so an account without one is refused. A bare
    ``MagicMock`` would answer the attribute with another mock and pass the
    comparison against nothing.
    """
    user = MagicMock()
    user.user_id = "user_123"
    user.email = "user@example.com"
    user.enterprise_id = enterprise_id
    return user


def _multi_provider(enterprise_id=CTX_ENTERPRISE, is_member=True):
    """A real MultiTenantProvider backed by a mock enterprise repository."""
    enterprise = MagicMock()
    enterprise.enterprise_id = enterprise_id
    enterprise.name = "Acme"
    repo = MagicMock()
    repo.get_enterprise = AsyncMock(return_value=enterprise)
    repo.get_member_role = AsyncMock(return_value="role_admin" if is_member else None)
    return MultiTenantProvider(enterprise_repository=repo)


@pytest.mark.asyncio
async def test_passes_the_bound_enterprise_to_the_provider():
    """The bound enterprise (not None) reaches get_current_enterprise."""
    recording = MagicMock()
    resolved = MagicMock()
    resolved.enterprise_id = CTX_ENTERPRISE
    recording.get_current_enterprise = AsyncMock(return_value=resolved)

    set_current_enterprise_id(CTX_ENTERPRISE)
    await validate_enterprise_access(recording, _current_user())

    recording.get_current_enterprise.assert_awaited_once()
    _, kwargs = recording.get_current_enterprise.call_args
    assert kwargs.get("enterprise_id") == CTX_ENTERPRISE


@pytest.mark.asyncio
async def test_multi_tenant_no_longer_403s_on_matching_case():
    """The bug fix: a real MultiTenantProvider validates instead of 403-ing."""
    set_current_enterprise_id(CTX_ENTERPRISE)
    # No exception == access granted for a case in the caller's enterprise.
    await validate_enterprise_access(
        _multi_provider(), _current_user(), case_enterprise_id=CTX_ENTERPRISE
    )


@pytest.mark.asyncio
async def test_multi_tenant_rejects_a_case_from_another_enterprise():
    """A case owned by a different enterprise is denied with 403."""
    set_current_enterprise_id(CTX_ENTERPRISE)
    with pytest.raises(HTTPException) as exc:
        await validate_enterprise_access(
            _multi_provider(), _current_user(), case_enterprise_id=OTHER_ENTERPRISE
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_multi_tenant_rejects_non_member():
    """An account anchored to another enterprise is denied with 403."""
    set_current_enterprise_id(CTX_ENTERPRISE)
    with pytest.raises(HTTPException) as exc:
        await validate_enterprise_access(
            _multi_provider(), _current_user(enterprise_id=OTHER_ENTERPRISE)
        )
    assert exc.value.status_code == 403
