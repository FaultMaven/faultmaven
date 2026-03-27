"""Team Service Module

Purpose: Team collaboration management service

This service provides business logic for managing teams within organizations,
enabling sub-organization collaboration and resource sharing.

Core Responsibilities:
- Team lifecycle management (create, update, delete)
- Team member management
- Team-based access control
- Team settings and configuration
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.domain.models.organization import (
    AuditCategory,
    AuditEventType,
    IOrganizationRepository,
    ITeamRepository,
    Team,
    TeamMember,
)

logger = logging.getLogger(__name__)


class TeamService:
    """Service for team management and collaboration."""

    def __init__(
        self,
        team_repository: ITeamRepository,
        organization_repository: IOrganizationRepository,
        audit_repository: Any | None = None,
        settings: Any | None = None,
    ):
        """
        Initialize the Team Service.

        Args:
            team_repository: Repository for team persistence
            organization_repository: Repository for org permission checks
            audit_repository: Optional audit repository for logging
            settings: Configuration settings for the service
        """
        self.repository = team_repository
        self.org_repository = organization_repository
        self.audit_repository = audit_repository
        self._settings = settings

    @trace("team_service_create_team")
    async def create_team(
        self,
        organization_id: str,
        name: str,
        creator_user_id: str,
        description: str | None = None,
    ) -> Team:
        """
        Create a new team within an organization.

        Args:
            organization_id: Organization ID
            name: Team name
            creator_user_id: User creating the team (becomes team lead)
            description: Optional description

        Returns:
            Created team

        Raises:
            ValidationException: If user lacks permission
        """
        # Check permission
        has_permission = await self.org_repository.user_has_permission(
            creator_user_id, organization_id, "teams.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to create teams")

        # Create team
        team_id = f"team_{uuid.uuid4().hex[:17]}"
        now = datetime.now(UTC)

        team = Team(
            team_id=team_id,
            organization_id=organization_id,
            name=name,
            description=description,
            settings={},
            created_at=now,
            updated_at=now,
        )

        created_team = await self.repository.create_team(team)

        # Add creator as team lead
        await self.repository.add_member(team_id, creator_user_id, team_role="lead")

        # Audit log
        if self.audit_repository:
            await self.audit_repository.log_event(
                user_id=creator_user_id,
                event_type=AuditEventType.TEAM_CREATED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="team",
                resource_id=team_id,
                organization_id=organization_id,
                details={"name": name},
            )

        logger.info(f"Created team {team_id} ({name}) in org {organization_id}")
        return created_team

    @trace("team_service_get_team")
    async def get_team(self, team_id: str) -> Team | None:
        """Get team by ID."""
        return await self.repository.get_team(team_id)

    @trace("team_service_update_team")
    async def update_team(
        self,
        team_id: str,
        user_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool:
        """
        Update team details.

        Args:
            team_id: Team ID
            user_id: User performing the update
            name: Optional new name
            description: Optional new description
            settings: Optional settings updates

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Get team to check org
        team = await self.repository.get_team(team_id)
        if not team:
            raise ValidationException(f"Team {team_id} not found")

        # Check permission
        has_permission = await self.org_repository.user_has_permission(
            user_id, team.organization_id, "teams.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to update team")

        # Update fields
        if name:
            team.name = name
        if description is not None:
            team.description = description

        return await self.repository.update_team(team)

    @trace("team_service_delete_team")
    async def delete_team(self, team_id: str, user_id: str) -> bool:
        """
        Soft delete a team.

        Args:
            team_id: Team ID
            user_id: User performing the deletion

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Get team to check org
        team = await self.repository.get_team(team_id)
        if not team:
            raise ValidationException(f"Team {team_id} not found")

        # Check permission
        has_permission = await self.org_repository.user_has_permission(
            user_id, team.organization_id, "teams.manage"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to delete team")

        return await self.repository.delete_team(team_id)

    @trace("team_service_add_member")
    async def add_member(
        self,
        team_id: str,
        user_id: str,
        added_by: str,
        team_role: str | None = "member",
    ) -> bool:
        """
        Add user to team.

        Args:
            team_id: Team ID
            user_id: User to add
            added_by: User performing the action
            team_role: Team-specific role ('lead', 'member')

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Get team to check org
        team = await self.repository.get_team(team_id)
        if not team:
            raise ValidationException(f"Team {team_id} not found")

        # Check permission
        has_permission = await self.org_repository.user_has_permission(
            added_by, team.organization_id, "teams.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to add team members")

        success = await self.repository.add_member(team_id, user_id, team_role)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=added_by,
                event_type=AuditEventType.TEAM_MEMBER_ADDED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="team_member",
                resource_id=user_id,
                organization_id=team.organization_id,
                details={
                    "team_id": team_id,
                    "target_user_id": user_id,
                    "role": team_role,
                },
            )

        return success

    @trace("team_service_remove_member")
    async def remove_member(self, team_id: str, user_id: str, removed_by: str) -> bool:
        """
        Remove user from team.

        Args:
            team_id: Team ID
            user_id: User to remove
            removed_by: User performing the action

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Get team to check org
        team = await self.repository.get_team(team_id)
        if not team:
            raise ValidationException(f"Team {team_id} not found")

        # Check permission
        has_permission = await self.org_repository.user_has_permission(
            removed_by, team.organization_id, "teams.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to remove team members")

        success = await self.repository.remove_member(team_id, user_id)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=removed_by,
                event_type=AuditEventType.TEAM_MEMBER_REMOVED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="team_member",
                resource_id=user_id,
                organization_id=team.organization_id,
                details={"team_id": team_id, "target_user_id": user_id},
            )

        return success

    @trace("team_service_list_organization_teams")
    async def list_organization_teams(self, organization_id: str) -> list[Team]:
        """List all teams in an organization."""
        return await self.repository.list_organization_teams(organization_id)

    @trace("team_service_list_user_teams")
    async def list_user_teams(self, user_id: str, organization_id: str) -> list[Team]:
        """List all teams a user belongs to in an organization."""
        return await self.repository.list_user_teams(user_id, organization_id)

    @trace("team_service_list_team_members")
    async def list_team_members(self, team_id: str) -> list[TeamMember]:
        """List all members of a team."""
        return await self.repository.list_team_members(team_id)

    @trace("team_service_is_team_member")
    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """Check if user is member of team."""
        return await self.repository.is_team_member(team_id, user_id)
