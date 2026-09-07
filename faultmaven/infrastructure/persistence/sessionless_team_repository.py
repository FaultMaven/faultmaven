"""Sessionless Team Repository.

Wrapper around PostgreSQLTeamRepository that creates a session per operation via
get_db_session(), following the same pattern as SessionlessOrganizationRepository.
This removes the need for a long-lived db_session in the DI container.
"""

from datetime import datetime
from typing import List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.team_repository import (
    PostgreSQLTeamRepository,
)
from faultmaven.models.interfaces_user import (
    ITeamRepository,
    Team,
    TeamInvitation,
    TeamMember,
)


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

    async def list_enterprise_teams(self, enterprise_id: str) -> List[Team]:
        """List all teams in an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_enterprise_teams(enterprise_id)

    async def list_user_teams(self, user_id: str) -> List[Team]:
        """List the teams a user belongs to (full objects; RLS-scoped)."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_user_teams(user_id)

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

    # -- invitations (ADR-017 D4) ------------------------------------------- #

    async def create_invitation(self, invitation: TeamInvitation) -> TeamInvitation:
        """Persist a new invitation."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.create_invitation(invitation)

    async def get_invitation(
        self, enterprise_id: str, invitation_id: str
    ) -> Optional[TeamInvitation]:
        """Get one invitation, scoped to an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.get_invitation(enterprise_id, invitation_id)

    async def find_pending_invitation(
        self, team_id: str, email: str
    ) -> Optional[TeamInvitation]:
        """The live offer for an address on a team, if there is one."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.find_pending_invitation(team_id, email)

    async def list_team_invitations(self, team_id: str) -> List[TeamInvitation]:
        """Every invitation ever issued for a team, newest first."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_team_invitations(team_id)

    async def list_invitations_for_invitee(
        self, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The pending invitations addressed to one account."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_invitations_for_invitee(
                enterprise_id, user_id, email
            )

    async def mark_invitation_accepted(
        self, invitation_id: str, user_id: str, at: datetime
    ) -> bool:
        """Stamp an invitation accepted, but only if it is still pending."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.mark_invitation_accepted(invitation_id, user_id, at)

    async def mark_invitation_revoked(
        self, invitation_id: str, by_user_id: str, at: datetime
    ) -> bool:
        """Stamp an invitation revoked, but only if it is still pending."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.mark_invitation_revoked(invitation_id, by_user_id, at)

    async def mark_invitation_expired(self, invitation_id: str) -> bool:
        """Stamp a pending invitation expired (lazy, on read or accept)."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.mark_invitation_expired(invitation_id)

    async def resolve_invitations_for_account(
        self, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """Stamp an account on every unresolved pending invitation for its address."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.resolve_invitations_for_account(
                enterprise_id, email, user_id
            )
