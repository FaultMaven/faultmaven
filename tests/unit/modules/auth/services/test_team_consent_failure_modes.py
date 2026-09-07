"""What the consent surface does when a step fails (fm#1365 review, section A).

The happy paths are pinned next door in ``test_team_invitations.py``. This
module is about the second half of every one of those sequences: the write that
does not land, the two callers who arrive together, the row that elapsed while
somebody was reading it. Each test here was red before the fix it names.

The theme is **which direction a partial failure falls in**. An invitation is a
one-shot token: consume it before the membership exists and a failure leaves the
invitee with no membership and no way to retry, because the only thing that
could have let them retry is the row that was just spent. Every fix below moves
the failure into the recoverable direction — retryable, idempotent, or refused
before anything was written.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest

from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    User,
)
from faultmaven.models.interfaces_user import TeamInvitationStatus
from faultmaven.modules.auth.domain.services.team_service import (
    REASON_INVITATION_EXPIRED,
    TEAM_ROLE_ADMIN,
    TeamService,
)
from faultmaven.modules.auth.exceptions import TeamOperationRefused
from tests.unit.modules.auth.conftest import FakeEnterpriseRepository, utcnow
from tests.unit.modules.auth.services.test_team_invitations import (
    ACME,
    ALICE,
    BOB,
    MALLORY,
    FakeTeamRepository,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _account(user_id: str, email: str, enterprise_id: str = ACME) -> User:
    now = utcnow()
    return User(
        user_id=user_id,
        username=user_id,
        email=email,
        display_name=user_id,
        enterprise_id=enterprise_id,
        created_at=now,
        updated_at=now,
    )


async def _build(accounts=(), ttl_days: Optional[int] = None):
    """A service over the real in-memory user store and the team fake.

    ``InMemoryUserRepository`` rather than a bespoke stub, deliberately: its
    ``get_by_email`` keys on ``email.lower()``, which is what the PostgreSQL
    repository's ``func.lower()`` predicate does. A double that case-*folded*
    instead — which the first version of these tests had — agrees with the
    service and therefore cannot see A7 at all.
    """
    users = InMemoryUserRepository()
    anchors = {ALICE.user_id: ACME, BOB.user_id: ACME}
    for account in accounts:
        await users.create(account)
        anchors[account.user_id] = account.enterprise_id
    teams = FakeTeamRepository(anchors)
    service = TeamService(
        teams,
        enterprise_repository=FakeEnterpriseRepository({ACME: "acme.com"}),
        user_repository=users,
        invitation_ttl_days=ttl_days,
    )
    return service, teams, users


async def _team_with_offer(service, teams, email: str):
    team = await service.create_team(
        enterprise_id=ACME, creator_user_id=ALICE.user_id, name="payments"
    )
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=email,
    )
    return team, invitation


# =============================================================================
# A1 — the offer must not be spent before the membership exists
# =============================================================================


async def test_a1_a_failed_membership_leaves_the_invitation_retryable():
    """The accept writes the membership FIRST, then stamps the row.

    Consuming the offer first is unrecoverable by construction: the row is
    ``accepted`` with no membership, and every retry answers 409 because the
    only thing that could have authorised the retry was the row just spent.
    Reachable without any exotic failure — the team is soft-deleted between the
    read and the write when its sole member leaves, or the invitee's anchor
    moved after the token was minted.

    Writing the membership first fails in the recoverable direction: the offer
    is still pending, so the invitee simply tries again.
    """
    service, teams, _ = await _build()
    team, invitation = await _team_with_offer(service, teams, BOB.email)
    teams.fail_next_add_member = True

    # ``NotFoundError``, because a membership the database refuses is
    # indistinguishable from a team that is not there — the read shape (D2).
    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.PENDING
    ), "the offer was consumed even though the membership was never written"
    assert not teams.has_member(team.team_id, BOB.user_id)

    joined = await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )
    assert joined.team_id == team.team_id
    assert teams.has_member(team.team_id, BOB.user_id)


async def test_a1_a_failed_stamp_after_the_membership_is_recovered_by_a_retry():
    """The other order of failure: membership written, stamp lost.

    ``add_member`` is an idempotent upsert, so the retry re-writes the same
    membership row and completes the stamp. Nothing is double-counted and the
    invitee ends up exactly where a clean run would have put them.
    """
    service, teams, _ = await _build()
    team, invitation = await _team_with_offer(service, teams, BOB.email)
    teams.fail_next_accept_stamp = True

    with pytest.raises(TeamOperationRefused):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    retried = await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    assert retried.team_id == team.team_id
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.ACCEPTED
    )
    roster = await teams.list_team_members(ACME, team.team_id)
    assert len([m for m in roster if m.user_id == BOB.user_id]) == 1


# =============================================================================
# A6 — one reader settles expiry, so every verb agrees about an elapsed offer
# =============================================================================


async def _elapse(teams, invitation_id):
    teams.invitations[invitation_id] = teams.invitations[invitation_id].model_copy(
        update={"expires_at": utcnow() - timedelta(seconds=1)}
    )


async def test_a6_declining_an_elapsed_offer_records_expired_not_revoked():
    """A withdrawal that never happened must not be written into the record.

    ``revoked_by`` is what tells a decline from an admin's withdrawal, and the
    admin's list is where that shows. Stamping an offer nobody answered as
    ``revoked`` by the invitee puts a decision in the record that no person
    made.
    """
    service, teams, _ = await _build(ttl_days=1)
    _, invitation = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, invitation.invitation_id)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.decline_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 410
    assert caught.value.reason == REASON_INVITATION_EXPIRED
    row = teams.invitations[invitation.invitation_id]
    assert row.status == TeamInvitationStatus.EXPIRED
    assert row.revoked_by is None


async def test_a6_revoking_an_elapsed_offer_records_expired_not_revoked():
    """Same rule from the admin's side, and the same reason."""
    service, teams, _ = await _build(ttl_days=1)
    team, invitation = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, invitation.invitation_id)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.revoke_invitation(
            enterprise_id=ACME,
            team_id=team.team_id,
            invitation_id=invitation.invitation_id,
            actor_user_id=ALICE.user_id,
        )

    assert caught.value.status_code == 410
    row = teams.invitations[invitation.invitation_id]
    assert row.status == TeamInvitationStatus.EXPIRED
    assert row.revoked_by is None


