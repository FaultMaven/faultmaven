"""Membership writes and token revocation are one operation (#874, #1042).

Membership *and role* are verified at login only, so a removed member keeps
working tokens — and a demoted admin keeps **elevated** ones — until they expire
unless the revocation watermark is bumped too. These pin the four properties that
make each pairing trustworthy rather than incidental:

* both writes happen, in the order that has no TOCTOU window;
* the revocation happens even when no row matched, which is how an interrupted
  run is finished;
* a failed revocation is reported as the half-state it is, never as success;
* the service cannot be built without something to revoke with.

The role-change half (#1042) adds one property removal has no analogue for:
revocation is unconditional, so a *promotion* revokes too. See the service's
module docstring for why "which direction is this?" is not a question the
chokepoint should be answering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.tenant_context import (
    get_current_enterprise_id,
    set_current_enterprise_id,
)
from faultmaven.modules.auth.domain.services.organization_membership_service import (
    MembershipRemovalIncomplete,
    MembershipRemovalMisscoped,
    MembershipRoleChangeIncomplete,
    MembershipRoleChangeMisscoped,
    MembershipWriteMisscoped,
    OrganizationMembershipService,
)

pytestmark = pytest.mark.unit

ORG_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
#: The tenant the roster row is RLS-filtered by (ADR-017 D1). Deliberately NOT
#: the organization id: two organizations of one enterprise are equally
#: reachable from one binding, so a guard that compared the organization would
#: refuse correct writes and admit nothing extra.
ENTERPRISE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
USER_ID = "225bae2f-f459-4a54-9c08-2da5c2b3a961"
REVOKED_AT = datetime(2026, 8, 12, 9, 30, tzinfo=timezone.utc)

#: Seeded role ids, as a caller passes them (names are resolved before this
#: layer — see set_member_role's docstring for why resolution is not its job).
MEMBER_ROLE_ID = "8b1d4a2c-0000-4000-8000-00000000feed"
ADMIN_ROLE_ID = "8b1d4a2c-0000-4000-8000-00000000beef"


@pytest.fixture(autouse=True)
def bound_to_the_target_enterprise():
    """Bind the tenant context, as every real caller must, then put it back.

    The DELETE is RLS-filtered by it, so the service refuses when it names a
    different enterprise — see ``test_refuses_when_the_tenant_context_names_a
    _different_org``.

    The restore is the point of the ``yield``: this fixture is synchronous, so
    the set lands in pytest's own context and outlives the test. Leaving it set
    would make every later test in the session read this module's org id where
    it expects the documented ``STANDALONE_ENTERPRISE_ID`` default — a failure that
    points at the wrong file.
    """
    previous = get_current_enterprise_id()
    set_current_enterprise_id(ENTERPRISE_ID)
    yield
    set_current_enterprise_id(previous)


@pytest.fixture
def orgs():
    repo = AsyncMock()
    repo.remove_member.return_value = True
    repo.update_member_role.return_value = True
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
    result = await service.remove_member(ORG_ID, USER_ID, ENTERPRISE_ID)

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
    await service.remove_member(ORG_ID, USER_ID, ENTERPRISE_ID)

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

    result = await service.remove_member(ORG_ID, USER_ID, ENTERPRISE_ID)

    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert result.membership_removed is False
    assert result.revoked_before == REVOKED_AT


async def test_failed_revocation_raises_and_names_the_half_state(
    service, orgs, auth_service
):
    """The delete already landed, so this cannot be reported as "nothing happened"."""
    auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is gone")

    with pytest.raises(MembershipRemovalIncomplete) as exc:
        await service.remove_member(ORG_ID, USER_ID, ENTERPRISE_ID)

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
    set_current_enterprise_id("11111111-2222-3333-4444-555555555555")

    with pytest.raises(MembershipRemovalMisscoped, match="RLS-filtered"):
        await service.remove_member(ORG_ID, USER_ID, ENTERPRISE_ID)

    orgs.remove_member.assert_not_awaited()
    auth_service.revoke_user_tokens.assert_not_awaited()


async def test_cannot_be_constructed_without_an_auth_service(orgs):
    """A service with no way to revoke is the unpaired removal wearing a hat."""
    with pytest.raises(ValueError, match="requires an auth_service"):
        OrganizationMembershipService(organization_repository=orgs, auth_service=None)


# ---------------------------------------------------------------------------
# Role change (#1042) — the unfixed sibling of removal.
#
# The demotion case is the one that matters: `organization_members.role_id` says
# `member` while every outstanding token still says `admin`, and the admin
# console shows the demotion as done. Nothing on the request path re-reads the
# row, so only the watermark closes that gap.
# ---------------------------------------------------------------------------


async def test_role_change_also_revokes(service, orgs, auth_service):
    """The whole point: one call, both writes."""
    result = await service.set_member_role(
        ORG_ID, USER_ID, MEMBER_ROLE_ID, ENTERPRISE_ID
    )

    orgs.update_member_role.assert_awaited_once_with(ORG_ID, USER_ID, MEMBER_ROLE_ID)
    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert result.role_changed is True
    assert result.revoked_before == REVOKED_AT


async def test_revocation_happens_after_the_role_write(orgs, auth_service):
    """Order matters, so it is asserted rather than assumed.

    Revoking first leaves a window in which a login mints a token whose ``iat``
    is past the watermark but which still reads the *old* role — the elevated
    claim this demotion is taking away, surviving the revocation meant to kill
    it.
    """
    calls: list[str] = []
    orgs.update_member_role.side_effect = lambda *a: calls.append("write") or True
    auth_service.revoke_user_tokens.side_effect = (
        lambda *a: calls.append("revoke") or REVOKED_AT
    )

    service = OrganizationMembershipService(
        organization_repository=orgs, auth_service=auth_service
    )
    await service.set_member_role(ORG_ID, USER_ID, MEMBER_ROLE_ID, ENTERPRISE_ID)

    assert calls == ["write", "revoke"]


async def test_promotion_revokes_too(service, orgs, auth_service):
    """Revocation is unconditional, not conditioned on the direction of the move.

    A promotion is safe to leave alone — the outstanding token is *less*
    privileged than the new state. Revoking anyway is the deliberate choice:
    deciding direction would mean this service ranking arbitrary ``role_id``
    values, a question with no stable answer and a silent privilege leak when
    answered wrongly. The cost is one re-login.
    """
    await service.set_member_role(ORG_ID, USER_ID, ADMIN_ROLE_ID, ENTERPRISE_ID)

    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)


async def test_revokes_even_when_no_membership_row_matched(service, orgs, auth_service):
    """Re-running after a failed revocation must finish the job.

    Same reasoning as removal: if the role write landed and the revocation did
    not, the retry has to revoke regardless of what the second write reports, or
    it would return "not a member" and leave the elevated tokens alive forever.
    """
    orgs.update_member_role.return_value = False

    result = await service.set_member_role(
        ORG_ID, USER_ID, MEMBER_ROLE_ID, ENTERPRISE_ID
    )

    auth_service.revoke_user_tokens.assert_awaited_once_with(USER_ID)
    assert result.role_changed is False
    assert result.revoked_before == REVOKED_AT


async def test_failed_revocation_after_role_change_names_the_half_state(
    service, orgs, auth_service
):
    """The role write already landed, so this cannot read as "nothing happened".

    The message has to say which way the half-state points: the stored role is
    the new one, the tokens still carry the old one. An operator who reads this
    as "the demotion did not apply" would retry the console and see success,
    while the elevated tokens keep working.
    """
    auth_service.revoke_user_tokens.side_effect = RuntimeError("redis is gone")

    with pytest.raises(MembershipRoleChangeIncomplete) as exc:
        await service.set_member_role(ORG_ID, USER_ID, MEMBER_ROLE_ID, ENTERPRISE_ID)

    orgs.update_member_role.assert_awaited_once_with(ORG_ID, USER_ID, MEMBER_ROLE_ID)
    message = str(exc.value)
    assert "STILL VALID" in message
    assert "PREVIOUS role" in message
    assert "Re-run" in message
    assert "redis is gone" in message
    assert exc.value.__cause__ is auth_service.revoke_user_tokens.side_effect


async def test_role_change_refuses_when_the_tenant_context_names_a_different_org(
    service, orgs, auth_service
):
    """A misscoped UPDATE matches nothing and reports as "not a member".

    ``organization_members`` is RLS-tenanted, so the write is filtered by the
    bound context. A caller that bound its own org would see
    ``role_changed=False``, read it as "not a member here", and move on — while
    the member keeps the role the call was taking away. Refusing before either
    write is what makes this a chokepoint rather than a convention.
    """
    set_current_enterprise_id("11111111-2222-3333-4444-555555555555")

    with pytest.raises(MembershipRoleChangeMisscoped, match="RLS-filtered"):
        await service.set_member_role(ORG_ID, USER_ID, MEMBER_ROLE_ID, ENTERPRISE_ID)

    orgs.update_member_role.assert_not_awaited()
    auth_service.revoke_user_tokens.assert_not_awaited()


async def test_a_repository_failure_leaves_no_half_state(service, orgs, auth_service):
    """If the role write itself fails, nothing was revoked and nothing changed.

    This is the ordering paying off: an unknown ``role_id`` (an FK violation) or
    a dropped connection surfaces before the watermark is touched, so the caller
    gets a clean "nothing happened" rather than a state to reconcile.
    """
    orgs.update_member_role.side_effect = RuntimeError("foreign key violation")

    with pytest.raises(RuntimeError, match="foreign key violation"):
        await service.set_member_role(
            ORG_ID, USER_ID, "not-a-real-role-id", ENTERPRISE_ID
        )

    auth_service.revoke_user_tokens.assert_not_awaited()


@pytest.mark.parametrize(
    "misscoped",
    [MembershipRemovalMisscoped, MembershipRoleChangeMisscoped],
    ids=["removal", "role-change"],
)
def test_both_misscoped_errors_share_a_base(misscoped):
    """A caller that wants to treat "refused before any write" uniformly can.

    The two are separate classes so an error message can name the write it
    refused, but they mean the same thing to a caller deciding whether anything
    happened — and a caller forced to enumerate subclasses is a caller that
    misses the next one.
    """
    assert issubclass(misscoped, MembershipWriteMisscoped)
