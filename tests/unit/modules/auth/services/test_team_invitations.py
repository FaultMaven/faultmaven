"""The invitation rule, row by row (ADR-017 D3 + D4).

A team forms by consent: any account may create one and is its team admin, the
admin offers an address a place, and the invitee accepts. The offer is decided
**by domain**, so nothing on this surface enumerates accounts — and that is the
property most of this module exists to pin, because it is the one that fails
silently. A refusal that distinguished "no account at that address" from "an
account anchored elsewhere" would still look correct in every other respect,
and would answer "who works here?" to anybody who can create a team, which is
everybody.

The rules, as the spec states them, with the test that holds each:

1. the caller's enterprise is personal (``domain`` is NULL — an island): every
   invitation refused, ``enterprise_is_personal``;
2. the address's domain is not the enterprise's: refused,
   ``address_outside_enterprise_domain``;
3. domain matches and an account exists — in this enterprise, the invitation
   names it; anchored elsewhere, refused with the SAME answer as (2);
4. domain matches and no account: created unresolved, and resolved by the
   sign-up hook if and only if that address lands in THIS enterprise;
5. already a member → 409; a live offer for the same address on the same team
   is returned rather than duplicated;
6. expiry is lazy and enforced on read and on accept; an invitation is
   answerable only by its invitee.

The repositories are fakes rather than mocks, and they enforce the two
invariants the real ones enforce — ``add_member`` refuses a member anchored to
another enterprise, and the pending offer per (team, address) is unique. A
``Mock`` would accept any call and let a rule that never fired look like a rule
that passed.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pytest

from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    User,
)
from faultmaven.models.interfaces_user import (
    LeaveOutcome,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamMember,
    TeamNameTakenError,
)
from faultmaven.modules.auth.domain.services.team_service import (
    REASON_ADDRESS_OUTSIDE_DOMAIN,
    REASON_ALREADY_A_MEMBER,
    REASON_ENTERPRISE_IS_PERSONAL,
    REASON_INVITATION_EXPIRED,
    REASON_LAST_ADMIN_CANNOT_LEAVE,
    REASON_NOT_A_TEAM_ADMIN,
    REASON_TEAM_NAME_TAKEN,
    TEAM_ROLE_ADMIN,
    TEAM_ROLE_MEMBER,
    TeamService,
)
from faultmaven.modules.auth.exceptions import TeamOperationRefused
from tests.unit.modules.auth.conftest import FakeEnterpriseRepository

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACME = "ent-acme"
OTHER = "ent-other"
PERSONAL = "ent-personal"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeAccount:
    """The three fields the rule reads off an account."""

    def __init__(self, user_id: str, email: str, enterprise_id: str):
        self.user_id = user_id
        self.email = email
        self.enterprise_id = enterprise_id


class FakeTeamRepository:
    """An in-memory team store that keeps the invariants that matter.

    * ``create_team_with_admin`` is atomic — either both rows or neither — and
      refuses a creator anchored to another enterprise, exactly as the SQL
      implementation does with its explicit anchor comparison;
    * one live team name per enterprise, and one PENDING invitation per (team,
      address), both of which the real schema enforces with a **partial** unique
      index — so a retired team and an answered offer free their keys;
    * ``leave_team`` decides the last-admin rule and performs the removal in one
      step, because that is the only version of the rule that is true under
      concurrency.

    ``fail_next_add_member`` / ``fail_next_accept_stamp`` are one-shot fault
    injections. They are how the accept's ordering is tested at all: the failure
    they simulate (a team retired between the read and the write, an anchor that
    moved after the token was minted) is real but not reachable from a single
    linear test.
    """

    def __init__(self, account_enterprise: Dict[str, str]):
        self.teams: Dict[str, Team] = {}
        self.members: Dict[Tuple[str, str], TeamMember] = {}
        self.invitations: Dict[str, TeamInvitation] = {}
        self.account_enterprise = dict(account_enterprise)
        self.fail_next_add_member = False
        self.fail_next_accept_stamp = False

    # -- teams -------------------------------------------------------------- #

    def _live_name_clash(self, team: Team) -> bool:
        return any(
            other.enterprise_id == team.enterprise_id
            and other.name == team.name
            and other.deleted_at is None
            and other.team_id != team.team_id
            for other in self.teams.values()
        )

    async def create_team(self, team: Team) -> Team:
        if self._live_name_clash(team):
            raise TeamNameTakenError(team.name)
        self.teams[team.team_id] = team
        return team

    async def create_team_with_admin(
        self, team: Team, admin_user_id: str, team_role: Optional[str]
    ) -> Optional[Team]:
        if self.account_enterprise.get(admin_user_id) != team.enterprise_id:
            return None
        if self._live_name_clash(team):
            raise TeamNameTakenError(team.name)
        self.teams[team.team_id] = team
        self.members[(team.team_id, admin_user_id)] = TeamMember(
            user_id=admin_user_id,
            team_id=team.team_id,
            team_role=team_role,
            joined_at=_now(),
        )
        return team

    async def get_team(self, enterprise_id: str, team_id: str) -> Optional[Team]:
        team = self.teams.get(team_id)
        if (
            team is None
            or team.deleted_at is not None
            or team.enterprise_id != enterprise_id
        ):
            return None
        return team

    async def get_team_with_members(self, enterprise_id: str, team_id: str):
        team = await self.get_team(enterprise_id, team_id)
        if team is None:
            return None, []
        return team, await self.list_team_members(enterprise_id, team_id)

    async def get_team_names(self, enterprise_id: str, team_ids) -> Dict[str, str]:
        names = {}
        for team_id in team_ids:
            team = await self.get_team(enterprise_id, team_id)
            if team is not None:
                names[team_id] = team.name
        return names

    async def delete_team(self, enterprise_id: str, team_id: str) -> bool:
        team = await self.get_team(enterprise_id, team_id)
        if team is None:
            return False
        self.teams[team_id] = team.model_copy(update={"deleted_at": _now()})
        return True

    async def leave_team(
        self, enterprise_id: str, team_id: str, user_id: str, admin_role: str
    ) -> LeaveOutcome:
        team = await self.get_team(enterprise_id, team_id)
        if team is None or (team_id, user_id) not in self.members:
            return LeaveOutcome.ABSENT
        roster = await self.list_team_members(enterprise_id, team_id)
        leaver = next(m for m in roster if m.user_id == user_id)
        others = [m for m in roster if m.user_id != user_id]
        if (
            others
            and leaver.team_role == admin_role
            and not any(m.team_role == admin_role for m in others)
        ):
            return LeaveOutcome.LAST_ADMIN
        del self.members[(team_id, user_id)]
        if others:
            return LeaveOutcome.LEFT
        await self.delete_team(enterprise_id, team_id)
        now = _now()
        for invitation_id, row in list(self.invitations.items()):
            if row.team_id == team_id and row.status == TeamInvitationStatus.PENDING:
                self.invitations[invitation_id] = row.model_copy(
                    update={
                        "status": TeamInvitationStatus.REVOKED,
                        "revoked_by": user_id,
                        "revoked_at": now,
                    }
                )
        return LeaveOutcome.LEFT_AND_RETIRED

    async def add_member(
        self, team_id: str, user_id: str, team_role: Optional[str] = None
    ) -> bool:
        if self.fail_next_add_member:
            self.fail_next_add_member = False
            return False
        team = self.teams.get(team_id)
        anchor = self.account_enterprise.get(user_id)
        if team is None or team.deleted_at is not None or anchor is None:
            return False
        if team.enterprise_id != anchor:
            return False
        self.members[(team_id, user_id)] = TeamMember(
            user_id=user_id,
            team_id=team_id,
            team_role=team_role,
            joined_at=_now(),
        )
        return True

    async def remove_member(self, team_id: str, user_id: str) -> bool:
        return self.members.pop((team_id, user_id), None) is not None

    async def list_team_members(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamMember]:
        team = self.teams.get(team_id)
        if team is None or team.enterprise_id != enterprise_id:
            return []
        return [member for (tid, _), member in self.members.items() if tid == team_id]

    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        return (team_id, user_id) in self.members

    # -- invitations -------------------------------------------------------- #

    async def create_invitation(self, invitation: TeamInvitation) -> TeamInvitation:
        clash = await self.find_pending_invitation(
            invitation.enterprise_id, invitation.team_id, invitation.email
        )
        assert clash is None, (
            "the partial unique index admits one PENDING offer per address per "
            "team; this write would have made two"
        )
        self.invitations[invitation.invitation_id] = invitation
        return invitation

    async def get_invitation(
        self, enterprise_id: str, invitation_id: str
    ) -> Optional[TeamInvitation]:
        row = self.invitations.get(invitation_id)
        if row is None or row.enterprise_id != enterprise_id:
            return None
        return row

    async def find_pending_invitation(
        self, enterprise_id: str, team_id: str, email: str
    ) -> Optional[TeamInvitation]:
        for row in self.invitations.values():
            if (
                row.enterprise_id == enterprise_id
                and row.team_id == team_id
                and row.email == email
                and row.status == TeamInvitationStatus.PENDING
            ):
                return row
        return None

    async def list_team_invitations(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamInvitation]:
        return [
            row
            for row in self.invitations.values()
            if row.team_id == team_id and row.enterprise_id == enterprise_id
        ]

    async def list_invitations_for_invitee(
        self, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        return [
            row
            for row in self.invitations.values()
            if row.enterprise_id == enterprise_id
            and row.status == TeamInvitationStatus.PENDING
            and (
                row.invited_user_id == user_id
                or (row.invited_user_id is None and row.email == email)
            )
        ]

    async def mark_invitation_accepted(
        self, invitation_id: str, user_id: str, at: datetime
    ) -> bool:
        if self.fail_next_accept_stamp:
            self.fail_next_accept_stamp = False
            return False
        row = self.invitations.get(invitation_id)
        if row is None or row.status != TeamInvitationStatus.PENDING:
            return False
        self.invitations[invitation_id] = row.model_copy(
            update={
                "status": TeamInvitationStatus.ACCEPTED,
                "invited_user_id": user_id,
                "accepted_at": at,
            }
        )
        return True

    async def mark_invitation_revoked(
        self, invitation_id: str, by_user_id: str, at: datetime
    ) -> bool:
        row = self.invitations.get(invitation_id)
        if row is None or row.status != TeamInvitationStatus.PENDING:
            return False
        self.invitations[invitation_id] = row.model_copy(
            update={
                "status": TeamInvitationStatus.REVOKED,
                "revoked_by": by_user_id,
                "revoked_at": at,
            }
        )
        return True

    async def expire_invitations(self, invitation_ids) -> int:
        expired = 0
        for invitation_id in invitation_ids:
            row = self.invitations.get(invitation_id)
            if row is None or row.status != TeamInvitationStatus.PENDING:
                continue
            self.invitations[invitation_id] = row.model_copy(
                update={"status": TeamInvitationStatus.EXPIRED}
            )
            expired += 1
        return expired

    async def resolve_invitations_for_account(
        self, enterprise_id: str, email: str, user_id: str
    ) -> int:
        resolved = 0
        for invitation_id, row in list(self.invitations.items()):
            if (
                row.enterprise_id == enterprise_id
                and row.email == email
                and row.invited_user_id is None
                and row.status == TeamInvitationStatus.PENDING
            ):
                self.invitations[invitation_id] = row.model_copy(
                    update={"invited_user_id": user_id}
                )
                resolved += 1
        return resolved


# =============================================================================
# The world these tests run in
# =============================================================================
#
# One company enterprise on acme.com, one other company on other.com, and one
# personal enterprise with no domain at all. Alice and Bob are both at acme.com;
# Mallory is at acme.com's *address space* but anchored to the other enterprise,
# which is the operator-mapped case rule 3's second half is about.

ALICE = FakeAccount("user-alice", "alice@acme.com", ACME)
BOB = FakeAccount("user-bob", "bob@acme.com", ACME)
MALLORY = FakeAccount("user-mallory", "mallory@acme.com", OTHER)
RIVAL = FakeAccount("user-rival", "rival@other.com", OTHER)
HERMIT = FakeAccount("user-hermit", "hermit@gmail.com", PERSONAL)


def build(
    accounts=(ALICE, BOB, MALLORY, RIVAL, HERMIT), ttl_days: Optional[int] = None
):
    """A service over the shared doubles, plus the team store so tests can look.

    ``InMemoryUserRepository`` rather than a hand-rolled stub: its
    ``get_by_email`` keys on ``email.lower()``, which is what the PostgreSQL
    repository's ``func.lower()`` predicate does. A double that case-*folded*
    would agree with an earlier version of the service and hide the mismatch
    (fm#1365 A7) rather than catch it.
    """
    anchors = {account.user_id: account.enterprise_id for account in accounts}
    teams = FakeTeamRepository(anchors)
    users = InMemoryUserRepository()
    for account in accounts:
        now = _now()
        users._users[account.user_id] = User(
            user_id=account.user_id,
            username=account.user_id,
            email=account.email,
            display_name=account.user_id,
            enterprise_id=account.enterprise_id,
            created_at=now,
            updated_at=now,
        )
        users._email_index[account.email.lower()] = account.user_id
    service = TeamService(
        teams,
        enterprise_repository=FakeEnterpriseRepository(
            {ACME: "acme.com", OTHER: "other.com", PERSONAL: None}
        ),
        user_repository=users,
        invitation_ttl_days=ttl_days,
    )
    return service, teams


async def a_team(service, *, enterprise_id=ACME, creator=ALICE, name="payments"):
    return await service.create_team(
        enterprise_id=enterprise_id,
        creator_user_id=creator.user_id,
        name=name,
    )


# =============================================================================
# Teams: anyone creates, and is its admin (D4)
# =============================================================================


async def test_the_creator_of_a_team_is_its_admin_and_its_only_member():
    """D4's first sentence, and the thing every later rule is gated on."""
    service, teams = build()

    team = await a_team(service)

    assert team.enterprise_id == ACME
    members = await teams.list_team_members(ACME, team.team_id)
    assert [(m.user_id, m.team_role) for m in members] == [
        (ALICE.user_id, TEAM_ROLE_ADMIN)
    ]


async def test_a_team_whose_creator_cannot_join_it_is_never_written_at_all():
    """The creator's anchor must match the team's, and NOTHING is written.

    This used to be two writes and a compensating delete, which left a window —
    and a soft-deleted squatter afterwards, holding its name against a
    then-total unique constraint. The team and the creator's membership are now
    one transaction, so the refusal leaves no row of any kind and the name is
    free for the next attempt.
    """
    service, teams = build()

    with pytest.raises(NotFoundError):
        await service.create_team(
            enterprise_id=OTHER,  # Alice is anchored to ACME
            creator_user_id=ALICE.user_id,
            name="not-mine",
        )

    assert teams.teams == {}, "a refused creation left a row behind"


async def test_a_team_in_another_enterprise_is_absent_not_forbidden():
    """404, never 403 — a 403 confirms the id names something (ADR-017 D2)."""
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(NotFoundError):
        await service.list_members(
            enterprise_id=OTHER, team_id=team.team_id, user_id=MALLORY.user_id
        )


async def test_a_non_member_of_a_team_in_their_own_enterprise_gets_the_same_404():
    """Same enterprise, no membership: still absent.

    Who is on a team is what a team shares, so the roster is readable by the
    people who agreed to share it. Answering 403 here would tell a colleague
    which teams exist, which is the enumeration the 404 shape prevents.
    """
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(NotFoundError):
        await service.list_members(
            enterprise_id=ACME, team_id=team.team_id, user_id=BOB.user_id
        )


# =============================================================================
# Rule 1 — a personal enterprise is an island
# =============================================================================


async def test_rule_1_a_personal_enterprise_can_invite_nobody():
    """No address can land in a private enterprise, so none may be offered one."""
    service, _ = build()
    team = await a_team(service, enterprise_id=PERSONAL, creator=HERMIT)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=PERSONAL,
            team_id=team.team_id,
            actor_user_id=HERMIT.user_id,
            email="friend@gmail.com",
        )

    assert caught.value.status_code == 403
    assert caught.value.reason == REASON_ENTERPRISE_IS_PERSONAL


