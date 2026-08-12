"""Membership removal and token revocation are one operation (#874).

Membership is verified at login only, so a removed member keeps working tokens
until they expire unless the revocation watermark is bumped too. These pin the
four properties that make the pairing trustworthy rather than incidental:

* both writes happen, in the order that has no TOCTOU window;
* the revocation happens even when there was no row to delete, which is how an
  interrupted run is finished;
* a failed revocation is reported as the half-state it is, never as success;
* the service cannot be built without something to revoke with.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.tenant_context import set_current_org_id
from faultmaven.modules.auth.domain.services.organization_membership_service import (
    MembershipRemovalIncomplete,
    MembershipRemovalMisscoped,
    OrganizationMembershipService,
)

pytestmark = pytest.mark.unit

ORG_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
USER_ID = "225bae2f-f459-4a54-9c08-2da5c2b3a961"
REVOKED_AT = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def bound_to_the_target_org():
    """Bind the tenant context, as every real caller must.

    The DELETE is RLS-filtered by it, so the service refuses when it names a
    different organization — see ``test_refuses_when_the_tenant_context_names_a
    _different_org``.
    """
    set_current_org_id(ORG_ID)


@pytest.fixture
def orgs():
    repo = AsyncMock()
    repo.remove_member.return_value = True
    return repo


@pytest.fixture
def auth_service():
    service = AsyncMock()
    service.revoke_user_tokens.return_value = REVOKED_AT
    return service


@pytest.fixture
def service(orgs, auth_service):
    return OrganizationMembershipService(
        organization_repository=orgs, auth_service=auth_service
    )


async def test_removal_also_revokes(service, orgs, auth_service):
    """The whole point: one call, both writes."""
    result = await service.remove_member(ORG_ID, USER_ID)

    orgs.remove_member.assert_awaited_once_with(ORG_ID, USER_ID)
    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert result.membership_removed is True
    assert result.revoked_before == REVOKED_AT


async def test_revocation_happens_after_the_delete(orgs, auth_service):
    """Order matters, so it is asserted rather than assumed.

    Revoking first leaves a window in which a login mints a token with an ``iat``
    past the watermark but before the delete — carrying the very membership being
    removed, and surviving the revocation meant to kill it. Under SSO a JIT login
    in that window re-adds the membership row outright.
    """
    calls: list[str] = []
    orgs.remove_member.side_effect = lambda *a: calls.append("delete") or True
    auth_service.revoke_user_tokens.side_effect = (
        lambda *a: calls.append("revoke") or REVOKED_AT
    )

    service = OrganizationMembershipService(
        organization_repository=orgs, auth_service=auth_service
    )
    await service.remove_member(ORG_ID, USER_ID)

    assert calls == ["delete", "revoke"]


async def test_revokes_even_when_there_was_no_membership_row(
    service, orgs, auth_service
):
    """Re-running after a failed revocation must finish the job.

    The row is already gone by then, so a service that skipped revocation when
    ``remove_member`` returned False would report "not a member" and leave the
    tokens alive forever — exactly the state the retry exists to clear.
    """
    orgs.remove_member.return_value = False

    result = await service.remove_member(ORG_ID, USER_ID)

    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert result.membership_removed is False
    assert result.revoked_before == REVOKED_AT


async def test_failed_revocation_raises_and_names_the_half_state(
    service, orgs, auth_service
):
    """The delete already landed, so this cannot be reported as "nothing happened"."""
    auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is gone")

    with pytest.raises(MembershipRemovalIncomplete) as exc:
        await service.remove_member(ORG_ID, USER_ID)

    orgs.remove_member.assert_awaited_once_with(ORG_ID, USER_ID)
    message = str(exc.value)
    assert "STILL VALID" in message
    assert "Re-run" in message
    assert "redis is gone" in message
    assert exc.value.__cause__ is auth_service.revoke_user_tokens.side_effect


async def test_refuses_when_the_tenant_context_names_a_different_org(
    service, orgs, auth_service
):
    """A misscoped call would delete nothing and report it as "was not a member".

    ``organization_members`` is RLS-tenanted, so the DELETE is filtered by the
    bound context. A caller that forgot to bind (or bound its own org) would see
    ``membership_removed=False``, read it as "already not a member", and move on
    — while the membership survives and only the tokens were revoked. Refusing
    before either write is what makes this service a chokepoint rather than a
    convention.
    """
    set_current_org_id("11111111-2222-3333-4444-555555555555")

    with pytest.raises(MembershipRemovalMisscoped, match="RLS-filtered"):
        await service.remove_member(ORG_ID, USER_ID)

    orgs.remove_member.assert_not_awaited()
    auth_service.revoke_user_tokens.assert_not_awaited()


async def test_cannot_be_constructed_without_an_auth_service(orgs):
    """A service with no way to revoke is the unpaired removal wearing a hat."""
    with pytest.raises(ValueError, match="requires an auth_service"):
        OrganizationMembershipService(organization_repository=orgs, auth_service=None)
