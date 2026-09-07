"""A stale organization claim must not select someone else's ledger (ADR-017 D2/D5).

The front door binds two facts from the verified token: the enterprise (the
isolation binding, refused when absent) and the billing organization
(attribution, ``None`` when the account is in none). The enterprise claim is
checked — ``usable_tenant_id`` refuses the sentinel and the empty case. The
organization claim was not checked at all: whatever the token said became the
value every writer stamps and the value the turn cap charges.

An access token lives under thirty minutes, so the stale window is bounded — but
inside it a claim naming an organization the account has left, or one that has
been deleted, still selects that organization's ledger row and stamps its id on
every row the request writes. The claim is caller-presented; the row is not. So
the claim is validated where it is read: it must name a LIVE organization of the
enterprise the same request is bound to, or it is dropped and the request acts as
what it now is — an account in no organization.

The lookup runs after the enterprise is bound, so it is itself RLS-scoped: an
organization of another enterprise does not resolve, and needs no separate
predicate.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.api.middleware.tenant_scope import bind_request_enterprise_context
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    get_current_enterprise_id,
    set_current_billing_organization_id,
    set_current_enterprise_id,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENTERPRISE = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
LIVE_ORG = "org_live"
GHOST_ORG = "org_deleted_yesterday"


@pytest.fixture(autouse=True)
def _multi_tenant(monkeypatch):
    from faultmaven.providers.tenancy import factory as tenancy_factory

    monkeypatch.setattr(
        tenancy_factory,
        "requested_tenant_provider",
        lambda: tenancy_factory.BUILTIN_MULTI,
    )
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)


def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {"authorization": "Bearer forged-but-verified"}
    return request


def _auth_service(organization_id) -> MagicMock:
    service = MagicMock()
    service.verify_token_with_revocation_check = AsyncMock(
        return_value={
            "sub": "user_1",
            "enterprise_id": ENTERPRISE,
            "organization_id": organization_id,
        }
    )
    return service


def _organizations(known) -> MagicMock:
    repository = MagicMock()

    async def get_organization(organization_id):
        return (
            SimpleNamespace(organization_id=organization_id, enterprise_id=ENTERPRISE)
            if organization_id in known
            else None
        )

    repository.get_organization = AsyncMock(side_effect=get_organization)
    return repository


async def test_a_claim_naming_a_live_organization_is_bound():
    """The control: validation must drop the stale claim, not every claim."""
    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(LIVE_ORG),
        organization_repository=_organizations({LIVE_ORG}),
    )

    assert get_current_enterprise_id() == ENTERPRISE
    assert get_current_billing_organization_id() == LIVE_ORG


async def test_a_claim_naming_an_organization_that_does_not_resolve_is_dropped():
    """Deleted, left, or in another enterprise — all the same answer."""
    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(GHOST_ORG),
        organization_repository=_organizations({LIVE_ORG}),
    )

    assert get_current_enterprise_id() == ENTERPRISE, "the isolation binding held"
    assert get_current_billing_organization_id() is None


async def test_the_isolation_binding_is_not_affected_by_a_bad_billing_claim():
    """Billing is attribution; dropping it must not refuse the request.

    Refusing here would turn a lapsed subscription into an outage. The account
    keeps its enterprise anchor — leaving an organization changes what is
    metered, not what is visible (ADR-017 D5).
    """
    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(GHOST_ORG),
        organization_repository=_organizations(set()),
    )

    assert get_current_enterprise_id() == ENTERPRISE


async def test_no_claim_asks_no_question():
    """An account in no organization is the ordinary case — and costs no query."""
    organizations = _organizations({LIVE_ORG})

    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(None),
        organization_repository=organizations,
    )

    assert get_current_billing_organization_id() is None
    organizations.get_organization.assert_not_awaited()


async def test_an_unavailable_repository_drops_the_claim_rather_than_trusting_it():
    """The validation's own failure direction.

    A lookup that cannot run has not established that the claim is good, and the
    claim is the caller-presented half. Dropping it costs the deployment
    attribution on those requests and charges the account's own allowance, which
    is the conservative direction; trusting it would restore exactly the hole
    the check exists to close.
    """
    organizations = MagicMock()
    organizations.get_organization = AsyncMock(side_effect=RuntimeError("no database"))

    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(LIVE_ORG),
        organization_repository=organizations,
    )

    assert get_current_enterprise_id() == ENTERPRISE
    assert get_current_billing_organization_id() is None


async def test_a_deployment_with_no_organization_repository_still_binds():
    """Standalone composes none, and cloud+multi can be built before it is wired."""
    await bind_request_enterprise_context(
        _request(),
        auth_service=_auth_service(LIVE_ORG),
        organization_repository=None,
    )

    assert get_current_enterprise_id() == ENTERPRISE
    assert get_current_billing_organization_id() is None