async def test_rule_1_refuses_even_an_address_at_the_inviters_own_domain():
    """Two gmail.com accounts are never one enterprise (D3/D4's accepted cost).

    The refusal is the *personal* one, not the domain one: the rule that fires
    is "this enterprise admits nobody", and it fires before the address is
    looked at, which is why the inviter's own domain does not help.
    """
    service, _ = build()
    team = await a_team(service, enterprise_id=PERSONAL, creator=HERMIT)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=PERSONAL,
            team_id=team.team_id,
            actor_user_id=HERMIT.user_id,
            email=HERMIT.email,
        )

    assert caught.value.reason == REASON_ENTERPRISE_IS_PERSONAL


# =============================================================================
# Rules 2 and 3 — the domain rule, and the pairing that hides account existence
# =============================================================================


async def test_rule_2_an_address_on_another_domain_is_refused():
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email="someone@other.com",
        )

    assert caught.value.status_code == 403
    assert caught.value.reason == REASON_ADDRESS_OUTSIDE_DOMAIN


async def test_rule_3_an_account_in_the_enterprise_is_named_on_the_invitation():
    service, teams = build()
    team = await a_team(service)

    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="BOB@Acme.com",
    )

    assert invitation.invited_user_id == BOB.user_id
    assert invitation.invited_by == ALICE.user_id
    assert invitation.status == TeamInvitationStatus.PENDING
    # Case-folded on the way in: the sign-up hook matches on exactly this
    # spelling, so a stored "BOB@Acme.com" would be an offer nobody can resolve.
    assert invitation.email == "bob@acme.com"
    assert teams.invitations[invitation.invitation_id].email == "bob@acme.com"


