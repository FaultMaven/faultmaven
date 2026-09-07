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
    AcceptOutcome,
    ITeamRepository,
    LeaveOutcome,
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

    async def create_team_with_admin(
        self, team: Team, admin_user_id: str, team_role: str
    ) -> Optional[Team]:
        """Create a team and its creator's membership as one transaction."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.create_team_with_admin(team, admin_user_id, team_role)

    async def get_team(self, enterprise_id: str, team_id: str) -> Optional[Team]:
        """Get a team by id, within an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.get_team(enterprise_id, team_id)

    async def get_team_with_members(
        self, enterprise_id: str, team_id: str
    ) -> tuple[Optional[Team], List[TeamMember]]:
        """The team and its roster, in one session."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.get_team_with_members(enterprise_id, team_id)

    async def get_team_names(self, enterprise_id: str, team_ids: List[str]) -> dict:
        """Map team ids to names, for live teams of an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.get_team_names(enterprise_id, team_ids)

    async def update_team(self, enterprise_id: str, team: Team) -> bool:
        """Update team, within an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.update_team(enterprise_id, team)

    async def leave_team(
        self, enterprise_id: str, team_id: str, user_id: str, admin_role: str
    ) -> LeaveOutcome:
        """Remove a member, deciding the last-admin rule under a row lock."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.leave_team(enterprise_id, team_id, user_id, admin_role)

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
        self,
        enterprise_id: str,
        team_id: str,
        user_id: str,
        team_role: Optional[str] = None,
    ) -> bool:
        """Add user to team, within an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.add_member(enterprise_id, team_id, user_id, team_role)

    async def list_team_members(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamMember]:
        """List all members of a team, within an enterprise."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_team_members(enterprise_id, team_id)

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
        self, enterprise_id: str, team_id: str, email: str
    ) -> Optional[TeamInvitation]:
        """The live offer for an address on a team, if there is one."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.find_pending_invitation(enterprise_id, team_id, email)

    async def list_team_invitations(
        self, enterprise_id: str, team_id: str
    ) -> List[TeamInvitation]:
        """Every invitation ever issued for a team, newest first."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_team_invitations(enterprise_id, team_id)

    async def list_invitations_for_invitee(
        self, enterprise_id: str, user_id: str, email: str
    ) -> List[TeamInvitation]:
        """The pending invitations addressed to one account."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.list_invitations_for_invitee(
                enterprise_id, user_id, email
            )

    async def accept_invitation(
        self,
        enterprise_id: str,
        invitation_id: str,
        user_id: str,
        team_role: str,
        at: datetime,
    ) -> tuple[AcceptOutcome, Optional[Team]]:
        """Stamp the invitation accepted AND write the membership, atomically."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.accept_invitation(
                enterprise_id, invitation_id, user_id, team_role, at
            )

    async def mark_invitation_revoked(
        self, enterprise_id: str, invitation_id: str, by_user_id: str, at: datetime
    ) -> bool:
        """Stamp an invitation revoked, but only if it is still pending."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.mark_invitation_revoked(
                enterprise_id, invitation_id, by_user_id, at
            )

    async def expire_invitations(
        self, enterprise_id: str, invitation_ids: List[str]
    ) -> List[str]:
        """Stamp pending invitations expired, and report which ones moved."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.expire_invitations(enterprise_id, invitation_ids)

    async def resolve_invitations_for_account(
        self, enterprise_id: str, email: str, user_id: str
    ) -> int:
        """Stamp an account on every unresolved pending invitation for its address."""
        async with get_db_session() as session:
            repo = PostgreSQLTeamRepository(session)
            return await repo.resolve_invitations_for_account(
                enterprise_id, email, user_id
            )
