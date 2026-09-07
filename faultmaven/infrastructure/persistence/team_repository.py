"""Team Repository - SQLAlchemy ORM Implementation.

Implements ITeamRepository for team and team-membership persistence. Mirrors
PostgreSQLOrganizationRepository: the core ships the repository *substrate*
(used by the single-tenant default-team bootstrap and by KB scope resolution);
team *management* (create/invite from a UI) is the hosted admin composed
module, which drives these same methods (ADR-010 D4 / ADR-013).

Isolation posture (team_members RLS): ``team_members`` has no ``enterprise_id``
column of its own, so it is keyed by one hop — its policy reads
``USING (team_id IN (SELECT team_id FROM teams WHERE teams.enterprise_id =
current_setting('app.current_enterprise_id', true)))`` — and membership rows are
enterprise-scoped through their team, failing closed under the limited
``faultmaven_app`` role. (Rejected alternative: add ``enterprise_id`` to
``team_members`` + a direct policy — duplicates the key already reachable via
``teams.enterprise_id`` and invites drift.) See ADR-013 + ADR-017 D4.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import (
    TeamInvitationModel,
    TeamMemberModel,
    TeamModel,
)
from faultmaven.models.interfaces_user import (
    AcceptOutcome,
    ITeamRepository,
    LeaveOutcome,
    Team,
    TeamInvitation,
    TeamInvitationStatus,
    TeamMember,
    TeamNameTakenError,
)

logger = logging.getLogger(__name__)


# An ``IntegrityError`` says *a* constraint was violated; it does not portably
# say which. The first version of this file matched the index name in the
# driver's message, which asyncpg supplies and **SQLite does not** — SQLite says
# ``UNIQUE constraint failed: teams.enterprise_id, teams.name`` and names no
# index — so every recovery below degraded to a re-raise on the single-tenant
# dialect, where it could take down default-team bootstrap at startup.
#
# So nothing here reads a driver message. Each recovery rolls back and
# **re-reads to decide**, which is dialect-independent and is also what the
# recovery actually means: not "was this error the unique index?" but "is there
# now a row that makes my write redundant?". If there is not, the error was
# something else and is re-raised unchanged.


def _member_to_domain(model: TeamMemberModel) -> TeamMember:
    return TeamMember(
        user_id=model.user_id,
        team_id=model.team_id,
        team_role=model.team_role,
        joined_at=model.joined_at,
    )


def _invitation_to_domain(model: TeamInvitationModel) -> TeamInvitation:
    """Convert an invitation row to its domain object."""
    return TeamInvitation(
        invitation_id=model.invitation_id,
        enterprise_id=model.enterprise_id,
        team_id=model.team_id,
        email=model.email,
        invited_user_id=model.invited_user_id,
        invited_by=model.invited_by,
        status=TeamInvitationStatus(model.status),
        created_at=model.created_at,
        expires_at=model.expires_at,
        accepted_at=model.accepted_at,
        revoked_by=model.revoked_by,
        revoked_at=model.revoked_at,
    )


def _model_to_domain(model: TeamModel) -> Team:
    """Convert ORM model to domain object."""
    return Team(
        team_id=model.team_id,
        enterprise_id=model.enterprise_id,
        name=model.name,
        description=model.description,
        created_at=model.created_at,
        updated_at=model.updated_at,
        deleted_at=model.deleted_at,
    )


class PostgreSQLTeamRepository(ITeamRepository):
    """SQLAlchemy ORM implementation of team repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    def _dialect(self) -> Optional[str]:
        """The bound dialect's name, or ``None`` when it cannot be determined.

        ``None`` matters because of how it is used: the lock is taken *unless*
        the dialect is positively identified as SQLite. The first version asked
        ``bind is not None and dialect != "sqlite"``, which skipped the lock
        whenever the bind was merely unresolvable — silently removing the one
        mechanism the method exists to provide, in exactly the situation where
        least is known. Failing closed here means an unknown dialect gets the
        lock and, at worst, an error naming the unsupported syntax.
        """
        bind = None
        try:
            bind = self.db.get_bind()
        except Exception:  # noqa: BLE001 - an unbound session is the point
            bind = getattr(self.db, "bind", None)
        return getattr(getattr(bind, "dialect", None), "name", None)

    def _with_row_lock(self, stmt):
        """``SELECT … FOR UPDATE``, except on SQLite, which has no such thing.

        SQLite is the single-tenant store, where team collaboration is unwired
        entirely (``team_service is None``), so the only deployment that can
        reach the locked paths is the one that has the lock. Skipping it there
        is not a degraded guarantee; it is an unreachable branch kept honest.
        """
        if self._dialect() == "sqlite":
            return stmt
        return stmt.with_for_update()

    async def _live_team_named(
        self, enterprise_id: str, name: str, exclude_team_id: str
    ) -> bool:
        """Is there a LIVE team of this enterprise with this name, other than mine?

        The re-read that decides what an ``IntegrityError`` on ``teams`` was:
        this is the only conflict the caller has a better answer for than
        "something went wrong".
        """
        found = (
            await self.db.execute(
                select(TeamModel.team_id).where(
                    TeamModel.enterprise_id == enterprise_id,
                    TeamModel.name == name,
                    TeamModel.team_id != exclude_team_id,
                    TeamModel.deleted_at.is_(None),
                )
            )
        ).first()
        return found is not None

    def _team_row(self, team: Team) -> TeamModel:
        return TeamModel(
            team_id=team.team_id,
            enterprise_id=team.enterprise_id,
            name=team.name,
            description=team.description,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )

    async def create_team(self, team: Team) -> Team:
        """Create a new team, with no members (the bootstrap form)."""
        self.db.add(self._team_row(team))
        try:
            await self.db.commit()
        except IntegrityError as error:
            await self.db.rollback()
            if await self._live_team_named(team.enterprise_id, team.name, team.team_id):
                raise TeamNameTakenError(team.name) from error
            raise

        logger.info(f"Created team: {team.team_id} ({team.name})")
        return team

    async def create_team_with_admin(
        self, team: Team, admin_user_id: str, team_role: str
    ) -> Optional[Team]:
        """Create a team and its creator's membership as ONE transaction.

        The anchor comparison ``add_member`` makes is repeated here rather than
        delegated, because ``add_member`` commits: calling it would end this
        transaction half way through and reintroduce exactly the two-write
        window this method exists to close. The rule is the same one, and the
        two are held together by
        ``tests/.../test_team_consent_failure_modes.py``.
        """
        anchors = (
            await self.db.execute(
                text("SELECT enterprise_id FROM users WHERE user_id = :u"),
                {"u": admin_user_id},
            )
        ).first()
        creator_enterprise = anchors[0] if anchors else None
        if not creator_enterprise or creator_enterprise != team.enterprise_id:
            logger.warning(
                "Refusing team creation: %s is anchored to %s, the team is in %s",
                admin_user_id,
                creator_enterprise,
                team.enterprise_id,
            )
            return None

        try:
            self.db.add(self._team_row(team))
            # Flushed, not committed: the membership row's foreign key needs the
            # team to exist, and the unit of work does not order two unrelated
            # mappers for us. Still one transaction — which is the whole point,
            # so a failure below takes the team with it.
            await self.db.flush()
            self.db.add(
                TeamMemberModel(
                    user_id=admin_user_id,
                    team_id=team.team_id,
                    team_role=team_role,
                    joined_at=datetime.now(timezone.utc),
                )
            )
            await self.db.commit()
        except IntegrityError as error:
            await self.db.rollback()
            if await self._live_team_named(team.enterprise_id, team.name, team.team_id):
                raise TeamNameTakenError(team.name) from error
            raise

        logger.info(
            "Created team %s (%s) with admin %s", team.team_id, team.name, admin_user_id
        )
        return team

    async def get_team(self, enterprise_id: str, team_id: str) -> Optional[Team]:
        """Get a team by id, within an enterprise."""
        stmt = select(TeamModel).where(
            TeamModel.team_id == team_id,
            TeamModel.enterprise_id == enterprise_id,
            TeamModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def get_team_with_members(
        self, enterprise_id: str, team_id: str
    ) -> tuple[Optional[Team], List[TeamMember]]:
        """The team and its roster, in one session."""
        team = await self.get_team(enterprise_id, team_id)
        if team is None:
            return None, []
        return team, await self.list_team_members(enterprise_id, team_id)

    async def get_team_names(self, enterprise_id: str, team_ids: List[str]) -> dict:
        """Map team ids to names, for live teams of ``enterprise_id``."""
        if not team_ids:
            return {}
        stmt = select(TeamModel.team_id, TeamModel.name).where(
            TeamModel.team_id.in_(list(team_ids)),
            TeamModel.enterprise_id == enterprise_id,
            TeamModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return {row[0]: row[1] for row in result.all()}

    async def update_team(self, enterprise_id: str, team: Team) -> bool:
        """Update team, within an enterprise."""
        team.updated_at = datetime.now(timezone.utc)
        stmt = (
            update(TeamModel)
            .where(
                TeamModel.team_id == team.team_id,
                TeamModel.enterprise_id == enterprise_id,
                TeamModel.deleted_at.is_(None),
            )
            .values(
                name=team.name,
                description=team.description,
                updated_at=team.updated_at,
            )
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def leave_team(
        self, enterprise_id: str, team_id: str, user_id: str, admin_role: str
    ) -> LeaveOutcome:
        """Remove a member, deciding the last-admin rule under a row lock.

        The lock is the whole point. "Is there another admin?" is a read, and
        the removal that depends on it is a write; between them two admins
        leaving together each see the other and both go, leaving a team with
        members and no admin — which no route can repair, because there is no
        promote endpoint. ``SELECT … FOR UPDATE`` on the team row serialises the
        pair, so the second leaver reads a roster the first has already left.

        **SQLite has no ``FOR UPDATE``** and is skipped there by dialect. That
        is not a silent degradation: SQLite is the single-tenant store, where
        team collaboration is unwired entirely (``team_service is None``), so
        the only deployment that can reach this method is the one that has the
        lock.
        """
        locked = self._with_row_lock(
            select(TeamModel).where(
                TeamModel.team_id == team_id,
                TeamModel.enterprise_id == enterprise_id,
                TeamModel.deleted_at.is_(None),
            )
        )
        team = (await self.db.execute(locked)).scalar_one_or_none()
        if team is None:
            await self.db.rollback()
            return LeaveOutcome.ABSENT

        members = (
            (
                await self.db.execute(
                    select(TeamMemberModel).where(TeamMemberModel.team_id == team_id)
                )
            )
            .scalars()
            .all()
        )
        leaver = next((m for m in members if m.user_id == user_id), None)
        if leaver is None:
            await self.db.rollback()
            return LeaveOutcome.ABSENT

        others = [m for m in members if m.user_id != user_id]
        if (
            others
            and leaver.team_role == admin_role
            and not any(m.team_role == admin_role for m in others)
        ):
            await self.db.rollback()
            return LeaveOutcome.LAST_ADMIN

        await self.db.execute(
            delete(TeamMemberModel).where(
                TeamMemberModel.team_id == team_id,
                TeamMemberModel.user_id == user_id,
            )
        )
        outcome = LeaveOutcome.LEFT
        if not others:
            now = datetime.now(timezone.utc)
            await self.db.execute(
                update(TeamModel)
                .where(TeamModel.team_id == team_id)
                .values(deleted_at=now)
            )
            # A pending offer to a retired team can be neither accepted (the
            # team is gone) nor declined (declining writes against a team
            # nobody can see), so it is ended here, in the transaction that
            # retires the team, rather than left to be discovered.
            #
            # Split by ``expires_at``, the same way every other reader settles
            # it. An offer that had already run out was ended by the clock, and
            # stamping it `revoked` with the leaver's id records a withdrawal
            # nobody performed — in the one column whose whole job is to say who
            # ended the offer, and against a person who was answering a
            # different question.
            # ``expires_at <= now`` is NULL for a row with no deadline, and NULL
            # is not true, so a never-expiring offer falls through to the revoke
            # below without a second predicate saying so.
            await self.db.execute(
                update(TeamInvitationModel)
                .where(
                    TeamInvitationModel.team_id == team_id,
                    TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
                    TeamInvitationModel.expires_at <= now,
                )
                .values(status=TeamInvitationStatus.EXPIRED.value)
            )
            await self.db.execute(
                update(TeamInvitationModel)
                .where(
                    TeamInvitationModel.team_id == team_id,
                    TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
                )
                .values(
                    status=TeamInvitationStatus.REVOKED.value,
                    revoked_by=user_id,
                    revoked_at=now,
                )
            )
            outcome = LeaveOutcome.LEFT_AND_RETIRED
        await self.db.commit()
        logger.info("Team %s: %s left (%s)", team_id, user_id, outcome.value)
        return outcome

    async def list_enterprise_teams(self, enterprise_id: str) -> List[Team]:
        """List all teams in an enterprise."""
        stmt = (
            select(TeamModel)
            .where(
                TeamModel.enterprise_id == enterprise_id,
                TeamModel.deleted_at.is_(None),
            )
            .order_by(TeamModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()
        return [_model_to_domain(m) for m in models]

    async def list_user_teams(self, user_id: str) -> List[Team]:
        """List the teams a user belongs to (full objects).

        Object-returning sibling of ``list_all_user_team_ids``: same JOIN of
        ``team_members`` through the RLS-tenanted ``teams`` table (excluding
        soft-deleted teams), so cross-enterprise membership fails closed under
        the ``faultmaven_app`` role — no explicit tenant filter is needed.
        """
        stmt = (
            select(TeamModel)
            .join(TeamMemberModel, TeamModel.team_id == TeamMemberModel.team_id)
            .where(
                TeamMemberModel.user_id == user_id,
                TeamModel.deleted_at.is_(None),
            )
            .order_by(TeamMemberModel.joined_at.desc())
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()
        return [_model_to_domain(m) for m in models]

    async def add_member(
        self,
        enterprise_id: str,
        team_id: str,
        user_id: str,
        team_role: Optional[str] = None,
    ) -> bool:
        """Add user to team (upsert), refusing a member from another enterprise.

        A team lives in one enterprise and nothing crosses an enterprise line
        (ADR-017 D2/D4), so an account anchored elsewhere is not a candidate
        member. The database enforces half of this: the ``team_members`` policy
        hops through ``teams.enterprise_id``, so a membership row for a team in
        another enterprise is refused outright under the limited role. It cannot
        enforce the other half — the policy says nothing about the *user's*
        anchor — so the comparison is made here, once, where every caller
        (present and future) inherits it rather than remembering it.

        Fails closed on an unresolvable pair: an id that names no team or no
        account is refused rather than admitted, because "I could not read the
        anchors" is not evidence that they match.
        """
        anchors = (
            await self.db.execute(
                text(
                    "SELECT (SELECT enterprise_id FROM teams WHERE team_id = :t "
                    "AND enterprise_id = :e AND deleted_at IS NULL), "
                    "(SELECT enterprise_id FROM users WHERE user_id = :u)"
                ),
                {"t": team_id, "e": enterprise_id, "u": user_id},
            )
        ).first()
        team_enterprise, user_enterprise = anchors if anchors else (None, None)
        if not team_enterprise or not user_enterprise:
            logger.warning(
                "Refusing team membership: team %s or user %s does not resolve",
                team_id,
                user_id,
            )
            return False
        if team_enterprise != user_enterprise:
            logger.warning(
                "Refusing team membership: user %s is anchored to enterprise %s, "
                "team %s is in %s",
                user_id,
                user_enterprise,
                team_id,
                team_enterprise,
            )
            return False

        now = datetime.now(timezone.utc)
        stmt = dialect_insert(self.db, TeamMemberModel).values(
            user_id=user_id,
            team_id=team_id,
            team_role=team_role,
            joined_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "team_id"],
            set_={"team_role": team_role},
        )
        await self.db.execute(stmt)
        await self.db.commit()

        logger.info(f"Added user {user_id} to team {team_id} (role={team_role})")
        return True

    async def list_team_members(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamMember]:
        """List all members of a team, within an enterprise.

        The enterprise predicate rides through ``teams`` — ``team_members``
        carries no tenant column of its own, which is the same one hop its RLS
        policy makes.
        """
        stmt = (
            select(TeamMemberModel)
            .join(TeamModel, TeamMemberModel.team_id == TeamModel.team_id)
            .where(
                TeamMemberModel.team_id == team_id,
                TeamModel.enterprise_id == enterprise_id,
                TeamModel.deleted_at.is_(None),
            )
            .order_by(TeamMemberModel.joined_at.desc())
        )
        result = await self.db.execute(stmt)
        return [_member_to_domain(row) for row in result.scalars().all()]

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        """List every team id a user belongs to (KB scope resolution).

        JOINs ``team_members`` through ``teams`` so that (a) soft-deleted teams
        are excluded and (b) under the limited ``faultmaven_app`` role the
        teams-table RLS policy fails a cross-enterprise membership row closed.

        ``team_members`` is itself RLS-tenanted by the one-hop policy through
        ``teams``, so under the limited role the membership boundary is covered
        twice over.

        **Both arms are row-level security, so both vanish together.** This
        query carries no enterprise predicate of its own: on a connection that
        bypasses RLS (the table owner, a superuser, or any ``BYPASSRLS`` role)
        neither policy applies and it degrades to "every non-deleted team this
        user has a membership row for", across enterprises. The soft-delete
        filter is the only part that holds unconditionally. Callers MUST
        therefore run under ``faultmaven_app``; do not reuse this from an
        owner-role path (``/health``, the operator break-glass paths) without
        adding an explicit enterprise predicate. See the class docstring +
        ADR-013/ADR-017.
        """
        stmt = (
            select(TeamMemberModel.team_id)
            .join(TeamModel, TeamMemberModel.team_id == TeamModel.team_id)
            .where(
                TeamMemberModel.user_id == user_id,
                TeamModel.deleted_at.is_(None),
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # -- invitations: the consent that forms a team (ADR-017 D4) ------------ #

    async def create_invitation(self, invitation: TeamInvitation) -> TeamInvitation:
        """Persist a new invitation, or return the live one that beat it."""
        model = TeamInvitationModel(
            invitation_id=invitation.invitation_id,
            enterprise_id=invitation.enterprise_id,
            team_id=invitation.team_id,
            email=invitation.email,
            invited_user_id=invitation.invited_user_id,
            invited_by=invitation.invited_by,
            status=invitation.status.value,
            created_at=invitation.created_at,
            expires_at=invitation.expires_at,
        )
        self.db.add(model)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            # Another admin invited the same address in the window between this
            # caller's read and this write. Returning their row is exactly what
            # this endpoint promises for a repeat invite; raising would answer
            # 500 to two people doing the same reasonable thing at once.
            #
            # The re-read is also what identifies the conflict: if there is no
            # live offer for this address, the error was something else — a
            # foreign key, a check — and is re-raised unchanged.
            existing = await self.find_pending_invitation(
                invitation.enterprise_id, invitation.team_id, invitation.email
            )
            if existing is None or existing.invitation_id == invitation.invitation_id:
                raise
            logger.info(
                "Team invitation for team %s already existed; returning %s",
                invitation.team_id,
                existing.invitation_id,
            )
            return existing
        logger.info(
            "Created team invitation %s for team %s",
            invitation.invitation_id,
            invitation.team_id,
        )
        return invitation

    async def get_invitation(
        self, enterprise_id: str, invitation_id: str
    ) -> Optional[TeamInvitation]:
        """Get one invitation, scoped to an enterprise."""
        stmt = select(TeamInvitationModel).where(
            TeamInvitationModel.invitation_id == invitation_id,
            TeamInvitationModel.enterprise_id == enterprise_id,
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _invitation_to_domain(model) if model else None

    async def find_pending_invitation(
        self, enterprise_id: str, team_id: str, email: str
    ) -> Optional[TeamInvitation]:
        """The live offer for ``email`` on ``team_id``, if there is one."""
        stmt = select(TeamInvitationModel).where(
            TeamInvitationModel.enterprise_id == enterprise_id,
            TeamInvitationModel.team_id == team_id,
            TeamInvitationModel.email == email,
            TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
        )
        result = await self.db.execute(stmt)
        model = result.scalars().first()
        return _invitation_to_domain(model) if model else None

    async def list_team_invitations(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamInvitation]:
        """Every invitation ever issued for a team, newest first."""
        stmt = (
            select(TeamInvitationModel)
            .where(
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.team_id == team_id,
            )
            .order_by(TeamInvitationModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return [_invitation_to_domain(m) for m in result.scalars().all()]

    async def list_invitations_for_invitee(
        self, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The PENDING invitations addressed to one account."""
        stmt = (
            select(TeamInvitationModel)
            .where(
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
                or_(
                    TeamInvitationModel.invited_user_id == user_id,
                    and_(
                        TeamInvitationModel.invited_user_id.is_(None),
                        TeamInvitationModel.email == email,
                    ),
                ),
            )
            .order_by(TeamInvitationModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return [_invitation_to_domain(m) for m in result.scalars().all()]

    async def accept_invitation(
        self,
        enterprise_id: str,
        invitation_id: str,
        user_id: str,
        team_role: str,
        at: datetime,
    ) -> tuple[AcceptOutcome, Optional[Team]]:
        """Stamp the invitation accepted AND write the membership. One transaction.

        The row lock on the invitation is taken first and everything else hangs
        off it, so a concurrent revoke either lands wholly before this (and the
        locked read then sees a row that is not pending, writing nothing) or
        waits behind it. There is no interleaving that produces a membership the
        invitee's offer did not authorise.

        The anchor comparison is repeated here rather than delegated to
        ``add_member`` for the reason ``create_team_with_admin`` repeats it:
        ``add_member`` commits, and calling it would end this transaction half
        way through — which is the whole thing this method exists to prevent.
        """
        locked = self._with_row_lock(
            select(TeamInvitationModel).where(
                TeamInvitationModel.invitation_id == invitation_id,
                TeamInvitationModel.enterprise_id == enterprise_id,
            )
        )
        invitation = (await self.db.execute(locked)).scalar_one_or_none()
        if invitation is None:
            await self.db.rollback()
            return AcceptOutcome.ABSENT, None
        if invitation.status != TeamInvitationStatus.PENDING.value:
            await self.db.rollback()
            return AcceptOutcome.NOT_PENDING, None

        team = (
            await self.db.execute(
                select(TeamModel).where(
                    TeamModel.team_id == invitation.team_id,
                    TeamModel.enterprise_id == enterprise_id,
                    TeamModel.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if team is None:
            await self.db.rollback()
            return AcceptOutcome.ABSENT, None

        anchor = (
            await self.db.execute(
                text("SELECT enterprise_id FROM users WHERE user_id = :u"),
                {"u": user_id},
            )
        ).first()
        if not anchor or not anchor[0] or anchor[0] != team.enterprise_id:
            await self.db.rollback()
            logger.warning(
                "Refusing accept: %s is not anchored to team %s's enterprise",
                user_id,
                invitation.team_id,
            )
            return AcceptOutcome.ABSENT, None

        stamped = await self.db.execute(
            update(TeamInvitationModel)
            .where(
                TeamInvitationModel.invitation_id == invitation_id,
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
            )
            .values(
                status=TeamInvitationStatus.ACCEPTED.value,
                invited_user_id=user_id,
                accepted_at=at,
            )
        )
        if not stamped.rowcount:
            # Belt to the lock's braces: on a dialect without ``FOR UPDATE``
            # this is the predicate that still refuses, and it refuses BEFORE
            # any membership is written.
            await self.db.rollback()
            return AcceptOutcome.NOT_PENDING, None

        member = dialect_insert(self.db, TeamMemberModel).values(
            user_id=user_id,
            team_id=invitation.team_id,
            team_role=team_role,
            joined_at=at,
        )
        await self.db.execute(
            member.on_conflict_do_update(
                index_elements=["user_id", "team_id"],
                set_={"team_role": team_role},
            )
        )
        await self.db.commit()
        logger.info(
            "Invitation %s accepted: %s joined team %s",
            invitation_id,
            user_id,
            invitation.team_id,
        )
        return AcceptOutcome.ACCEPTED, _model_to_domain(team)

    async def mark_invitation_revoked(
        self, enterprise_id: str, invitation_id: str, by_user_id: str, at: datetime
    ) -> bool:
        """Stamp an invitation revoked, but only if it is still pending."""
        stmt = (
            update(TeamInvitationModel)
            .where(
                TeamInvitationModel.invitation_id == invitation_id,
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
            )
            .values(
                status=TeamInvitationStatus.REVOKED.value,
                revoked_by=by_user_id,
                revoked_at=at,
            )
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def expire_invitations(
        self, enterprise_id: str, invitation_ids: List[str]
    ) -> List[str]:
        """Stamp pending invitations expired, and report WHICH ones moved.

        ``RETURNING`` rather than a rowcount, because the caller needs the
        identity of the rows and not their number: a row accepted between its
        read and this UPDATE is skipped by the pending predicate, and telling
        the caller "all of them" made it report an accepted invitation as
        expired. Supported by both dialects in use (PostgreSQL, and SQLite from
        3.35).
        """
        if not invitation_ids:
            return []
        stmt = (
            update(TeamInvitationModel)
            .where(
                TeamInvitationModel.invitation_id.in_(list(invitation_ids)),
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
            )
            .values(status=TeamInvitationStatus.EXPIRED.value)
            .returning(TeamInvitationModel.invitation_id)
        )
        result = await self.db.execute(stmt)
        moved = [row[0] for row in result.all()]
        await self.db.commit()
        return moved

    async def resolve_invitations_for_account(
        self, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """Stamp ``user_id`` on every unresolved pending invitation for ``email``.

        One statement, and idempotent by its own predicate: it matches only rows
        whose ``invited_user_id`` is still NULL, so running it twice resolves
        nothing the first run did not. The enterprise predicate is what makes
        "an address that signs up elsewhere never resolves" true rather than
        merely intended.
        """
        stmt = (
            update(TeamInvitationModel)
            .where(
                TeamInvitationModel.enterprise_id == enterprise_id,
                TeamInvitationModel.email == email,
                TeamInvitationModel.invited_user_id.is_(None),
                TeamInvitationModel.status == TeamInvitationStatus.PENDING.value,
            )
            .values(invited_user_id=user_id)
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        if result.rowcount:
            logger.info(
                "Resolved %s pending team invitation(s) to account %s",
                result.rowcount,
                user_id,
            )
        return int(result.rowcount or 0)