async def test_rule_2_an_off_domain_address_answers_the_same_with_or_without_an_account():
    """THE pairing. It is what keeps the endpoint from being an existence oracle.

    ``rival@other.com`` has an account; ``ghost@other.com`` does not. Both are
    off the enterprise's domain, so rule 2 refuses both — and the two refusals
    must be identical in status, reason AND message. Comparing all three rather
    than the reason alone is deliberate: a message naming the account would leak
    exactly as loudly as a different slug would.

    The reason it matters here rather than only at rule 3 is that rule 2 fires
    *before* any account lookup, so the two calls must be indistinguishable by
    construction; this test is what says so out loud, and what would fail if
    somebody "helpfully" moved the lookup earlier.
    """
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(TeamOperationRefused) as has_account:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email=RIVAL.email,
        )
    with pytest.raises(TeamOperationRefused) as no_account:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email="ghost@other.com",
        )

    assert has_account.value.status_code == no_account.value.status_code == 403
    assert has_account.value.reason == no_account.value.reason
    assert str(has_account.value) == str(no_account.value)
    assert has_account.value.reason == REASON_ADDRESS_OUTSIDE_DOMAIN


async def test_rule_3_an_elsewhere_anchored_account_answers_exactly_as_rule_2_does():
    """The second half of the pairing, and the subtler one.

    ``mallory@acme.com`` is on the enterprise's OWN domain — rule 2 lets it
    through — but the account behind it is anchored to another enterprise (the
    operator-mapped case). It must answer exactly what an address on a foreign
    domain answers, because the only thing that separates the two calls is a
    fact about where somebody's account lives.

    The contrast that gives this test its teeth is
    ``test_rule_4_an_address_with_no_account_gets_an_unresolved_invitation``:
    the same domain with NO account is *accepted*. So the elsewhere-anchored
    refusal cannot be inferred from the address alone — it is the one place the
    account lookup changes the answer, and it must change it into the answer a
    caller could already have predicted.
    """
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(TeamOperationRefused) as elsewhere:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email=MALLORY.email,
        )
    with pytest.raises(TeamOperationRefused) as off_domain:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email="someone@other.com",
        )

    assert elsewhere.value.status_code == off_domain.value.status_code == 403
    assert elsewhere.value.reason == off_domain.value.reason
    assert str(elsewhere.value) == str(off_domain.value)
    assert elsewhere.value.reason == REASON_ADDRESS_OUTSIDE_DOMAIN


