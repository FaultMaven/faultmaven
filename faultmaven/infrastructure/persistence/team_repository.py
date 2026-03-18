"""Team Repository - SQLAlchemy ORM Implementation.

Implements ITeamRepository for team and member management.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
        org_id=model.organization_id,
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
            organization_id=team.org_id,
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

    async def list_organization_teams(self, org_id: str) -> List[Team]:
        """List all teams in an organization."""
        stmt = (
            select(TeamModel)
            .where(
                TeamModel.organization_id == org_id,
                TeamModel.deleted_at.is_(None),
            )
            .order_by(TeamModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()
        return [_model_to_domain(m) for m in models]

    async def list_user_teams(self, user_id: str, org_id: str) -> List[Team]:
        """List all teams a user belongs to in an organization."""
        stmt = (
            select(TeamModel)
            .join(TeamMemberModel, TeamModel.team_id == TeamMemberModel.team_id)
            .where(
                TeamMemberModel.user_id == user_id,
                TeamModel.organization_id == org_id,
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
        stmt = sqlite_insert(TeamMemberModel).values(
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

        logger.info(f"Added user {user_id} to team {team_id}")
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
        """Check if user is member of team.

        Replaces the old SQL function user_is_team_member() with an ORM query.
        """
        stmt = (
            select(func.count())
            .select_from(TeamMemberModel)
            .where(
                TeamMemberModel.team_id == team_id,
                TeamMemberModel.user_id == user_id,
            )
        )
        result = await self.db.execute(stmt)
        count = result.scalar()
        return count > 0