async def test_a6_the_accept_answers_the_same_whoever_read_the_row_first():
    """410, whether or not a list settled the row a moment earlier.

    Two callers, one elapsed offer: before the fix the answer depended on who
    read first — 410 if the invitee accepted straight away, 409
    ``invitation_not_pending`` if any list had already stamped it. One status
    for one fact, because the person holding the invitation is entitled to know
    it lapsed rather than that it "is no longer open".
    """
    service, teams, _ = await _build(ttl_days=1)
    _, first = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, first.invitation_id)

    with pytest.raises(TeamOperationRefused) as accept_first:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=first.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    service2, teams2, _ = await _build(ttl_days=1)
    _, second = await _team_with_offer(service2, teams2, BOB.email)
    await _elapse(teams2, second.invitation_id)
    await service2.list_my_invitations(
        enterprise_id=ACME, user_id=BOB.user_id, email=BOB.email
    )
    with pytest.raises(TeamOperationRefused) as list_first:
        await service2.accept_invitation(
            enterprise_id=ACME,
            invitation_id=second.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert accept_first.value.status_code == list_first.value.status_code == 410
    assert accept_first.value.reason == list_first.value.reason
    assert str(accept_first.value) == str(list_first.value)


# =============================================================================
# A7 — the address key must match the index the account lookup uses
# =============================================================================


async def test_a7_an_address_whose_casefold_differs_from_its_lower_still_resolves():
    """``casefold`` is not the repository's index, and the gap is a security hole.

    ``get_by_email`` matches on ``func.lower()``; the writer case-*folded*. For
    any address where the two differ — ``MAẞE@`` lowers to ``maße@`` but folds
    to ``masse@`` — the account lookup misses, and with it BOTH rules that
    depend on finding an account: rule 3 (an account anchored to another
    enterprise must be refused) and the already-a-member check. The invitation
    is created unresolved for an address that has an account, and nothing looks
    wrong until somebody cannot accept it.

    Pinned through the already-a-member check because that one is observable
    from outside: it must fire, which it can only do if the lookup hit.
    """
    account = _account("user-masse", "maße@acme.com")
    service, teams, _ = await _build(accounts=[account])
    team = await service.create_team(
        enterprise_id=ACME, creator_user_id=ALICE.user_id, name="payments"
    )
    assert await teams.add_member(ACME, team.team_id, account.user_id, TEAM_ROLE_ADMIN)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email="MAẞE@acme.com",
        )

    assert caught.value.reason == "already_a_member", (
        "the account lookup missed, so the already-a-member check never ran — "
        "which means rule 3's anchored-elsewhere refusal would have missed too"
    )


