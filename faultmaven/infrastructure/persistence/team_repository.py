"""Team Repository - SQLAlchemy ORM Implementation.

Implements ITeamRepository for team and team-membership persistence. Mirrors
PostgreSQLOrganizationRepository: the core ships the repository *substrate*
(used by the single-tenant default-team bootstrap and by KB scope resolution);
team *management* (create/invite from a UI) is the hosted admin composed
module, which drives these same methods (ADR-010 D4 / ADR-013).

Isolation posture (team_members RLS): ``team_members`` has no
``organization_id`` column, so it is not in migration 018's
``_TENANTED_TABLES``; migration 030 gives it its own subquery policy —
``USING (team_id IN (SELECT team_id FROM teams WHERE organization_id =
current_setting('app.current_org_id', true)))`` — so membership rows are
org-scoped through their team and fail closed under the limited
``faultmaven_app`` role. (Rejected alternative: add ``organization_id`` to
``team_members`` + a direct policy — duplicates the org already reachable via
``teams.organization_id`` and invites drift.) See ADR-013 + migration 030.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, func, select, update
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
        organization_id=model.organization_id,
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
            organization_id=team.organization_id,
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

    async def list_organization_teams(self, organization_id: str) -> List[Team]:
        """List all teams in an organization."""
        stmt = (
            select(TeamModel)
            .where(
                TeamModel.organization_id == organization_id,
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
        soft-deleted teams), so cross-organization membership fails closed under
        the ``faultmaven_app`` role — no explicit org filter is needed.
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
        """Add user to team (upsert)."""
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
        teams-table RLS policy fails a cross-organization membership row closed.

        ``team_members`` is itself RLS-tenanted as of migration 030 (a policy
        keyed by subquery through ``teams``), so under the limited role the
        membership boundary is covered twice over.

        **Both arms are row-level security, so both vanish together.** This
        query carries no organization predicate of its own: on a connection that
        bypasses RLS (the table owner, a superuser, or any ``BYPASSRLS`` role)
        neither policy applies and it degrades to "every non-deleted team this
        user has a membership row for", across organizations. The soft-delete
        filter is the only part that holds unconditionally. Callers MUST
        therefore run under ``faultmaven_app``; do not reuse this from an
        owner-role path (``/health``, the operator break-glass paths of
        migrations 035/036) without adding an explicit organization predicate.
        See the class docstring + ADR-013.
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
