"""Tenancy on the JWT-derived ``DevUser`` (ADR-010 P2b, re-keyed by ADR-017).

``get_current_user_optional`` (``api/v1/auth_dependencies``) builds a ``DevUser``
from a validated JWT. It carries the two tenancy facts, and it must source them
from two DIFFERENT places, because they are two different kinds of thing:

* ``enterprise_id`` — ISOLATION — comes from the request-scoped tenant
  contextvar (``config.tenant_context``), the same value PostgreSQL RLS is
  enforcing for this request. NOT from the raw claim: sourcing the claim would
  let a forged one diverge from the enforced tenant, and would silently mask a
  missing one to Standalone through ``DevUser.__post_init__``.
* ``organization_id`` — BILLING — comes from the raw claim, or is ``None``. It
  decides nothing about visibility, and an account in no organization must
  present ``None`` rather than a sentinel a later reader could mistake for a
  tenant.

The global ``bind_request_enterprise_context`` dependency (P2b) has already
resolved the contextvar before this dependency runs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id

OTHER_ENTERPRISE = "22222222-2222-2222-2222-222222222222"
FORGED_ENTERPRISE = "99999999-9999-9999-9999-999999999999"
BILLING_ORG = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """Keep contextvar state from leaking across tests."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def _resolve_user(claims: dict):
    """Invoke get_current_user_optional with a verified-claims stub.

    Verification + revocation are AuthService's job (pinned by
    test_auth_dependencies_revocation.py); these tests stub the verified
    claims to isolate the tenancy-sourcing behaviour.
    """
    auth_service = MagicMock()
    auth_service.verify_token_with_revocation_check = AsyncMock(return_value=claims)
    return await get_current_user_optional(
        request=MagicMock(), token="a.valid.token", auth_service=auth_service
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_the_enterprise_comes_from_the_binding_not_the_raw_claim():
    """A forged claim can never re-scope the user object.

    The binding is what RLS is enforcing; anything else on this object would be
    a second, disagreeing answer to the same question.
    """
    set_current_enterprise_id(OTHER_ENTERPRISE)  # the binder bound the verified one

    user = await _resolve_user(
        {"sub": "user-1", "enterprise_id": FORGED_ENTERPRISE, "auth_mode": "oauth"}
    )

    assert user is not None
    assert user.enterprise_id == OTHER_ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_missing_enterprise_claim_reflects_the_binding_not_the_sentinel():
    """The silent-masking trap: ``DevUser.__post_init__`` stamps Standalone."""
    set_current_enterprise_id(OTHER_ENTERPRISE)

    user = await _resolve_user({"sub": "user-1", "auth_mode": "oauth"})

    assert user is not None
    assert user.enterprise_id == OTHER_ENTERPRISE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_tenant_default_binding_yields_standalone():
    """Single-tenant: the binder forces the Standalone enterprise, so the
    DevUser is scoped to it regardless of any injected claim."""
    user = await _resolve_user(
        {"sub": "user-1", "enterprise_id": FORGED_ENTERPRISE, "auth_mode": "local"}
    )

    assert user is not None
    assert user.enterprise_id == STANDALONE_ENTERPRISE_ID


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_billing_organization_is_the_raw_claim():
    """Billing is attribution, so the claim is the right source for it.

    And it is NOT the binding: a user object whose organization mirrored the
    enterprise would re-conflate exactly what ADR-017 separated.
    """
    set_current_enterprise_id(OTHER_ENTERPRISE)

    user = await _resolve_user(
        {
            "sub": "user-1",
            "organization_id": BILLING_ORG,
            "auth_mode": "oauth",
        }
    )

    assert user is not None
    assert user.organization_id == BILLING_ORG
    assert user.enterprise_id == OTHER_ENTERPRISE


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{}, {"organization_id": ""}])
async def test_an_account_in_no_organization_presents_none(claims):
    """``None``, never a sentinel.

    An account nobody pays for is the ordinary case (ADR-017 D5), and a sentinel
    here is what a later reader would mistake for a tenant — the conflation this
    campaign exists to undo.
    """
    set_current_enterprise_id(OTHER_ENTERPRISE)

    user = await _resolve_user({"sub": "user-1", "auth_mode": "oauth", **claims})

    assert user is not None
    assert user.organization_id is None