async def test_a7_an_existing_account_is_named_on_the_invitation_for_such_an_address():
    """The same miss, seen from the other side: ``invited_user_id`` stays NULL."""
    account = _account("user-masse", "maße@acme.com")
    service, teams, _ = await _build(accounts=[account])
    team = await service.create_team(
        enterprise_id=ACME, creator_user_id=ALICE.user_id, name="payments"
    )

    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="MAẞE@acme.com",
    )

    assert invitation.invited_user_id == account.user_id


# =============================================================================
# A8 — a soft-deleted team must not leave live offers behind
# =============================================================================


async def test_a8_soft_deleting_a_team_revokes_its_pending_invitations():
    """An offer to a team nobody can see is unanswerable and undeclinable.

    Before the fix it listed with a null ``team_name``, accepted into a 404
    forever, and declining it wrote a withdrawal against a team that no longer
    exists. The sole member leaving is the ordinary way to reach it — no race
    required.
    """
    service, teams, _ = await _build()
    team, invitation = await _team_with_offer(service, teams, BOB.email)

    await service.leave_team(
        enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
    )

    assert await teams.get_team(ACME, team.team_id) is None
    row = teams.invitations[invitation.invitation_id]
    assert row.status != TeamInvitationStatus.PENDING, (
        "the team is gone but its offer is still live: the invitee sees an "
        "invitation they can neither accept nor decline"
    )
    assert (
        await service.list_my_invitations(
            enterprise_id=ACME, user_id=BOB.user_id, email=BOB.email
        )
        == []
    )


# =============================================================================
# D1 — the accept writes both rows or neither
# =============================================================================
#
# The first version stamped the offer and then upserted the membership; the
# second (this reviewer's own instruction) swapped them. Both are wrong, in
# opposite directions, and the tests below are the pair that shows why: each
# ordering passes one of them and fails the other. Only a transaction passes
# both.


async def test_d1_a_revoke_landing_mid_accept_leaves_no_membership():
    """The invariant the whole surface exists to hold: a withdrawn offer grants
    nothing.

    Membership-first fails exactly here. The upsert lands, the stamp then finds
    no PENDING row because the offer was withdrawn, the caller is told 409 — and
    a ``team_members`` row remains for somebody the team never consented to
    admit. Nobody sees it: the invitee gets an error, the admin gets no signal,
    and the row is indistinguishable from a legitimate membership afterwards.

    Asserting the 409 alone would pass against the broken ordering, which is why
    the membership assertion is the point of this test and not decoration.
    """
    service, teams, _ = await _build()
    team, invitation = await _team_with_offer(service, teams, BOB.email)
    # The offer is answered inside the transaction — the shape a concurrent
    # revoke presents to it.
    teams.fail_next_accept_stamp = True

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 409
    assert not teams.has_member(team.team_id, BOB.user_id), (
        "the accept left a membership behind for an offer it then refused — a "
        "member of a team nobody consented to admit"
    )


async def test_d1_a_refused_membership_leaves_the_offer_answerable():
    """The other direction, which stamp-first fails.

    If the membership cannot be written — the team retired between the read and
    the write, the invitee's anchor moved after the token was minted — the offer
    must still be pending, because a spent one-shot token cannot authorise the
    retry that would fix it.
    """
    service, teams, _ = await _build()
    team, invitation = await _team_with_offer(service, teams, BOB.email)
    teams.fail_next_add_member = True

    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.PENDING
    )
    assert not teams.has_member(team.team_id, BOB.user_id)

    joined = await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )
    assert joined.team_id == team.team_id
    assert teams.has_member(team.team_id, BOB.user_id)


