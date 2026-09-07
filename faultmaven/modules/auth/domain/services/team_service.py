"""Team service — teams form by consent (ADR-017 D4).

Two jobs, and they used to be one.

**Resolution** is the read side the KB retrieval paths consume to build the team
arm of a principal's read scope (``build_kb_scope_filter``): agent retrieval and
the KB document inventory route both call ``list_all_user_team_ids`` on the
wired ``team_service``.

**Consent** is the rest of this module: any account may create a team and is its
team admin; the admin invites addresses; the invitee accepts. A pending
invitation grants nothing — it is what keeps a stranger from pulling a colleague
into a team's view without a word.

Scope (ADR-010 / ADR-013 / ADR-017): the core owns the whole of this. The
service is wired only in multi-tenant (Cloud) deployments; standalone leaves it
unwired (``team_service=None``) so team collaboration stays inert (ADR-017 D8:
one enterprise, one default team, one user — there is nobody to invite). Even
when wired, resolution degrades safely: a user with no memberships resolves to
``[]`` and KB scope collapses to ``personal ∪ global``.

Every method that answers a caller takes the caller's ``enterprise_id`` and
uses it as a predicate. That is deliberately belt-and-braces: the RLS policies
already scope every one of these tables on ``app.current_enterprise_id`` under
the limited ``faultmaven_app`` role, but the rule must also hold where RLS does
not exist (SQLite, and any owner-role path), and it is what makes the unit tests
of the domain rule tests of the rule rather than tests of PostgreSQL.

**The refusal shape is part of the design.** A team in another enterprise is
absent, not forbidden: it answers 404 to everyone, because a 403 confirms that
the id names something (ADR-017 D2). Refusals that a caller *is* entitled to
understand — "you are already a member", "that address cannot land in your
enterprise" — carry a reason slug on ``TeamOperationRefused``.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from faultmaven.models.interfaces_user import (
    ITeamRepository,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamMember,
)
from faultmaven.modules.auth.domain.personal_tenant import email_domain
from faultmaven.modules.auth.exceptions import TeamOperationRefused

logger = logging.getLogger(__name__)

#: The team role that may invite, revoke and be counted as an admin. Any account
#: may create a team and is its team admin (ADR-017 D4); everyone who joins by
#: accepting an invitation joins as a plain member.
TEAM_ROLE_ADMIN = "admin"
TEAM_ROLE_MEMBER = "member"

#: The reason slugs this service refuses with. Named constants rather than
#: literals at the raise sites so that the two rules which MUST answer
#: identically — an address on another domain, and an address whose account is
#: anchored to another enterprise — cannot drift into two spellings and turn
#: the endpoint into an account-existence oracle.
REASON_ENTERPRISE_IS_PERSONAL = "enterprise_is_personal"
REASON_ADDRESS_OUTSIDE_DOMAIN = "address_outside_enterprise_domain"
REASON_ALREADY_A_MEMBER = "already_a_member"
REASON_NOT_A_TEAM_ADMIN = "not_a_team_admin"
REASON_NOT_FOUND = "not_found"
REASON_INVITATION_EXPIRED = "invitation_expired"
REASON_INVITATION_NOT_PENDING = "invitation_not_pending"
REASON_LAST_ADMIN_CANNOT_LEAVE = "last_admin_cannot_leave"

#: The message that accompanies a 404. One string for every "no such thing"
#: answer on this surface, carrying no id and naming no kind of row: a message
#: that said *what* was not found would re-open the existence question the
#: status code closes.
_NOT_FOUND_MESSAGE = "Not found."


def normalize_invitation_email(email: Optional[str]) -> str:
    """The one spelling an address is stored and compared as.

    Case-folded rather than lowercased: ``str.casefold`` is the comparison the
    Unicode standard defines for caseless matching, and an address reaching here
    is IdP-verified but not necessarily ASCII. Lowercasing would leave two
    spellings of one address as two invitations — one of which nobody could ever
    accept, because the sign-up hook matches on exactly one of them.

    Module-level, and imported by the sign-up hook in ``sso_login_service``
    rather than re-implemented there: the invite writes the key and the sign-up
    matches on it, so a second copy of this rule is a class of invitation that
    silently never resolves.
    """
    return (email or "").strip().casefold()


def _not_found() -> TeamOperationRefused:
    """The read shape for anything the caller may not see (ADR-017 D2)."""
    return TeamOperationRefused(
        reason=REASON_NOT_FOUND, message=_NOT_FOUND_MESSAGE, status_code=404
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TeamService:
    """Create teams, invite by consent, and resolve membership."""

    def __init__(
        self,
        team_repository: ITeamRepository,
        enterprise_repository: Optional[Any] = None,
        user_repository: Optional[Any] = None,
        invitation_ttl_days: Optional[int] = None,
    ):
        """Wire the service.

        ``enterprise_repository`` and ``user_repository`` are optional because
        the resolution half — the only half standalone and the KB read paths
        use — needs neither. The consent half needs both (the enterprise for its
        ``domain``, the accounts to resolve an address), and refuses rather than
        guesses when either is missing: an unwired dependency must not silently
        become "no account exists", which on the invitation path is a *decision*
        rather than an absence.

        ``invitation_ttl_days`` overrides the configured TTL; it exists for the
        tests, which must be able to mint an expired invitation without waiting
        two weeks.
        """
        self._team_repository = team_repository
        self._enterprises = enterprise_repository
        self._users = user_repository
        self._invitation_ttl_days = invitation_ttl_days

    # -- resolution: the KB read scope -------------------------------------- #

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        """Return every team id ``user_id`` belongs to (empty when none).

        Isolation is enforced in the repository, which joins ``team_members``
        through the RLS-tenanted ``teams`` table so cross-enterprise membership
        fails closed under the ``faultmaven_app`` role.
        """
        return await self._team_repository.list_all_user_team_ids(user_id)

    async def list_user_teams(self, user_id: str) -> List[Team]:
        """Return the full ``Team`` objects ``user_id`` belongs to (empty when none).

        The object-returning form of ``list_all_user_team_ids`` (same RLS-scoped
        membership resolution). Backs the ``GET /teams`` read path: the frontend
        needs team names to render share badges and to populate the
        share-to-team picker, which the id-only method cannot supply.
        """
        return await self._team_repository.list_user_teams(user_id)

    # -- teams: anyone creates, and is its admin (D4) ----------------------- #

    async def create_team(
        self,
        *,
        enterprise_id: str,
        creator_user_id: str,
        name: str,
        description: Optional[str] = None,
    ) -> Team:
        """Create a team in ``enterprise_id`` with its creator as team admin.

        No permission is checked beyond being an authenticated account in an
        enterprise, and that is D4 exactly: *any* account may create a team.
        Creating one grants the creator nothing over anybody else — a team with
        one member sees what that member already saw.

        The creator's membership is written second and is not optional: a team
        whose creator is not in it would be a team nobody can administer, and
        (since every other read here is gated on membership) one nobody can
        even see. ``add_member`` refuses a member from another enterprise, so a
        creator whose anchor does not match the team it just created leaves no
        half-formed team behind — the team is deleted and the call refuses.
        """
        now = _now()
        team = Team(
            team_id=str(uuid.uuid4()),
            enterprise_id=enterprise_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        created = await self._team_repository.create_team(team)
        added = await self._team_repository.add_member(
            created.team_id, creator_user_id, TEAM_ROLE_ADMIN
        )
        if not added:
            # The anchors did not match, or one of them did not resolve. Undo
            # rather than leave a team no account can reach.
            await self._team_repository.delete_team(created.team_id)
            logger.warning(
                "Refusing team creation: %s could not be made a member of the "
                "team it created",
                creator_user_id,
            )
            raise _not_found()
        logger.info(
            "Created team %s in enterprise %s (admin %s)",
            created.team_id,
            enterprise_id,
            creator_user_id,
        )
        return created

    async def get_team_for_member(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> Team:
        """The team, iff it is in ``enterprise_id`` and ``user_id`` is in it.

        Every other team-addressed operation starts here, so the 404 shape is
        stated once: a team in another enterprise, a team that does not exist,
        and a team the caller is simply not in are one answer.
        """
        team = await self._team_repository.get_team(team_id)
        if team is None or team.enterprise_id != enterprise_id:
            raise _not_found()
        if not await self._team_repository.is_team_member(team_id, user_id):
            raise _not_found()
        return team

    async def list_members(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> List[TeamMember]:
        """The roster, readable by any member of the team."""
        await self.get_team_for_member(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        return await self._team_repository.list_team_members(team_id)

    async def get_team_name(self, team_id: str) -> Optional[str]:
        """The team's display name, or ``None`` when it cannot be read.

        A name is not access. This exists for the invitee's own list, where the
        caller is *not* a member — so ``get_team_for_member`` cannot serve it —
        and is called only for invitations already established as addressed to
        that caller. The read is still enterprise-scoped by RLS, so it can name
        no team outside the caller's own enterprise.
        """
        team = await self._team_repository.get_team(team_id)
        return team.name if team else None

    async def leave_team(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> None:
        """Leave a team; the last member out soft-deletes it.

        Two rules, and they are the same rule seen from either end of a team's
        life. A team must never be left without an admin **while other members
        remain** — those members would keep sharing into a team nobody can
        administer, invite to, or wind up. But the sole member of a team is not
        stranding anybody by leaving, and a team with no members at all is not a
        sharing unit; it is soft-deleted, which also drops it out of every
        share-to-team picker.
        """
        await self.get_team_for_member(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        members = await self._team_repository.list_team_members(team_id)
        others = [member for member in members if member.user_id != user_id]
        leaver_is_admin = any(
            member.user_id == user_id and member.team_role == TEAM_ROLE_ADMIN
            for member in members
        )
        other_admins = [
            member for member in others if member.team_role == TEAM_ROLE_ADMIN
        ]
        if others and leaver_is_admin and not other_admins:
            raise TeamOperationRefused(
                reason=REASON_LAST_ADMIN_CANNOT_LEAVE,
                message=(
                    "You are the team's only admin and other members remain. "
                    "Make another member an admin, or remove them, first."
                ),
                status_code=409,
            )
        await self._team_repository.remove_member(team_id, user_id)
        if not others:
            await self._team_repository.delete_team(team_id)
            logger.info("Team %s soft-deleted: its last member left", team_id)

    # -- invitations: the consent (D3 + D4) --------------------------------- #

    async def invite(
        self, *, enterprise_id: str, team_id: str, actor_user_id: str, email: str
    ) -> TeamInvitation:
        """Offer ``email`` a place on ``team_id``. The domain rule, in order.

        The order is the design, not an implementation detail. Everything that
        can be decided from the **address and the enterprise alone** is decided
        first, and only then is an account looked up — so a caller learns
        nothing about who has an account at a domain they cannot invite from.

        1. The caller's enterprise is **personal** (its ``domain`` is NULL — an
           island): every invitation is refused. Nobody can ever land in a
           private enterprise, so an invitation into one could never resolve.
        2. The address's domain is **not** the enterprise's: refused. A
           personal-domain address gets a private enterprise of its own and a
           company address gets its own domain's enterprise, so neither can ever
           land here (D3).
        3. The domain matches and an **account exists**: if it is anchored to
           this enterprise the invitation names it; if it is anchored elsewhere
           — the operator-mapped-enterprise case — it is refused with the SAME
           reason and status as (2), deliberately indistinguishable.
        4. The domain matches and **no account exists**: the invitation is
           created unresolved. It resolves if and when that address signs up
           into this enterprise; if it signs up elsewhere it stays pending until
           it expires, and never resolves.
        5. Already a member → 409. A pending offer for the same address on the
           same team → that offer is returned, not a second row.
        """
        team = await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=actor_user_id
        )
        if self._enterprises is None or self._users is None:
            # Not "no account exists" — an unwired dependency is an absence of
            # evidence, and on this path that must never be read as evidence of
            # absence. See __init__.
            logger.error("Team invitations unavailable: repositories unwired")
            raise _not_found()

        address = self._normalize_email(email)

        enterprise = await self._enterprises.get_enterprise(enterprise_id)
        enterprise_domain = getattr(enterprise, "domain", None) if enterprise else None
        if not enterprise_domain:
            raise TeamOperationRefused(
                reason=REASON_ENTERPRISE_IS_PERSONAL,
                message=(
                    "This is a personal enterprise: it holds one account and no "
                    "address can be invited into it."
                ),
                status_code=403,
            )

        if email_domain(address) != enterprise_domain.casefold():
            raise self._outside_domain()

        account = await self._users.get_by_email(address)
        invited_user_id: Optional[str] = None
        if account is not None:
            if getattr(account, "enterprise_id", None) != enterprise_id:
                # Rule 3's second half. Same refusal as rule 2, by construction.
                raise self._outside_domain()
            invited_user_id = account.user_id
            if await self._team_repository.is_team_member(team_id, account.user_id):
                raise TeamOperationRefused(
                    reason=REASON_ALREADY_A_MEMBER,
                    message="That address is already a member of this team.",
                    status_code=409,
                )

        existing = await self._team_repository.find_pending_invitation(team_id, address)
        if existing is not None and not existing.is_expired(_now()):
            return existing

        now = _now()
        invitation = TeamInvitation(
            invitation_id=str(uuid.uuid4()),
            enterprise_id=team.enterprise_id,
            team_id=team_id,
            email=address,
            invited_user_id=invited_user_id,
            invited_by=actor_user_id,
            status=TeamInvitationStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(days=self._ttl_days()),
        )
        if existing is not None:
            # The live offer had run out. Expire it before minting its
            # replacement: the partial unique index admits one PENDING row per
            # address per team, and an expired-but-unstamped row still counts.
            await self._team_repository.mark_invitation_expired(existing.invitation_id)
        return await self._team_repository.create_invitation(invitation)

    async def list_team_invitations(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> List[TeamInvitation]:
        """Every invitation issued for a team, for its admin.

        Expiry is applied on the way out (and stamped on the row), so an admin
        reading the list sees offers that have run out as ``expired`` rather
        than as still-live ones a lazy scheme has not got round to.
        """
        await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        invitations = await self._team_repository.list_team_invitations(team_id)
        return [await self._settle_expiry(item) for item in invitations]

    async def revoke_invitation(
        self,
        *,
        enterprise_id: str,
        team_id: str,
        invitation_id: str,
        actor_user_id: str,
    ) -> None:
        """Withdraw an offer, as the team's admin."""
        await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=actor_user_id
        )
        invitation = await self._team_repository.get_invitation(
            enterprise_id, invitation_id
        )
        if invitation is None or invitation.team_id != team_id:
            raise _not_found()
        await self._team_repository.mark_invitation_revoked(
            invitation_id, actor_user_id, _now()
        )

    async def list_my_invitations(
        self, *, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The live offers addressed to me.

        Addressed two ways because an invitation may predate the account: by
        ``invited_user_id`` once it resolved, and by address while it has not.
        Only the ones still live are returned — an offer that has run out is
        stamped ``expired`` here and dropped, which is what makes the lazy
        scheme indistinguishable from a swept one at every surface a person
        sees.
        """
        address = self._normalize_email(email)
        invitations = await self._team_repository.list_invitations_for_invitee(
            enterprise_id, user_id, address
        )
        live: List[TeamInvitation] = []
        for invitation in invitations:
            settled = await self._settle_expiry(invitation)
            if settled.status == TeamInvitationStatus.PENDING:
                live.append(settled)
        return live

    async def accept_invitation(
        self, *, enterprise_id: str, invitation_id: str, user: Any
    ) -> Team:
        """Consent: become a member of the team this invitation names.

        The consent that forms the team (D4). Membership is created here and
        nowhere else on this surface — a pending invitation grants nothing, and
        an admin cannot add a member directly.
        """
        invitation = await self._invitation_addressed_to(
            enterprise_id=enterprise_id, invitation_id=invitation_id, user=user
        )
        if invitation.is_expired(_now()):
            await self._team_repository.mark_invitation_expired(invitation_id)
            raise TeamOperationRefused(
                reason=REASON_INVITATION_EXPIRED,
                message="This invitation has expired.",
                status_code=410,
            )
        team = await self._team_repository.get_team(invitation.team_id)
        if team is None or team.enterprise_id != enterprise_id:
            raise _not_found()

        accepted = await self._team_repository.mark_invitation_accepted(
            invitation_id, user.user_id, _now()
        )
        if not accepted:
            # Somebody answered it between the read and the write. The status is
            # whatever they made it; either way this call did not accept it.
            raise TeamOperationRefused(
                reason=REASON_INVITATION_NOT_PENDING,
                message="This invitation is no longer open.",
                status_code=409,
            )
        added = await self._team_repository.add_member(
            invitation.team_id, user.user_id, TEAM_ROLE_MEMBER
        )
        if not added:
            # ``add_member`` refuses a member anchored to another enterprise.
            # Reaching here means the invitation and the account disagree about
            # the enterprise, which the invite rule should have made impossible
            # — refuse in the read shape rather than report a half-done accept.
            logger.warning(
                "Invitation %s accepted but membership refused for %s",
                invitation_id,
                user.user_id,
            )
            raise _not_found()
        logger.info(
            "Invitation %s accepted: %s joined team %s",
            invitation_id,
            user.user_id,
            invitation.team_id,
        )
        return team

    async def decline_invitation(
        self, *, enterprise_id: str, invitation_id: str, user: Any
    ) -> None:
        """Refuse an offer. Recorded, so the admin can see it was answered."""
        invitation = await self._invitation_addressed_to(
            enterprise_id=enterprise_id, invitation_id=invitation_id, user=user
        )
        await self._team_repository.mark_invitation_revoked(
            invitation.invitation_id, user.user_id, _now()
        )

    async def resolve_invitations_for_account(
        self, *, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """The sign-up hook: name this account on the offers waiting for it.

        Called once the account's enterprise anchor is written (ADR-017 D3). An
        invitation issued to an address with no account resolves here — and only
        when the address lands in the enterprise that issued it, which the
        repository's own predicate enforces. Idempotent: it touches only rows
        whose ``invited_user_id`` is still NULL.
        """
        address = self._normalize_email(email)
        if not address:
            return 0
        return await self._team_repository.resolve_invitations_for_account(
            enterprise_id, address, user_id
        )

    # -- internals ---------------------------------------------------------- #

    def _ttl_days(self) -> int:
        """How long a new invitation stays live.

        Read through ``get_settings()`` at the point of use, like every other
        setting on this campaign's paths, so a configured value is the one the
        decision uses rather than whatever was set when this module loaded.
        """
        if self._invitation_ttl_days is not None:
            return int(self._invitation_ttl_days)
        from faultmaven.config.settings import get_settings

        return int(get_settings().auth.team_invitation_ttl_days)

    @staticmethod
    def _normalize_email(email: Optional[str]) -> str:
        """Case-fold and trim an address. The stored and compared form."""
        return normalize_invitation_email(email)

    @staticmethod
    def _outside_domain() -> TeamOperationRefused:
        """Rules 2 and 3 answer with this — the SAME object shape, deliberately.

        An address on another domain and an address whose account is anchored
        elsewhere must be indistinguishable: telling them apart would answer
        "does an account exist at this address?" to anyone who can create a
        team, which is everybody.
        """
        return TeamOperationRefused(
            reason=REASON_ADDRESS_OUTSIDE_DOMAIN,
            message=(
                "That address cannot join a team in this enterprise. An "
                "invitation can only reach an address on the enterprise's own "
                "domain."
            ),
            status_code=403,
        )

    async def _require_team_admin(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> Team:
        """The team, iff the caller is a member AND its admin.

        Two different answers on purpose. A non-member is told the team is not
        there (404) — the enterprise-crossing shape, and the one that discloses
        nothing. A member without the role is told they lack it (403), which
        reveals nothing they do not already know: they can see the team.
        """
        team = await self.get_team_for_member(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        members = await self._team_repository.list_team_members(team_id)
        if not any(
            member.user_id == user_id and member.team_role == TEAM_ROLE_ADMIN
            for member in members
        ):
            raise TeamOperationRefused(
                reason=REASON_NOT_A_TEAM_ADMIN,
                message="Only a team admin can manage this team's invitations.",
                status_code=403,
            )
        return team

    async def _invitation_addressed_to(
        self, *, enterprise_id: str, invitation_id: str, user: Any
    ) -> TeamInvitation:
        """The invitation, iff it is addressed to ``user`` and still open.

        "Addressed to me" is two tests, matching the two ways an invitation can
        name someone: by ``invited_user_id`` once it has resolved, and by
        address while it has not. Everything else — another account's
        invitation, an invitation in another enterprise, an id that names
        nothing — is one 404, because a 403 would confirm the id exists and let
        an invitation id be probed for.
        """
        invitation = await self._team_repository.get_invitation(
            enterprise_id, invitation_id
        )
        if invitation is None:
            raise _not_found()
        if invitation.invited_user_id is not None:
            if invitation.invited_user_id != user.user_id:
                raise _not_found()
        elif invitation.email != self._normalize_email(getattr(user, "email", None)):
            raise _not_found()
        if invitation.status != TeamInvitationStatus.PENDING:
            raise TeamOperationRefused(
                reason=REASON_INVITATION_NOT_PENDING,
                message="This invitation is no longer open.",
                status_code=409,
            )
        return invitation

    async def _settle_expiry(self, invitation: TeamInvitation) -> TeamInvitation:
        """Stamp a pending-but-elapsed invitation ``expired``, and return it so.

        Lazy expiry's one obligation: whoever reads a row past its deadline is
        the one that settles it, so the stored status and what the reader is
        told never disagree.
        """
        if (
            invitation.status != TeamInvitationStatus.PENDING
            or not invitation.is_expired(_now())
        ):
            return invitation
        await self._team_repository.mark_invitation_expired(invitation.invitation_id)
        return invitation.model_copy(update={"status": TeamInvitationStatus.EXPIRED})