# =============================================================================
# Rule 4 — an address with no account, and the sign-up hook
# =============================================================================


async def test_rule_4_an_address_with_no_account_gets_an_unresolved_invitation():
    service, _ = build()
    team = await a_team(service)

    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )

    assert invitation.invited_user_id is None
    assert invitation.status == TeamInvitationStatus.PENDING


async def test_rule_4_signing_up_into_this_enterprise_resolves_the_invitation():
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )

    resolved = await service.resolve_invitations_for_account(
        enterprise_id=ACME, email="NewHire@ACME.com", user_id="user-newhire"
    )

    assert resolved == 1
    assert teams.invitations[invitation.invitation_id].invited_user_id == (
        "user-newhire"
    )


async def test_rule_4_the_hook_is_idempotent():
    """A second login must not re-resolve, re-stamp, or count anything again."""
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )
    await service.resolve_invitations_for_account(
        enterprise_id=ACME, email="newhire@acme.com", user_id="user-newhire"
    )

    again = await service.resolve_invitations_for_account(
        enterprise_id=ACME, email="newhire@acme.com", user_id="user-someone-else"
    )

    assert again == 0
    assert teams.invitations[invitation.invitation_id].invited_user_id == (
        "user-newhire"
    )


async def test_rule_4_signing_up_into_a_different_enterprise_never_resolves_it():
    """The offer stays pending where it was issued, to expire there.

    Nothing crosses an enterprise line (D2), so an address that lands somewhere
    else is not the account this invitation was for — even though the address
    matches exactly.
    """
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )

    resolved = await service.resolve_invitations_for_account(
        enterprise_id=OTHER, email="newhire@acme.com", user_id="user-newhire"
    )

    assert resolved == 0
    row = teams.invitations[invitation.invitation_id]
    assert row.invited_user_id is None
    assert row.status == TeamInvitationStatus.PENDING