# =============================================================================
# D3 — entitlement is decided before anything is written
# =============================================================================


async def test_d3_an_unentitled_caller_cannot_settle_someone_elses_invitation():
    """A refusal must not be reached through a side effect.

    Settling expiry inside the shared read made every caller's first act a
    write: any authenticated account in the enterprise could name an invitation
    id that was not theirs, mutate that row to ``expired``, and be told 404 —
    and a team-A admin could do it to team-B's offer. The row is checked
    afterwards because "the caller was refused" and "the row is unchanged" are
    two different claims and only the second one is about the defect.
    """
    service, teams, _ = await _build(ttl_days=1)
    _, invitation = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, invitation.invitation_id)
    before = teams.invitations[invitation.invitation_id]

    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=MALLORY.user_id,
            email=MALLORY.email,
        )
    with pytest.raises(NotFoundError):
        await service.decline_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=MALLORY.user_id,
            email=MALLORY.email,
        )

    assert (
        teams.invitations[invitation.invitation_id] == before
    ), "an unentitled caller changed the row it was refused access to"


async def test_d3_the_entitled_caller_still_settles_it():
    """The control: moving the settle must not have removed it.

    Without this, D3's fix is satisfiable by never settling at all — which would
    silently undo A6 and put the verbs back to disagreeing about an elapsed
    offer.
    """
    service, teams, _ = await _build(ttl_days=1)
    _, invitation = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, invitation.invitation_id)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 410
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.EXPIRED
    )


# =============================================================================
# D4 — only the rows the UPDATE moved are reported as expired
# =============================================================================


async def test_d4_a_row_answered_during_the_settle_is_not_reported_expired():
    """410 for an invitation that was in fact accepted.

    ``_settle_all`` decided which rows were elapsed, wrote them, and then
    rebuilt *its own candidate list* as EXPIRED — so a row accepted between the
    read and the UPDATE, which the pending predicate correctly skipped, was
    still handed back to the caller as expired. That is the same "depends who
    read first" answer A6 existed to remove, one layer down.

    Simulated by having the store refuse to move the row, which is exactly what
    the pending predicate does when somebody else got there first.
    """
    service, teams, _ = await _build(ttl_days=1)
    _, invitation = await _team_with_offer(service, teams, BOB.email)
    await _elapse(teams, invitation.invitation_id)
    # Answered by somebody else in the window: no longer PENDING, so the
    # expiring UPDATE will match nothing.
    teams.invitations[invitation.invitation_id] = teams.invitations[
        invitation.invitation_id
    ].model_copy(update={"status": TeamInvitationStatus.ACCEPTED})

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 409, (
        "the caller was told the invitation expired, but it had been accepted "
        "— the settle reported a row its UPDATE never touched"
    )
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.ACCEPTED
    )


# =============================================================================
# D5 — retiring a team does not invent a withdrawal
# =============================================================================


async def test_d5_retirement_expires_elapsed_offers_and_revokes_live_ones():
    """Both in one retirement, because the split is the whole assertion.

    ``revoked_by`` exists to say who ended an offer. Stamping an offer that had
    already run out as ``revoked`` by the leaver records a withdrawal nobody
    performed, against a person who was answering a different question — and
    adds a third writer of that column, which the published contract does not
    describe.
    """
    service, teams, _ = await _build(ttl_days=1)
    team = await service.create_team(
        enterprise_id=ACME, creator_user_id=ALICE.user_id, name="payments"
    )
    live = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    elapsed = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="gone@acme.com",
    )
    await _elapse(teams, elapsed.invitation_id)

    await service.leave_team(
        enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
    )

    ended = teams.invitations[live.invitation_id]
    lapsed = teams.invitations[elapsed.invitation_id]
    assert ended.status == TeamInvitationStatus.REVOKED
    assert ended.revoked_by == ALICE.user_id
    assert lapsed.status == TeamInvitationStatus.EXPIRED
    assert (
        lapsed.revoked_by is None
    ), "an offer the clock ended was recorded as withdrawn by the leaver"
