"""Team Repository - PostgreSQL Implementation.

Implements ITeamRepository for team and member management.
"""

from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from faultmaven.models.interfaces_user import (
    ITeamRepository,
    Team,
    TeamMember
)

logger = logging.getLogger(__name__)


class PostgreSQLTeamRepository(ITeamRepository):
    """PostgreSQL implementation of team repository.

    Manages teams and team members within organizations.
    """

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session
        """
        self.db = db_session

    async def create_team(self, team: Team) -> Team:
        """Create a new team."""
        query = text("""
            INSERT INTO teams (
                team_id, org_id, name, description, settings, created_at, updated_at
            ) VALUES (
                :team_id, :org_id, :name, :description, :settings::jsonb, :created_at, :updated_at
            )
            RETURNING team_id
        """)

        await self.db.execute(query, {
            "team_id": team.team_id,
            "org_id": team.org_id,
            "name": team.name,
            "description": team.description,
            "settings": team.settings or {},
            "created_at": team.created_at,
            "updated_at": team.updated_at
        })
        await self.db.commit()

        logger.info(f"Created team: {team.team_id} ({team.name})")
        return team

    async def get_team(self, team_id: str) -> Optional[Team]:
        """Get team by ID."""
        query = text("""
            SELECT team_id, org_id, name, description, settings, created_at, updated_at, deleted_at
            FROM teams
            WHERE team_id = :team_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {"team_id": team_id})
        row = result.fetchone()

        if not row:
            return None

        return Team(
            team_id=row.team_id,
            org_id=row.org_id,
            name=row.name,
            description=row.description,
            settings=row.settings or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at
        )

    async def update_team(self, team: Team) -> bool:
        """Update team."""
        team.updated_at = datetime.now(timezone.utc)

        query = text("""
            UPDATE teams
            SET name = :name,
                description = :description,
                settings = :settings::jsonb,
                updated_at = :updated_at
            WHERE team_id = :team_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "team_id": team.team_id,
            "name": team.name,
            "description": team.description,
            "settings": team.settings or {},
            "updated_at": team.updated_at
        })
        await self.db.commit()

        return result.rowcount > 0

    async def delete_team(self, team_id: str) -> bool:
        """Soft delete team."""
        query = text("""
            UPDATE teams
            SET deleted_at = :deleted_at
            WHERE team_id = :team_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "team_id": team_id,
            "deleted_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        return result.rowcount > 0

    async def list_organization_teams(self, org_id: str) -> List[Team]:
        """List all teams in an organization."""
        query = text("""
            SELECT team_id, org_id, name, description, settings, created_at, updated_at, deleted_at
            FROM teams
            WHERE org_id = :org_id AND deleted_at IS NULL
            ORDER BY created_at DESC
        """)

        result = await self.db.execute(query, {"org_id": org_id})
        rows = result.fetchall()

        return [
            Team(
                team_id=row.team_id,
                org_id=row.org_id,
                name=row.name,
                description=row.description,
                settings=row.settings or {},
                created_at=row.created_at,
                updated_at=row.updated_at,
                deleted_at=row.deleted_at
            )
            for row in rows
        ]

    async def list_user_teams(self, user_id: str, org_id: str) -> List[Team]:
        """List all teams a user belongs to in an organization."""
        query = text("""
            SELECT t.team_id, t.org_id, t.name, t.description, t.settings,
                   t.created_at, t.updated_at, t.deleted_at
            FROM teams t
            JOIN team_members tm ON t.team_id = tm.team_id
            WHERE tm.user_id = :user_id AND t.org_id = :org_id AND t.deleted_at IS NULL
            ORDER BY tm.joined_at DESC
        """)

        result = await self.db.execute(query, {"user_id": user_id, "org_id": org_id})
        rows = result.fetchall()

        return [
            Team(
                team_id=row.team_id,
                org_id=row.org_id,
                name=row.name,
                description=row.description,
                settings=row.settings or {},
                created_at=row.created_at,
                updated_at=row.updated_at,
                deleted_at=row.deleted_at
            )
            for row in rows
        ]

    async def add_member(
        self,
        team_id: str,
        user_id: str,
        team_role: Optional[str] = None
    ) -> bool:
        """Add user to team."""
        query = text("""
            INSERT INTO team_members (user_id, team_id, team_role, joined_at)
            VALUES (:user_id, :team_id, :team_role, :joined_at)
            ON CONFLICT (user_id, team_id) DO UPDATE
            SET team_role = EXCLUDED.team_role
        """)

        await self.db.execute(query, {
            "user_id": user_id,
            "team_id": team_id,
            "team_role": team_role,
            "joined_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        logger.info(f"Added user {user_id} to team {team_id}")
        return True

    async def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove user from team."""
        query = text("""
            DELETE FROM team_members
            WHERE team_id = :team_id AND user_id = :user_id
        """)

        result = await self.db.execute(query, {"team_id": team_id, "user_id": user_id})
        await self.db.commit()

        return result.rowcount > 0

    async def list_team_members(self, team_id: str) -> List[TeamMember]:
        """List all members of a team."""
        query = text("""
            SELECT user_id, team_id, team_role, joined_at
            FROM team_members
            WHERE team_id = :team_id
            ORDER BY joined_at DESC
        """)

        result = await self.db.execute(query, {"team_id": team_id})
        rows = result.fetchall()

        return [
            TeamMember(
                user_id=row.user_id,
                team_id=row.team_id,
                team_role=row.team_role,
                joined_at=row.joined_at
            )
            for row in rows
        ]

    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """Check if user is member of team.

        Uses the SQL function created in migration 003.
        """
        query = text("""
            SELECT user_is_team_member(:user_id, :team_id)
        """)

        result = await self.db.execute(query, {"user_id": user_id, "team_id": team_id})
        row = result.fetchone()

        return bool(row[0]) if row else False
