"""Unit tests for the request→organization binding dependency (ADR-010 P2b).

``bind_request_org_context`` is the request front door that sets the RLS
contextvar (``config.tenant_context``) the engine ``begin`` listener reads. It
must:

* single-tenant: force the Standalone org, ignoring any injected org (re-leak
  guard) — regardless of what a forged token/header claims;
* multi-tenant: bind the authenticated user's verified ``organization_id``;
* multi-tenant: fail closed (403) when a verified user carries no org, never
  falling through to the contextvar's Standalone default;
* multi-tenant: leave the default for unauthenticated / invalid-token requests
  (public endpoints), whose own auth dependency will 401.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.api.middleware import tenant_scope
from faultmaven.api.middleware.tenant_scope import bind_request_org_context
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    TokenRevocationError,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE
from tests.utils import request_with_authorization

OTHER_ORG = "11111111-1111-1111-1111-111111111111"

#: The single override point for "which tenant provider is in force".
#: ``bind_request_org_context`` selects its arm through this module attribute and
#: ``config.tenant_context.usable_tenant_id`` resolves the same one, so patching
#: it here governs both — patching the middleware's own name would move only the
#: arm selection and leave the sentinel rule reading the real configuration.
_PROVIDER_TARGET = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


@pytest.fixture(autouse=True)
def _reset_org_context():
    """Keep contextvar state from leaking across tests."""
    set_current_org_id(STANDALONE_ORG_ID)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _user(organization_id):
    """Minimal AuthenticatedUser carrying the given org."""
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

    return AuthenticatedUser.from_jwt_claims(
        {"sub": "user-1", "organization_id": organization_id}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_forces_standalone_ignoring_injected_org(monkeypatch):
    """Single-tenant scopes to the Standalone org and never consults the token,
    so a forged org claim cannot re-scope a Standalone deployment."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_SINGLE)
    # Pre-set a foreign org to prove the dependency overrides it.
    set_current_org_id(OTHER_ORG)
    auth_service = AsyncMock()

    await bind_request_org_context(
        request_with_authorization("Bearer forged-token"), auth_service=auth_service
    )

    assert get_current_org_id() == STANDALONE_ORG_ID
    # The token is never verified in single-tenant mode.
    auth_service.extract_user_from_token_with_revocation_check.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_tenant_binds_verified_user_org(monkeypatch):
    """Multi-tenant binds the org from the user's verified claim."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    auth_service = AsyncMock()
    auth_service.extract_user_from_token_with_revocation_check.return_value = _user(
        OTHER_ORG
    )

    await bind_request_org_context(
        request_with_authorization("Bearer good-token"), auth_service=auth_service
    )

    assert get_current_org_id() == OTHER_ORG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_tenant_missing_org_fails_closed(monkeypatch):
    """A verified user with no org is refused, never scoped to the Standalone
    default (the fail-to-default hole P2b closes)."""
    from fastapi import HTTPException

    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    auth_service = AsyncMock()
    # from_jwt_claims defaults a missing organization_id claim to "".
    auth_service.extract_user_from_token_with_revocation_check.return_value = _user("")

    with pytest.raises(HTTPException) as exc:
        await bind_request_org_context(
            request_with_authorization("Bearer no-org-token"), auth_service=auth_service
        )

    assert exc.value.status_code == 403
    # Contextvar untouched — no silent fall-through to Standalone.
    assert get_current_org_id() == STANDALONE_ORG_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_multi_tenant_sentinel_org_claim_fails_closed(monkeypatch):
    """A token *claiming* the Standalone org is refused under multi.

    Under multi the sentinel is not a tenant — it identifies the single-tenant
    deployment, and migration 033 keys the global-KB write policy on it. Any
    user arriving with it carries an invented default (``DevUser.__post_init__``
    stamps it on every ``DatabaseUserStore`` load), so accepting it would pool
    tenants *and* grant global-KB write. Enforced here so the guarantee does not
    depend on every token-minting path getting it right.
    """
    from fastapi import HTTPException

    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    auth_service = AsyncMock()
    auth_service.extract_user_from_token_with_revocation_check.return_value = _user(
        STANDALONE_ORG_ID
    )

    with pytest.raises(HTTPException) as exc:
        await bind_request_org_context(
            request_with_authorization("Bearer sentinel-org-token"),
            auth_service=auth_service,
        )

    assert exc.value.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_tenant_no_token_binds_unscoped_org(monkeypatch):
    """Unauthenticated (public) requests are bound to the empty non-org: they
    match no org-owned rows AND can never satisfy the RLS write policies'
    single-tenant sentinel arm (migration 033, #770) — structurally stronger
    than leaving the contextvar's Standalone default. The endpoint's own auth
    dependency still 401s where auth is required."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    auth_service = AsyncMock()

    await bind_request_org_context(
        request_with_authorization(), auth_service=auth_service
    )

    assert get_current_org_id() == ""
    auth_service.extract_user_from_token_with_revocation_check.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AuthenticationError("bad"), TokenRevocationError()])
async def test_multi_tenant_invalid_token_binds_unscoped_org(monkeypatch, error):
    """An invalid or revoked token is not the org binder's job to reject — bind
    the empty non-org (no rows, no sentinel write license) and defer the
    401/403 to the endpoint's auth dependency."""
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)
    auth_service = AsyncMock()
    auth_service.extract_user_from_token_with_revocation_check.side_effect = error

    await bind_request_org_context(
        request_with_authorization("Bearer bad-token"), auth_service=auth_service
    )

    assert get_current_org_id() == ""
