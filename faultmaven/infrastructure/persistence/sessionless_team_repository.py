"""Sessionless Team Repository.

Wrapper around PostgreSQLTeamRepository that creates a session per operation via
get_db_session(), following the same pattern as SessionlessOrganizationRepository.
This removes the need for a long-lived db_session in the DI container.
"""

from typing import List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import ITeamRepository, Team, TeamMember


class SessionlessTeamRepository(ITeamRepository):
    """Sessionless wrapper for team repository.

    Creates a new database session for each operation using get_db_session().
    """

    async def create_team(self, team: Team) -> Team:
        """Create a new team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.create_team(team)

    async def get_team(self, team_id: str) -> Optional[Team]:
        """Get team by ID."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.get_team(team_id)

    async def update_team(self, team: Team) -> bool:
        """Update team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.update_team(team)

    async def delete_team(self, team_id: str) -> bool:
        """Soft delete team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.delete_team(team_id)

    async def list_organization_teams(self, organization_id: str) -> List[Team]:
        """List all teams in an organization."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_organization_teams(organization_id)

    async def list_user_teams(self, user_id: str, organization_id: str) -> List[Team]:
        """List all teams a user belongs to in an organization."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_user_teams(user_id, organization_id)

    async def add_member(
        self, team_id: str, user_id: str, team_role: Optional[str] = None
    ) -> bool:
        """Add user to team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.add_member(team_id, user_id, team_role)

    async def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove user from team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.remove_member(team_id, user_id)

    async def list_team_members(self, team_id: str) -> List[TeamMember]:
        """List all members of a team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_team_members(team_id)

    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """Check if user is member of team."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.is_team_member(team_id, user_id)

    async def list_all_user_team_ids(self, user_id: str) -> List[str]:
        """List every team id a user belongs to (KB scope resolution)."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_all_user_team_ids(user_id)
