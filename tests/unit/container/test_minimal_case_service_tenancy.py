"""The degraded-mode case service stamps the two tenancy columns correctly.

``MinimalCaseService`` is what the container falls back to when no case
repository is available, so every line of it runs in production the moment the
repository is missing. It built its ``Case`` with no ``enterprise_id`` — a field
this campaign made required — so the fallback answered 500 to every case
creation, in exactly the situation where a working fallback is the point. And it
stamped ``organization_id = owner_id``: a user id in the organization column,
which is neither isolation nor billing but a third thing that means nothing.

The two columns come from two different places, and the stand-in must take them
from the same places the real service does — the request binding for isolation,
the actor's organization for billing (ADR-017 D1/D2).
"""

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)

pytestmark = [pytest.mark.unit]

ENTERPRISE = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
BILLING_ORG = "org_payer"
OWNER = "user_owner"


@pytest.fixture
def service():
    # Via the package facade, not ``_container_impl`` directly: the module
    # re-enters ``faultmaven.container`` and importing it first is a cycle.
    # ``object.__new__`` gives an unowned shell rather than the process
    # singleton ``DIContainer()`` returns.
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_case_service()


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)


async def test_a_degraded_case_creation_does_not_fail_on_a_required_field(service):
    """The whole point of a fallback is that it works."""
    set_current_enterprise_id(ENTERPRISE)
    set_current_billing_organization_id(None)

    case = await service.create_case(title="Checkout latency", owner_id=OWNER)

    assert case.enterprise_id == ENTERPRISE


async def test_billing_comes_from_the_actors_organization_not_from_the_owner_id(
    service,
):
    """``organization_id = owner_id`` was a user id in an organization column."""
    set_current_enterprise_id(ENTERPRISE)
    set_current_billing_organization_id(BILLING_ORG)

    case = await service.create_case(title="Checkout latency", owner_id=OWNER)

    assert case.organization_id == BILLING_ORG
    assert case.organization_id != OWNER


async def test_an_account_in_no_organization_is_billed_to_nobody(service):
    """``None`` is the ordinary answer, and the column is nullable for it."""
    set_current_enterprise_id(ENTERPRISE)
    set_current_billing_organization_id(None)

    case = await service.create_case(title="Checkout latency", owner_id=OWNER)

    assert case.organization_id is None
