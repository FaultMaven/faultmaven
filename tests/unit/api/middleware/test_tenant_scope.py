"""Unit tests for the request→enterprise binding dependency (ADR-010 P2b, ADR-017).

``bind_request_enterprise_context`` is the request front door that sets the RLS
contextvar (``config.tenant_context``) the engine ``begin`` listener reads. It
must:

* single-tenant: force the Standalone enterprise, ignoring any injected tenant
  (re-leak guard) — regardless of what a forged token claims;
* multi-tenant: bind the verified ``enterprise_id`` CLAIM;
* multi-tenant: fail closed (403) when the claim is absent, with **no fallback
  to ``users.enterprise_id``** — ADR-017's "no data migration" rule makes that a
  rule rather than an omission, and it is what stops a token minted before the
  cutover from being honoured;
* multi-tenant: leave the non-tenant sentinel bound for unauthenticated /
  invalid-token requests (public endpoints), whose own auth dependency 401s;
* bind the BILLING organization from the same claim set, and never let it decide
  anything about visibility.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    get_current_enterprise_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    TokenRevocationError,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI, BUILTIN_SINGLE
from tests.utils import request_with_authorization

OTHER_ENTERPRISE = "11111111-1111-1111-1111-111111111111"
BILLING_ORG = "22222222-2222-2222-2222-222222222222"

#: The single override point for "which tenant provider is in force".
#: ``bind_request_enterprise_context`` selects its arm through this module
#: attribute and ``config.tenant_context.usable_tenant_id`` resolves the same
#: one, so patching it here governs both — patching the middleware's own name
#: would move only the arm selection and leave the sentinel rule reading the
#: real configuration.
_PROVIDER_TARGET = "faultmaven.providers.tenancy.factory.requested_tenant_provider"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Keep contextvar state from leaking across tests."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)


def _auth_service(claims=None, error=None):
    """An AuthService double whose verification returns raw CLAIMS.

    Claims, not a user object: the binder reads the token, and reading the user
    row instead is the fallback ADR-017 forbids. A double that handed back a
    user would make that mistake untestable here.
    """
    service = AsyncMock()
    if error is not None:
        service.verify_token_with_revocation_check.side_effect = error
    else:
        service.verify_token_with_revocation_check.return_value = claims
    return service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_forces_standalone_ignoring_the_claim():
    """Single-tenant scopes to the Standalone enterprise and never consults the
    token, so a forged claim cannot re-scope a Standalone deployment."""
    import faultmaven.providers.tenancy.factory as factory

    original = factory.requested_tenant_provider
    factory.requested_tenant_provider = lambda: BUILTIN_SINGLE
    try:
        set_current_enterprise_id(OTHER_ENTERPRISE)
        auth_service = _auth_service({"enterprise_id": OTHER_ENTERPRISE})

        await bind_request_enterprise_context(
            request_with_authorization("Bearer forged-token"),
            auth_service=auth_service,
        )

        assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID
        # A standalone deployment has no organization at all (ADR-017 D8).
        assert get_current_billing_organization_id() is None
        auth_service.verify_token_with_revocation_check.assert_not_awaited()
    finally:
        factory.requested_tenant_provider = original


@pytest.fixture
def multi(monkeypatch):
    monkeypatch.setattr(_PROVIDER_TARGET, lambda: BUILTIN_MULTI)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_tenant_binds_the_verified_enterprise_claim(multi):
    auth_service = _auth_service({"sub": "user-1", "enterprise_id": OTHER_ENTERPRISE})

    await bind_request_enterprise_context(
        request_with_authorization("Bearer good-token"), auth_service=auth_service
    )

    assert get_current_enterprise_id() == OTHER_ENTERPRISE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_billing_organization_is_bound_beside_it(multi):
    """Both facts, from one verified claim set, into two separate bindings."""
    auth_service = _auth_service(
        {
            "sub": "user-1",
            "enterprise_id": OTHER_ENTERPRISE,
            "organization_id": BILLING_ORG,
        }
    )

    await bind_request_enterprise_context(
        request_with_authorization("Bearer good-token"), auth_service=auth_service
    )

    assert get_current_enterprise_id() == OTHER_ENTERPRISE
    assert get_current_billing_organization_id() == BILLING_ORG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_account_in_no_organization_binds_no_billing_subject(multi):
    """``None``, not a sentinel. An account nobody pays for is the ordinary case."""
    auth_service = _auth_service({"sub": "user-1", "enterprise_id": OTHER_ENTERPRISE})

    await bind_request_enterprise_context(
        request_with_authorization("Bearer good-token"), auth_service=auth_service
    )

    assert get_current_billing_organization_id() is None


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": "user-1"}, id="claim-absent"),
        pytest.param({"sub": "user-1", "enterprise_id": ""}, id="claim-empty"),
        pytest.param({"sub": "user-1", "enterprise_id": None}, id="claim-null"),
    ],
)
async def test_a_token_without_an_enterprise_claim_is_refused(multi, claims):
    """No claim, no tenant — and no fallback to the user row (ADR-017).

    The organization claim is deliberately present in one of these shapes'
    siblings elsewhere: a token that names a billing organization but no
    enterprise is still refused, because billing is not a tenant.
    """
    from fastapi import HTTPException

    auth_service = _auth_service({**claims, "organization_id": BILLING_ORG})

    with pytest.raises(HTTPException) as exc:
        await bind_request_enterprise_context(
            request_with_authorization("Bearer claimless-token"),
            auth_service=auth_service,
        )

    assert exc.value.status_code == 403
    # Contextvar untouched — no silent fall-through to Standalone.
    assert get_current_enterprise_id() == STANDALONE_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_binder_never_reads_the_user_row(multi):
    """The fallback ADR-017 forbids, asserted structurally.

    The binder is handed an ``AuthService`` and nothing else — no user store, no
    repository — so there is no row for it to fall back to. A future edit that
    added one would have to add a collaborator, which this assertion makes
    visible.
    """
    from fastapi import HTTPException

    auth_service = _auth_service({"sub": "user-1"})

    with pytest.raises(HTTPException):
        await bind_request_enterprise_context(
            request_with_authorization("Bearer claimless-token"),
            auth_service=auth_service,
        )

    # The ONLY thing it asked of its one collaborator was to verify the token.
    assert [call[0] for call in auth_service.method_calls] == [
        "verify_token_with_revocation_check"
    ]


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_sentinel_enterprise_claim_fails_closed(multi):
    """A token *claiming* the Standalone enterprise is refused under multi.

    Under multi the sentinel is not a tenant — it identifies the single-tenant
    deployment, and the global-KB write policy keys on it. Any account arriving
    with it carries an invented default (``DevUser.__post_init__`` stamps it),
    so accepting it would pool tenants *and* grant global-KB write. Enforced
    here so the guarantee does not depend on every token-minting path getting it
    right.
    """
    from fastapi import HTTPException

    auth_service = _auth_service(
        {"sub": "user-1", "enterprise_id": STANDALONE_ENTERPRISE_ID}
    )

    with pytest.raises(HTTPException) as exc:
        await bind_request_enterprise_context(
            request_with_authorization("Bearer sentinel-token"),
            auth_service=auth_service,
        )

    assert exc.value.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_token_binds_the_non_tenant_sentinel(multi):
    """Unauthenticated (public) requests bind the empty non-tenant: they match
    no enterprise's rows AND can never satisfy the RLS write policies'
    standalone-sentinel arm (#770) — structurally stronger than leaving the
    contextvar's Standalone default. The endpoint's own auth dependency still
    401s where auth is required."""
    auth_service = _auth_service({})

    await bind_request_enterprise_context(
        request_with_authorization(), auth_service=auth_service
    )

    assert get_current_enterprise_id() == ""
    assert get_current_billing_organization_id() is None
    auth_service.verify_token_with_revocation_check.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AuthenticationError("bad"), TokenRevocationError()])
async def test_an_invalid_token_binds_the_non_tenant_sentinel(multi, error):
    """An invalid or revoked token is not the binder's job to reject — bind the
    empty non-tenant (no rows, no sentinel write license) and defer the 401/403
    to the endpoint's auth dependency."""
    auth_service = _auth_service(error=error)

    await bind_request_enterprise_context(
        request_with_authorization("Bearer bad-token"), auth_service=auth_service
    )

    assert get_current_enterprise_id() == ""
    assert get_current_billing_organization_id() is None
