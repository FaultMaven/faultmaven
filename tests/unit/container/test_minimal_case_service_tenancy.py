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


class TestTheDegradedReadHonoursOwnership:
    """Ownership applies on BOTH arms of the stand-in's ``get_case`` (#1398).

    It used to apply only under ``owner_only=True``, so every read-arm caller —
    which is most of them, including the session resume — got any case from any
    caller. That made a route-level gate resolving through this stand-in inert
    in exactly the mode where a working fallback is the point.

    Refusing a non-owner is both the narrow answer and the accurate one here:
    the real read arm is owner ∪ shared-to-my-teams, and this stand-in cannot
    consult the share allowlist because ``resource_shares`` lives in the
    repository it is standing in for — so in this mode no share can exist to
    honour.
    """

    async def _owned_case(self, service):
        set_current_enterprise_id(ENTERPRISE)
        set_current_billing_organization_id(None)
        return await service.create_case(title="Checkout latency", owner_id=OWNER)

    async def test_the_owner_still_reads_their_own_case(self, service):
        case = await self._owned_case(service)

        assert await service.get_case(case.case_id, OWNER) is not None
        assert await service.get_case(case.case_id, OWNER, owner_only=True) is not None

    async def test_a_stranger_is_refused_on_the_READ_arm(self, service):
        case = await self._owned_case(service)

        # The regression: `owner_only` defaults False, so this used to return
        # the owner's case to anyone who asked.
        assert await service.get_case(case.case_id, "user_stranger") is None

    async def test_an_unscoped_read_is_still_allowed(self, service):
        """``user_id=None`` is an internal caller with no user to check, which
        is the pre-existing contract and not something this narrows."""
        case = await self._owned_case(service)

        assert await service.get_case(case.case_id) is not None


class TestTheDegradedMutatorsHonourOwnership:
    """``update_case`` and ``hard_delete_case`` read the ``user_id`` they take.

    Both accepted it and ignored it, so in degraded mode any authenticated
    caller could rewrite or DESTROY another user's case — and
    ``DELETE /cases/{case_id}`` reaches ``hard_delete_case`` with no
    route-level pre-gate. Accepting an argument and ignoring it is the shape
    that makes a gate look present; the real service resolves through
    ``get_case`` in both.
    """

    async def _owned_case(self, service):
        set_current_enterprise_id(ENTERPRISE)
        set_current_billing_organization_id(None)
        return await service.create_case(title="Checkout latency", owner_id=OWNER)

    async def test_a_stranger_cannot_rewrite_the_owners_case(self, service):
        case = await self._owned_case(service)

        assert not await service.update_case(
            case.case_id, {"description": "pwned"}, "user_stranger"
        )
        assert (await service.get_case(case.case_id)).description != "pwned"

    async def test_a_stranger_cannot_destroy_the_owners_case(self, service):
        case = await self._owned_case(service)

        assert not await service.hard_delete_case(case.case_id, "user_stranger")
        assert await service.get_case(case.case_id) is not None

    async def test_the_owner_still_updates_and_deletes(self, service):
        case = await self._owned_case(service)

        assert await service.update_case(case.case_id, {"description": "real"}, OWNER)
        assert await service.hard_delete_case(case.case_id, OWNER)
        assert await service.get_case(case.case_id) is None

    async def test_deleting_an_absent_case_is_still_idempotent(self, service):
        """The contract the stand-in advertises: gone is gone, not an error."""
        assert await service.hard_delete_case("case_never_existed", OWNER)
