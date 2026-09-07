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

from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure.persistence.user_repository import UserRepository
from faultmaven.models.interfaces_user import (
    IEnterpriseRepository,
    ITeamRepository,
    LeaveOutcome,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamMember,
    TeamNameTakenError,
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
REASON_TEAM_NAME_TAKEN = "team_name_taken"
REASON_INVITATION_EXPIRED = "invitation_expired"
REASON_INVITATION_NOT_PENDING = "invitation_not_pending"
REASON_LAST_ADMIN_CANNOT_LEAVE = "last_admin_cannot_leave"

#: The message that accompanies a 404. One string for every "no such thing"
#: answer on this surface, carrying no id and naming no kind of row: a message
#: that said *what* was not found would re-open the existence question the
#: status code closes.
_NOT_FOUND_MESSAGE = "Not found."


def normalize_invitation_email(email: Optional[str]) -> str:
    """The one spelling an address is stored and compared as: ``strip().lower()``.

    **Lower-cased, not case-folded, and that is a correctness requirement rather
    than a style choice.** ``casefold`` is the stronger Unicode comparison and
    would be the better key in isolation — but the key has to match the index
    the account lookup uses, and that is ``func.lower(users.email)``
    (``PostgreSQLUserRepository.get_by_email``; ``jwt_token_generator``
    documents ``.lower()`` as the deployment's convention). For any address
    where the two differ — ``MAẞE@`` lowers to ``maße@`` and folds to
    ``masse@`` — a case-folded key misses the account, and the miss is silent
    and security-relevant: rule 3's "anchored to another enterprise → refuse"
    and the already-a-member check both depend on finding that account, and
    both are skipped when the lookup returns nothing.

    Module-level, and imported by the sign-up hook in ``sso_login_service``
    rather than re-implemented there: the invite writes the key and the sign-up
    matches on it, so a second copy of this rule is a class of invitation that
    silently never resolves.
    """
    return (email or "").strip().lower()


def _not_found() -> NotFoundError:
    """The read shape for anything the caller may not see (ADR-017 D2).

    ``NotFoundError``, not a team-local 404: the house already has one
    not-found envelope and one handler for it, and a second with its own title
    map is a second thing to keep in step for no gain. Deliberately raised with
    the message form only — passing ``resource_type``/``resource_id`` would put
    the very id back in the body that answering 404 is meant to withhold.

    ``TeamOperationRefused`` is then exactly what its name says: a refusal the
    caller is entitled to *understand*, which is the 403/409/410 family. A 404
    is the one answer on this surface that must carry no reason at all.
    """
    return NotFoundError(message=_NOT_FOUND_MESSAGE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TeamService:
    """Create teams, invite by consent, and resolve membership."""

    def __init__(
        self,
        team_repository: ITeamRepository,
        enterprise_repository: IEnterpriseRepository,
        user_repository: UserRepository,
        invitation_ttl_days: Optional[int] = None,
    ):
        """Wire the service. All three repositories are REQUIRED.

        They used to be optional, on the reasoning that the resolution half (the
        KB read scope) needs only the team repository. That reasoning was
        right about the resolution half and wrong about the consequence: the
        composition root builds every repository with a ``try/except`` that
        answers ``None``, so a transient failure to construct the enterprise
        repository produced a service that looked wired, passed every
        capability check, and answered **404 for every team in the deployment**
        — a wiring failure wearing the shape of "you asked for a row that does
        not exist", which is the one answer nobody investigates.

        Refusing to construct is the honest failure: ``create_team_service``
        then returns ``None``, ``team_service is None`` is already the
        deployment-wide "team collaboration is not available here" signal, and
        the surface answers its own 403 saying so.

        ``invitation_ttl_days`` overrides the configured TTL; it exists for the
        tests, which must be able to mint an expired invitation without waiting
        two weeks.
        """
        if team_repository is None:
            raise ValueError("TeamService requires a team repository")
        if enterprise_repository is None:
            raise ValueError(
                "TeamService requires an enterprise repository: the invitation "
                "rule is decided from enterprises.domain (ADR-017 D3), and "
                "without it every invitation would be refused as though the "
                "enterprise were personal"
            )
        if user_repository is None:
            raise ValueError(
                "TeamService requires a user repository: without it an address "
                "with an account is indistinguishable from one without, and "
                "rule 3's anchored-elsewhere refusal cannot be made"
            )
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

        The team and the creator's membership are **one transaction**. Writing
        them separately and compensating with a delete is not the same thing:
        the compensation can itself fail, and until it runs the enterprise holds
        a team nobody is in — invisible to every read here, all of which are
        gated on membership, while still holding its name against the unique
        index.

        A name a live team in this enterprise already carries is a 409, not a
        500. The index is partial on ``deleted_at IS NULL``, so a retired team
        does not hold its name for ever.
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
        try:
            created = await self._team_repository.create_team_with_admin(
                team, creator_user_id, TEAM_ROLE_ADMIN
            )
        except TeamNameTakenError as error:
            raise TeamOperationRefused(
                reason=REASON_TEAM_NAME_TAKEN,
                message="A team in this enterprise already has that name.",
                status_code=409,
            ) from error
        if created is None:
            # The creator is anchored to another enterprise, or the account does
            # not resolve. Nothing was written.
            logger.warning(
                "Refusing team creation: %s cannot be a member of a team in %s",
                creator_user_id,
                enterprise_id,
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

        The 404 shape, stated once: a team in another enterprise, a team that
        does not exist, and a team the caller is simply not in are one answer.
        """
        team, _ = await self._team_and_roster(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        return team

    async def list_members(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> List[TeamMember]:
        """The roster, readable by any member of the team."""
        _, roster = await self._team_and_roster(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        return roster

    async def leave_team(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> None:
        """Leave a team; the last member out takes the team with them.

        Two rules, and they are the same rule seen from either end of a team's
        life. A team must never be left without an admin **while other members
        remain** — those members would keep sharing into a team nobody can
        administer, invite to, or wind up, and no route can repair it because
        there is no promote endpoint. The sole member of a team strands nobody
        by leaving, so the team is retired instead.

        Both are decided **inside one transaction in the repository**, holding a
        lock on the team row. Deciding them here would be a read-then-write, and
        two admins leaving at the same instant would each see the other and both
        go. The repository also revokes the retired team's pending invitations
        in that transaction: an offer to a team nobody can see can be neither
        accepted nor declined.
        """
        outcome = await self._team_repository.leave_team(
            enterprise_id, team_id, user_id, TEAM_ROLE_ADMIN
        )
        if outcome is LeaveOutcome.ABSENT:
            raise _not_found()
        if outcome is LeaveOutcome.LAST_ADMIN:
            raise TeamOperationRefused(
                reason=REASON_LAST_ADMIN_CANNOT_LEAVE,
                message=(
                    "You are the team's only admin and other members remain. "
                    "Make another member an admin, or remove them, first."
                ),
                status_code=409,
            )
        if outcome is LeaveOutcome.LEFT_AND_RETIRED:
            logger.info("Team %s retired: its last member left", team_id)

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
           same team → that offer is returned, not a second row — including when
           a concurrent invite won the race, which the repository settles.
        """
        team = await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=actor_user_id
        )
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

        if email_domain(address) != enterprise_domain.lower():
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

        existing = await self._team_repository.find_pending_invitation(
            enterprise_id, team_id, address
        )
        if existing is not None:
            settled = await self._settle_expiry(existing)
            if settled.status == TeamInvitationStatus.PENDING:
                return settled
            # The live offer had run out and has just been stamped ``expired``,
            # which frees the partial unique index for its replacement below.

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
        return await self._team_repository.create_invitation(invitation)

    async def list_team_invitations(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> List[TeamInvitation]:
        """Every invitation issued for a team, for its admin.

        Expiry is applied on the way out (and stamped on the rows, in one
        statement), so an admin reading the list sees offers that have run out
        as ``expired`` rather than as still-live ones a lazy scheme has not got
        round to.
        """
        await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        invitations = await self._team_repository.list_team_invitations(
            enterprise_id, team_id
        )
        return await self._settle_all(invitations)

    async def revoke_invitation(
        self,
        *,
        enterprise_id: str,
        team_id: str,
        invitation_id: str,
        actor_user_id: str,
    ) -> None:
        """Withdraw an offer, as the team's admin.

        An offer that has already run out answers 410 and is stamped
        ``expired``, not ``revoked``: ``revoked_by`` is the record of who ended
        it, and writing a withdrawal nobody performed puts a decision in the
        record that no person made.
        """
        await self._require_team_admin(
            enterprise_id=enterprise_id, team_id=team_id, user_id=actor_user_id
        )
        invitation = await self._read_invitation(enterprise_id, invitation_id)
        if invitation.team_id != team_id:
            raise _not_found()
        self._require_open(invitation)
        await self._team_repository.mark_invitation_revoked(
            invitation_id, actor_user_id, _now()
        )

    async def list_my_invitations(
        self, *, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The live offers addressed to me.

        Addressed two ways because an invitation may predate the account: by
        ``invited_user_id`` once it resolved, and by address while it has not.
        Only the ones still live are returned — offers that have run out are
        stamped ``expired`` here, in one statement, and dropped, which is what
        makes the lazy scheme indistinguishable from a swept one at every
        surface a person sees.
        """
        address = self._normalize_email(email)
        invitations = await self._team_repository.list_invitations_for_invitee(
            enterprise_id, user_id, address
        )
        settled = await self._settle_all(invitations)
        return [
            invitation
            for invitation in settled
            if invitation.status == TeamInvitationStatus.PENDING
        ]

    async def name_teams(self, *, enterprise_id: str, team_ids: List[str]) -> dict:
        """``{team_id: name}`` for live teams of this enterprise. One query.

        Serves the invitee's own list, where the caller is *not* a member of the
        teams being named — so the membership-gated reads cannot answer it — and
        is called only for invitations already established as addressed to that
        caller. A name is not access; the enterprise predicate is still applied,
        so it can name no team outside the caller's own.
        """
        return await self._team_repository.get_team_names(enterprise_id, team_ids)

    async def accept_invitation(
        self, *, enterprise_id: str, invitation_id: str, user_id: str, email: str
    ) -> Team:
        """Consent: become a member of the team this invitation names.

        The consent that forms the team (D4). Membership is created here and
        nowhere else on this surface — a pending invitation grants nothing, and
        an admin cannot add a member directly.

        **The membership is written before the offer is stamped**, and the order
        is the whole of whether a partial failure is recoverable. Stamping first
        spends a one-shot token: if the membership then fails — the team was
        retired between the read and the write, the invitee's anchor moved after
        the token was minted — the row reads ``accepted`` with no membership and
        every retry answers 409, because the only thing that could have
        authorised the retry is the row just spent. Writing the membership first
        fails the other way: ``add_member`` is an idempotent upsert, so a
        failure after it leaves the offer pending and the retry both re-writes
        the same membership row and completes the stamp.
        """
        invitation = await self._read_invitation(enterprise_id, invitation_id)
        self._require_addressed_to(invitation, user_id=user_id, email=email)
        self._require_open(invitation)

        team = await self._team_repository.get_team(enterprise_id, invitation.team_id)
        if team is None:
            raise _not_found()

        added = await self._team_repository.add_member(
            invitation.team_id, user_id, TEAM_ROLE_MEMBER
        )
        if not added:
            # ``add_member`` refuses a member anchored to another enterprise.
            # Nothing has been consumed, so the offer stays answerable.
            logger.warning(
                "Membership refused for %s on invitation %s; the offer is " "unchanged",
                user_id,
                invitation_id,
            )
            raise _not_found()

        accepted = await self._team_repository.mark_invitation_accepted(
            invitation_id, user_id, _now()
        )
        if not accepted:
            # Somebody answered it between the read and the stamp. The
            # membership upsert above is idempotent, so nothing is left
            # half-done either way; this call simply did not accept it.
            raise TeamOperationRefused(
                reason=REASON_INVITATION_NOT_PENDING,
                message="This invitation is no longer open.",
                status_code=409,
            )
        logger.info(
            "Invitation %s accepted: %s joined team %s",
            invitation_id,
            user_id,
            invitation.team_id,
        )
        return team

    async def decline_invitation(
        self, *, enterprise_id: str, invitation_id: str, user_id: str, email: str
    ) -> None:
        """Refuse an offer. Recorded, so the admin can see it was answered."""
        invitation = await self._read_invitation(enterprise_id, invitation_id)
        self._require_addressed_to(invitation, user_id=user_id, email=email)
        self._require_open(invitation)
        await self._team_repository.mark_invitation_revoked(
            invitation.invitation_id, user_id, _now()
        )

    async def resolve_invitations_for_account(
        self, *, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """The sign-up hook: name this account on the offers waiting for it.

        Called on every admitted SSO login, once the account is established as
        anchored to ``enterprise_id`` (ADR-017 D3) — not only on one that moved
        the anchor, because a JIT-provisioned account is created already
        carrying its enterprise and so writes no anchor on the first login. An
        invitation issued to an address with no account resolves here — and only
        when the address lands in the enterprise that issued it, which the
        repository's own predicate enforces. Idempotent: it touches only rows
        whose ``invited_user_id`` is still NULL.

        Short-circuits for an enterprise with no domain. A personal enterprise
        refuses every invitation at send time (rule 1), so it can hold none —
        and this runs on the hot login path, where a guaranteed-empty write
        transaction is a cost with no possible result.
        """
        address = self._normalize_email(email)
        if not address:
            return 0
        enterprise = await self._enterprises.get_enterprise(enterprise_id)
        if not getattr(enterprise, "domain", None):
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
        """Trim and lower-case an address. The stored and compared form."""
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

    async def _team_and_roster(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> tuple[Team, List[TeamMember]]:
        """The team and its roster, iff the caller is in it. One read.

        Every team-addressed operation needs both facts, and asking for them
        separately meant three sessions to answer one question — team, then "am
        I a member?", then the roster, of which the last already contains the
        answer to the second.
        """
        team, roster = await self._team_repository.get_team_with_members(
            enterprise_id, team_id
        )
        if team is None:
            raise _not_found()
        if not any(member.user_id == user_id for member in roster):
            raise _not_found()
        return team, roster

    async def _require_team_admin(
        self, *, enterprise_id: str, team_id: str, user_id: str
    ) -> Team:
        """The team, iff the caller is a member AND its admin.

        Two different answers on purpose. A non-member is told the team is not
        there (404) — the enterprise-crossing shape, and the one that discloses
        nothing. A member without the role is told they lack it (403), which
        reveals nothing they do not already know: they can see the team.
        """
        team, roster = await self._team_and_roster(
            enterprise_id=enterprise_id, team_id=team_id, user_id=user_id
        )
        if not any(
            member.user_id == user_id and member.team_role == TEAM_ROLE_ADMIN
            for member in roster
        ):
            raise TeamOperationRefused(
                reason=REASON_NOT_A_TEAM_ADMIN,
                message="Only a team admin can manage this team's invitations.",
                status_code=403,
            )
        return team

    async def _read_invitation(
        self, enterprise_id: str, invitation_id: str
    ) -> TeamInvitation:
        """The one way an invitation is read, expiry already settled.

        Every verb went through its own combination of "read the row" and
        "notice it elapsed", and they disagreed: decline and revoke looked only
        at the stored status, so an elapsed offer was recorded as *revoked* — a
        withdrawal nobody performed — and accept answered 410 or 409 depending
        on whether anything had listed the row first. Settling here means the
        row every caller holds is already the row the database now has.
        """
        invitation = await self._team_repository.get_invitation(
            enterprise_id, invitation_id
        )
        if invitation is None:
            raise _not_found()
        return await self._settle_expiry(invitation)

    @staticmethod
    def _require_addressed_to(
        invitation: TeamInvitation, *, user_id: str, email: str
    ) -> None:
        """Refuse anything not addressed to this caller, in the read shape.

        "Addressed to me" is two tests, matching the two ways an invitation can
        name someone: by ``invited_user_id`` once it has resolved, and by
        address while it has not. Everything else — another account's
        invitation, an invitation in another enterprise, an id that names
        nothing — is one 404, because a 403 would confirm the id exists and let
        an invitation id be probed for.
        """
        if invitation.invited_user_id is not None:
            if invitation.invited_user_id != user_id:
                raise _not_found()
        elif invitation.email != normalize_invitation_email(email):
            raise _not_found()

    @staticmethod
    def _require_open(invitation: TeamInvitation) -> None:
        """Refuse an offer that is no longer answerable, saying which way.

        ``expired`` is its own answer (410) rather than folded into "no longer
        open" (409): the caller was entitled to that invitation and is entitled
        to know it lapsed, which is a different fact from somebody else having
        answered it.
        """
        if invitation.status == TeamInvitationStatus.EXPIRED:
            raise TeamOperationRefused(
                reason=REASON_INVITATION_EXPIRED,
                message="This invitation has expired.",
                status_code=410,
            )
        if invitation.status != TeamInvitationStatus.PENDING:
            raise TeamOperationRefused(
                reason=REASON_INVITATION_NOT_PENDING,
                message="This invitation is no longer open.",
                status_code=409,
            )

    async def _settle_all(
        self, invitations: List[TeamInvitation]
    ) -> List[TeamInvitation]:
        """Stamp every elapsed row in a batch, in ONE statement, and return it so.

        Lazy expiry's obligation is that whoever reads a row past its deadline
        settles it. Doing that one commit per row made reading a stale mailbox
        cost a transaction per stale offer; the ids are collected and written
        together instead.
        """
        now = _now()
        elapsed = [
            invitation
            for invitation in invitations
            if invitation.status == TeamInvitationStatus.PENDING
            and invitation.is_expired(now)
        ]
        if not elapsed:
            return list(invitations)
        await self._team_repository.expire_invitations(
            [invitation.invitation_id for invitation in elapsed]
        )
        stamped = {invitation.invitation_id for invitation in elapsed}
        return [
            (
                invitation.model_copy(update={"status": TeamInvitationStatus.EXPIRED})
                if invitation.invitation_id in stamped
                else invitation
            )
            for invitation in invitations
        ]

    async def _settle_expiry(self, invitation: TeamInvitation) -> TeamInvitation:
        """The single-row form of :meth:`_settle_all`."""
        settled = await self._settle_all([invitation])
        return settled[0]