# =============================================================================
# Rule 5 — already a member, and the idempotent re-invite
# =============================================================================


async def test_rule_5_inviting_an_existing_member_is_a_conflict():
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=ALICE.user_id,
            email=ALICE.email,
        )

    assert caught.value.status_code == 409
    assert caught.value.reason == REASON_ALREADY_A_MEMBER


async def test_rule_5_re_inviting_returns_the_live_offer_rather_than_a_second_row():
    service, teams = build()
    team = await a_team(service)

    first = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="bob@acme.com",
    )
    second = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="BOB@ACME.COM",
    )

    assert second.invitation_id == first.invitation_id
    assert len(teams.invitations) == 1


async def test_re_inviting_after_expiry_mints_a_fresh_offer_and_settles_the_old_one():
    """The old offer must be stamped, not merely stepped over.

    The partial unique index admits one PENDING row per (team, address), so a
    replacement written beside an unstamped expired one would be refused by the
    database. The fake asserts that invariant, which is what makes this test
    about the schema and not only about the service.
    """
    service, teams = build(ttl_days=1)
    team = await a_team(service)
    stale = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="bob@acme.com",
    )
    teams.invitations[stale.invitation_id] = teams.invitations[
        stale.invitation_id
    ].model_copy(update={"expires_at": _now() - timedelta(days=2)})

    fresh = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="bob@acme.com",
    )

    assert fresh.invitation_id != stale.invitation_id
    assert teams.invitations[stale.invitation_id].status == (
        TeamInvitationStatus.EXPIRED
    )
    assert fresh.status == TeamInvitationStatus.PENDING


