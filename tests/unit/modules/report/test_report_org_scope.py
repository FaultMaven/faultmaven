"""Report-route organization scoping (ADR-010 P2c).

``validate_organization_access`` must resolve the current organization from the
request-bound tenant context (``config.tenant_context``) and pass it to
``TenantProvider.get_current_organization``. Before P2c it called the provider
with no ``organization_id``, so under ``TENANT_PROVIDER=multi`` the
``MultiTenantProvider`` raised "organization_id is required" and every report
endpoint failed closed with a blanket 403.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.modules.report.api.routes import validate_organization_access
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider

CTX_ORG = "22222222-2222-2222-2222-222222222222"
OTHER_ORG = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _current_user():
    user = MagicMock()
    user.user_id = "user_123"
    user.email = "user@example.com"
    return user


def _multi_provider(org_id=CTX_ORG, is_member=True):
    """A real MultiTenantProvider backed by a mock org repository."""
    org = MagicMock()
    org.organization_id = org_id
    org.name = "Acme"
    repo = MagicMock()
    repo.get_organization = AsyncMock(return_value=org)
    repo.get_member_role = AsyncMock(return_value="role_admin" if is_member else None)
    return MultiTenantProvider(organization_repository=repo)


@pytest.mark.asyncio
async def test_passes_context_org_to_provider():
    """The context org (not None) is threaded to get_current_organization."""
    recording = MagicMock()
    resolved = MagicMock()
    resolved.organization_id = CTX_ORG
    recording.get_current_organization = AsyncMock(return_value=resolved)

    set_current_org_id(CTX_ORG)
    await validate_organization_access(recording, _current_user())

    recording.get_current_organization.assert_awaited_once()
    _, kwargs = recording.get_current_organization.call_args
    assert kwargs.get("organization_id") == CTX_ORG


@pytest.mark.asyncio
async def test_multi_tenant_no_longer_403s_on_matching_case():
    """The bug fix: a real MultiTenantProvider validates instead of 403-ing."""
    set_current_org_id(CTX_ORG)
    # No exception == access granted for a case in the caller's org.
    await validate_organization_access(
        _multi_provider(), _current_user(), case_organization_id=CTX_ORG
    )


@pytest.mark.asyncio
async def test_multi_tenant_rejects_cross_org_case():
    """A case owned by a different org is denied with 403."""
    set_current_org_id(CTX_ORG)
    with pytest.raises(HTTPException) as exc:
        await validate_organization_access(
            _multi_provider(), _current_user(), case_organization_id=OTHER_ORG
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_multi_tenant_rejects_non_member():
    """A user who is not a member of the context org is denied with 403."""
    set_current_org_id(CTX_ORG)
    with pytest.raises(HTTPException) as exc:
        await validate_organization_access(
            _multi_provider(is_member=False), _current_user()
        )
    assert exc.value.status_code == 403
