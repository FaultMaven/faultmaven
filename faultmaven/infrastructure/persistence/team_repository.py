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

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import (
    TeamMemberModel,
    TeamModel,
)
from faultmaven.models.interfaces_user import ITeamRepository, Team, TeamMember

logger = logging.getLogger(__name__)


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

    async def create_team(self, team: Team) -> Team:
        """Create a new team."""
        model = TeamModel(
            team_id=team.team_id,
            enterprise_id=team.enterprise_id,
            name=team.name,
            description=team.description,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )
        self.db.add(model)
        await self.db.commit()

        logger.info(f"Created team: {team.team_id} ({team.name})")
        return team

    async def get_team(self, team_id: str) -> Optional[Team]:
        """Get team by ID."""
        stmt = select(TeamModel).where(
            TeamModel.team_id == team_id,
            TeamModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def update_team(self, team: Team) -> bool:
        """Update team."""
        team.updated_at = datetime.now(timezone.utc)
        stmt = (
            update(TeamModel)
            .where(
                TeamModel.team_id == team.team_id,
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

    async def delete_team(self, team_id: str) -> bool:
        """Soft delete team."""
        stmt = (
            update(TeamModel)
            .where(
                TeamModel.team_id == team_id,
                TeamModel.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

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
        self, team_id: str, user_id: str, team_role: Optional[str] = None
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
                    "AND deleted_at IS NULL), "
                    "(SELECT enterprise_id FROM users WHERE user_id = :u)"
                ),
                {"t": team_id, "u": user_id},
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

    async def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove user from team."""
        stmt = delete(TeamMemberModel).where(
            TeamMemberModel.team_id == team_id,
            TeamMemberModel.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def list_team_members(self, team_id: str) -> List[TeamMember]:
        """List all members of a team."""
        stmt = (
            select(TeamMemberModel)
            .where(TeamMemberModel.team_id == team_id)
            .order_by(TeamMemberModel.joined_at.desc())
        )
        result = await self.db.execute(stmt)
        rows = result.scalars().all()
        return [
            TeamMember(
                user_id=row.user_id,
                team_id=row.team_id,
                team_role=row.team_role,
                joined_at=row.joined_at,
            )
            for row in rows
        ]

    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """Check if user is member of team."""
        stmt = select(func.count()).where(
            TeamMemberModel.team_id == team_id,
            TeamMemberModel.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        return (result.scalar() or 0) > 0

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