# =============================================================================
# Rule 6 — expiry, and who may answer an invitation
# =============================================================================


async def test_rule_6_the_ttl_is_stamped_on_the_row_at_creation():
    """Expiry is a property of the offer, not of the setting at read time.

    Reading it off the row is what makes lowering the TTL affect only new
    invitations — and what makes the read path and the accept path agree without
    a sweeper between them.
    """
    service, _ = build(ttl_days=3)
    team = await a_team(service)

    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="bob@acme.com",
    )

    elapsed = invitation.expires_at - invitation.created_at
    assert elapsed == timedelta(days=3)


async def test_rule_6_accepting_an_expired_invitation_is_gone_and_stamps_the_row():
    service, teams = build(ttl_days=1)
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    teams.invitations[invitation.invitation_id] = teams.invitations[
        invitation.invitation_id
    ].model_copy(update={"expires_at": _now() - timedelta(seconds=1)})

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 410
    assert caught.value.reason == REASON_INVITATION_EXPIRED
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.EXPIRED
    )
    assert not await teams.is_team_member(team.team_id, BOB.user_id)


async def test_rule_6_an_expired_invitation_is_stamped_and_hidden_on_read():
    """Lazy expiry, settled by whoever reads it — the invitee's own list."""
    service, teams = build(ttl_days=1)
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    teams.invitations[invitation.invitation_id] = teams.invitations[
        invitation.invitation_id
    ].model_copy(update={"expires_at": _now() - timedelta(seconds=1)})

    listed = await service.list_my_invitations(
        enterprise_id=ACME, user_id=BOB.user_id, email=BOB.email
    )

    assert listed == []
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.EXPIRED
    )
    assert team.team_id  # the team itself is untouched


async def test_rule_6_only_the_invitee_may_accept():
    """Somebody else's invitation is absent, not forbidden.

    A 403 would confirm the id names a live invitation, and the id is the whole
    of what an accept needs — so the refusal has to be the read shape.
    """
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )

    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=MALLORY.user_id,
            email=MALLORY.email,
        )

    assert not await teams.is_team_member(team.team_id, MALLORY.user_id)


async def test_rule_6_an_invitation_in_another_enterprise_cannot_be_accepted():
    """The forged accept: a valid token, somebody else's enterprise's id."""
    service, _ = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )

    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=OTHER,
            invitation_id=invitation.invitation_id,
            user_id=MALLORY.user_id,
            email=MALLORY.email,
        )


async def test_an_unresolved_invitation_is_answerable_by_the_matching_address():
    """The other addressing arm: no ``invited_user_id`` yet, so the email decides.

    This is the path somebody invited before they signed up takes, and it is why
    the address is stored case-folded on both sides.
    """
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )
    newcomer = FakeAccount("user-newhire", "NewHire@Acme.com", ACME)
    teams.account_enterprise[newcomer.user_id] = ACME

    joined = await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=newcomer.user_id,
        email=newcomer.email,
    )

    assert joined.team_id == team.team_id
    members = {
        m.user_id: m.team_role
        for m in await teams.list_team_members(ACME, team.team_id)
    }
    assert members[newcomer.user_id] == TEAM_ROLE_MEMBER


async def test_an_unresolved_invitation_is_not_answerable_by_a_different_address():
    service, _ = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email="newhire@acme.com",
    )

    with pytest.raises(NotFoundError):
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )


async def test_accepting_creates_the_membership_and_nothing_before_it_does():
    """A pending invitation grants nothing — the whole of D4's consent claim."""
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    assert not await teams.is_team_member(team.team_id, BOB.user_id)

    joined = await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    assert joined.team_id == team.team_id
    assert await teams.is_team_member(team.team_id, BOB.user_id)
    row = teams.invitations[invitation.invitation_id]
    assert row.status == TeamInvitationStatus.ACCEPTED
    assert row.accepted_at is not None


async def test_declining_records_who_declined_and_grants_nothing():
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )

    await service.decline_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    row = teams.invitations[invitation.invitation_id]
    assert row.status == TeamInvitationStatus.REVOKED
    assert row.revoked_by == BOB.user_id
    assert not await teams.is_team_member(team.team_id, BOB.user_id)


