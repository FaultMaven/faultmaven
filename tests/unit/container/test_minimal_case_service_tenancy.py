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

from datetime import datetime, timezone

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


class TestTheDegradedTranscriptIsReadable:
    """The stand-in's conversation read returns its rows (#1397 review).

    Every `Message(...)` it built raised `ValidationError` — `turn_number` is
    required on the API model and neither the stored row nor the constructor
    supplied it — and the parse handler swallowed it. So
    `GET /cases/{id}/messages` and the operator transcript read both answered
    200 with an EMPTY list for a case that had a message, in the mode where a
    working fallback is the point.

    It is also why the role mapping this issue rewrote had no coverage: its
    output was discarded 100% of the time.
    """

    async def _case_with_a_message(self, service):
        set_current_enterprise_id(ENTERPRISE)
        set_current_billing_organization_id(None)
        return await service.create_case(
            title="Checkout latency", owner_id=OWNER, initial_message="hello world"
        )

    async def test_the_initial_message_is_actually_returned(self, service):
        case = await self._case_with_a_message(service)

        response = await service.get_case_messages_enhanced(case.case_id)

        assert response.total_count == 1
        # The half that was broken: counted, then dropped on the way out.
        assert response.retrieved_count == 1
        assert response.messages[0].content == "hello world"

    async def test_the_row_is_read_by_role(self, service):
        """`role` is read straight off the row now, not derived from a
        `message_type` this stand-in was the last writer and reader of."""
        case = await self._case_with_a_message(service)

        message = (await service.get_case_messages_enhanced(case.case_id)).messages[0]

        assert message.role == "user"
        assert message.turn_number == 1

    async def test_a_row_that_is_neither_participant_is_skipped(self, service):
        """The behaviour the old `message_type` mapping had, kept verbatim.

        `system` and not `tool`: the API model's `role` is a Literal, so a
        made-up role fails validation and would be excluded whatever this
        filter did — the test could not tell the filter from the model. A
        `system` row is valid on the model, so only the filter excludes it.

        ⚠️ This is a DIVERGENCE from the real service, which returns `system`
        rows (they are the runbook-conversion notices both clients render). It
        is preserved rather than fixed because it is what the mapping being
        replaced did, and nothing writes such a row into this stand-in today —
        `create_case` is its only writer and it writes `user`. Aligning the two
        is a separate question from retiring `message_type`.
        """
        case = await self._case_with_a_message(service)
        service.case_messages[case.case_id].append(
            {
                "message_id": "m-system",
                "case_id": case.case_id,
                "role": "system",
                "turn_number": 1,
                "content": "not a participant",
                "timestamp": datetime.now(timezone.utc),
            }
        )

        response = await service.get_case_messages_enhanced(case.case_id)

        assert [m.message_id for m in response.messages] == [f"initial_{case.case_id}"]