async def test_an_answered_invitation_cannot_be_answered_again():
    service, _ = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 409


# =============================================================================
# Who may manage a team's invitations
# =============================================================================


async def test_a_member_who_is_not_an_admin_cannot_invite():
    """403, not 404: they can see the team, so hiding it would say nothing."""
    service, _ = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    with pytest.raises(TeamOperationRefused) as caught:
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=BOB.user_id,
            email="newhire@acme.com",
        )

    assert caught.value.status_code == 403
    assert caught.value.reason == REASON_NOT_A_TEAM_ADMIN


async def test_a_non_member_inviting_gets_the_read_shape():
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(NotFoundError):
        await service.invite(
            enterprise_id=ACME,
            team_id=team.team_id,
            actor_user_id=BOB.user_id,
            email="newhire@acme.com",
        )


async def test_revoking_an_invitation_from_another_team_is_absent():
    """The team id in the path must own the invitation id in the path."""
    service, _ = build()
    payments = await a_team(service, name="payments")
    platform = await a_team(service, name="platform")
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=payments.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )

    with pytest.raises(NotFoundError):
        await service.revoke_invitation(
            enterprise_id=ACME,
            team_id=platform.team_id,
            invitation_id=invitation.invitation_id,
            actor_user_id=ALICE.user_id,
        )


async def test_a_revoked_invitation_can_no_longer_be_accepted():
    service, _ = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.revoke_invitation(
        enterprise_id=ACME,
        team_id=team.team_id,
        invitation_id=invitation.invitation_id,
        actor_user_id=ALICE.user_id,
    )

    with pytest.raises(TeamOperationRefused) as caught:
        await service.accept_invitation(
            enterprise_id=ACME,
            invitation_id=invitation.invitation_id,
            user_id=BOB.user_id,
            email=BOB.email,
        )

    assert caught.value.status_code == 409


async def test_the_admins_list_reports_and_stamps_an_expired_offer():
    service, teams = build(ttl_days=1)
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    teams.invitations[invitation.invitation_id] = teams.invitations[
        invitation.invitation_id
    ].model_copy(update={"expires_at": _now() - timedelta(seconds=1)})

    listed = await service.list_team_invitations(
        enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
    )

    assert [row.status for row in listed] == [TeamInvitationStatus.EXPIRED]
    assert teams.invitations[invitation.invitation_id].status == (
        TeamInvitationStatus.EXPIRED
    )


# =============================================================================
# Leaving
# =============================================================================


async def test_the_last_admin_cannot_leave_while_other_members_remain():
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    with pytest.raises(TeamOperationRefused) as caught:
        await service.leave_team(
            enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
        )

    assert caught.value.status_code == 409
    assert caught.value.reason == REASON_LAST_ADMIN_CANNOT_LEAVE
    assert await teams.is_team_member(team.team_id, ALICE.user_id)


async def test_a_plain_member_may_always_leave():
    """The control for the rule above: it is about admins, not about leaving."""
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )

    await service.leave_team(
        enterprise_id=ACME, team_id=team.team_id, user_id=BOB.user_id
    )

    assert not await teams.is_team_member(team.team_id, BOB.user_id)
    assert await teams.get_team(ACME, team.team_id) is not None


async def test_the_second_admin_frees_the_first_to_leave():
    """The rule is "no admin left behind", not "the creator is stuck"."""
    service, teams = build()
    team = await a_team(service)
    invitation = await service.invite(
        enterprise_id=ACME,
        team_id=team.team_id,
        actor_user_id=ALICE.user_id,
        email=BOB.email,
    )
    await service.accept_invitation(
        enterprise_id=ACME,
        invitation_id=invitation.invitation_id,
        user_id=BOB.user_id,
        email=BOB.email,
    )
    await teams.add_member(team.team_id, BOB.user_id, TEAM_ROLE_ADMIN)

    await service.leave_team(
        enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
    )

    assert not await teams.is_team_member(team.team_id, ALICE.user_id)
    assert await teams.get_team(ACME, team.team_id) is not None


async def test_the_sole_member_leaving_soft_deletes_the_team():
    """Nobody is stranded, and an empty team is not a sharing unit."""
    service, teams = build()
    team = await a_team(service)

    await service.leave_team(
        enterprise_id=ACME, team_id=team.team_id, user_id=ALICE.user_id
    )

    assert not await teams.is_team_member(team.team_id, ALICE.user_id)
    assert await teams.get_team(ACME, team.team_id) is None
    assert teams.teams[team.team_id].deleted_at is not None


async def test_leaving_a_team_in_another_enterprise_is_absent():
    service, _ = build()
    team = await a_team(service)

    with pytest.raises(NotFoundError):
        await service.leave_team(
            enterprise_id=OTHER, team_id=team.team_id, user_id=ALICE.user_id
        )


# =============================================================================
# Unwired dependencies fail closed
# =============================================================================


async def test_a9_the_service_refuses_to_exist_without_its_repositories():
    """An unwired dependency must not be answerable as "not found".

    Every factory in the composition root answers ``None`` on failure, so a
    ``TeamService`` missing its enterprise repository was a reachable state —
    and it answered **404 for every team in the deployment**, because the
    invitation rule read a domain it could not fetch. A wiring failure wearing
    the shape of "you asked for a row that does not exist" is the one failure
    nobody investigates.

    Refusing to construct moves it to the honest place: ``create_team_service``
    returns ``None``, which is already the deployment-wide "team collaboration
    is not available here" signal, and the surface answers its own 403.
    """
    teams = FakeTeamRepository({ALICE.user_id: ACME})
    enterprises = FakeEnterpriseRepository({ACME: "acme.com"})
    users = InMemoryUserRepository()

    with pytest.raises(ValueError, match="enterprise repository"):
        TeamService(teams, enterprise_repository=None, user_repository=users)
    with pytest.raises(ValueError, match="user repository"):
        TeamService(teams, enterprise_repository=enterprises, user_repository=None)


# =============================================================================
# The sign-up hook's placement (ADR-017 D4, rule 4)
# =============================================================================
#
# Rule 4's whole point is an invitation issued to an address that has NO account
# yet, resolving on that address's first sign-in. These two tests are about
# WHERE the hook hangs off the login, because the first version of it hung off
# the anchor *write* — and a JIT-provisioned account is created already carrying
# ``enterprise_id``, so on the one login rule 4 exists for, there is no anchor
# write and the hook never ran. Nothing else failed: the offer simply sat
# unresolved until it expired.
#
# Driven through ``SSOLoginService._ensure_enterprise_anchor`` rather than
# through ``TeamService``, because the defect was not in the rule — it was in
# which branch of the login the rule was reached from.


class _FakeEnterprise:
    def __init__(self, enterprise_id: str):
        self.enterprise_id = enterprise_id


async def test_the_signup_hook_runs_when_the_account_is_already_anchored():
    """The first login of a JIT-provisioned account writes no anchor.

    ``_provision_user`` stamps ``enterprise_id`` at creation, so by the time the
    anchor step runs the account already matches and there is nothing to move.
    That is the login an invitation issued before the account existed is waiting
    for, so the hook must run there or rule 4 is dead code.
    """
    from faultmaven.modules.auth.domain.services.sso_login_service import (
        SSOLoginService,
    )

    teams = FakeTeamRepository({})
    team_service = TeamService(
        teams,
        enterprise_repository=FakeEnterpriseRepository({ACME: "acme.com"}),
        user_repository=InMemoryUserRepository(),
    )
    service = SSOLoginService.__new__(SSOLoginService)
    # The SERVICE, as the composition root wires it: the hook goes through the
    # domain rule (address key, enterprise short-circuit, idempotence) rather
    # than reaching past it to the repository with a second copy of the key.
    service._teams = team_service
    user = FakeAccount("user-newhire", "NewHire@Acme.com", ACME)
    now = _now()
    teams.invitations["inv-1"] = TeamInvitation(
        invitation_id="inv-1",
        enterprise_id=ACME,
        team_id="team-1",
        email="newhire@acme.com",
        invited_user_id=None,
        status=TeamInvitationStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(days=14),
    )

    admitted = await service._ensure_enterprise_anchor(user, _FakeEnterprise(ACME))

    assert admitted is True
    assert teams.invitations["inv-1"].invited_user_id == "user-newhire", (
        "the sign-up hook did not run on a login that wrote no anchor — which "
        "is every first login of a JIT-provisioned account, and the only login "
        "rule 4 exists for"
    )


async def test_a_failed_invitation_resolution_never_costs_the_login():
    """An unwired or erroring team feature must not sign somebody out.

    Resolving an invitation is not a precondition of having an account: the
    offer stays pending and the next sign-in tries again.
    """
    from faultmaven.modules.auth.domain.services.sso_login_service import (
        SSOLoginService,
    )

    class Exploding:
        async def resolve_invitations_for_account(self, **_kwargs):
            raise RuntimeError("the invitations table is on fire")

    service = SSOLoginService.__new__(SSOLoginService)
    service._teams = Exploding()

    admitted = await service._ensure_enterprise_anchor(
        FakeAccount("user-alice", "alice@acme.com", ACME), _FakeEnterprise(ACME)
    )

    assert admitted is True
