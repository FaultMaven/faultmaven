"""User, organization, and team management interfaces.

This module defines the interface contracts for enterprise user management,
following FaultMaven's interface-based dependency injection pattern.

Implemented by:
- PostgreSQLOrganizationRepository
- PostgreSQLTeamRepository
- PostgreSQLUserRepository (enhanced)
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field


# ============================================================================
# Enums
# ============================================================================

class OrgPlanTier(str, Enum):
    """Organization subscription plan levels."""
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class AuditEventType(str, Enum):
    """User audit event types."""
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    PASSWORD_CHANGED = "password_changed"
    ACCOUNT_CREATED = "account_created"
    ROLE_ASSIGNED = "role_assigned"
    CASE_SHARED = "case_shared"
    KB_DOCUMENT_SHARED = "kb_document_shared"
    TEAM_CREATED = "team_created"


class AuditCategory(str, Enum):
    """Audit event categories."""
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    DATA_ACCESS = "data_access"
    ADMINISTRATION = "administration"
    SECURITY = "security"


# ============================================================================
# Models
# ============================================================================

class Organization(BaseModel):
    """Organization (workspace/tenant) model."""
    org_id: str
    name: str
    slug: str
    description: Optional[str] = None
    plan_tier: OrgPlanTier = OrgPlanTier.FREE
    max_members: int = 5
    max_cases: Optional[int] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class OrganizationMember(BaseModel):
    """User membership in organization."""
    user_id: str
    org_id: str
    role_id: str  # References roles.role_id
    joined_at: datetime
    last_active_at: Optional[datetime] = None


class Team(BaseModel):
    """Team (sub-organization group) model."""
    team_id: str
    org_id: str
    name: str
    description: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class TeamMember(BaseModel):
    """User membership in team."""
    user_id: str
    team_id: str
    team_role: Optional[str] = None  # 'lead', 'member', or custom
    joined_at: datetime


class Role(BaseModel):
    """RBAC role definition."""
    role_id: str
    name: str
    description: Optional[str] = None
    scope: str  # 'system', 'organization', 'team'
    is_system_role: bool = False
    created_at: datetime


class Permission(BaseModel):
    """RBAC permission definition."""
    permission_id: str
    resource: str  # 'cases', 'knowledge_base', 'teams', etc.
    action: str  # 'read', 'write', 'delete', 'manage'
    description: Optional[str] = None


class UserAuditLog(BaseModel):
    """User audit log entry."""
    audit_id: int
    user_id: str
    event_type: AuditEventType
    event_category: AuditCategory
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    session_id: Optional[str] = None
    org_id: Optional[str] = None
    event_at: datetime
    success: bool = True


# ============================================================================
# Repository Interfaces
# ============================================================================

class IOrganizationRepository(ABC):
    """Interface for organization data persistence operations."""

    @abstractmethod
    async def create_organization(self, org: Organization) -> Organization:
        """Create a new organization.

        Args:
            org: Organization object to create

        Returns:
            Created organization with generated ID
        """
        pass

    @abstractmethod
    async def get_organization(self, org_id: str) -> Optional[Organization]:
        """Get organization by ID.

        Args:
            org_id: Organization identifier

        Returns:
            Organization if found, None otherwise
        """
        pass

    @abstractmethod
    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug.

        Args:
            slug: Organization slug (URL-friendly identifier)

        Returns:
            Organization if found, None otherwise
        """
        pass

    @abstractmethod
    async def update_organization(self, org: Organization) -> bool:
        """Update organization.

        Args:
            org: Organization object with updates

        Returns:
            True if update was successful
        """
        pass

    @abstractmethod
    async def delete_organization(self, org_id: str) -> bool:
        """Soft delete organization.

        Args:
            org_id: Organization identifier

        Returns:
            True if deletion was successful
        """
        pass

    @abstractmethod
    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to.

        Args:
            user_id: User identifier

        Returns:
            List of organizations
        """
        pass

    @abstractmethod
    async def add_member(self, org_id: str, user_id: str, role_id: str) -> bool:
        """Add user to organization with role.

        Args:
            org_id: Organization identifier
            user_id: User identifier
            role_id: Role to assign

        Returns:
            True if member was added successfully
        """
        pass

    @abstractmethod
    async def remove_member(self, org_id: str, user_id: str) -> bool:
        """Remove user from organization.

        Args:
            org_id: Organization identifier
            user_id: User identifier

        Returns:
            True if member was removed successfully
        """
        pass

    @abstractmethod
    async def update_member_role(self, org_id: str, user_id: str, role_id: str) -> bool:
        """Update user's role in organization.

        Args:
            org_id: Organization identifier
            user_id: User identifier
            role_id: New role to assign

        Returns:
            True if role was updated successfully
        """
        pass

    @abstractmethod
    async def list_organization_members(self, org_id: str) -> List[OrganizationMember]:
        """List all members of an organization.

        Args:
            org_id: Organization identifier

        Returns:
            List of organization members
        """
        pass

    @abstractmethod
    async def get_member_role(self, org_id: str, user_id: str) -> Optional[str]:
        """Get user's role in organization.

        Args:
            org_id: Organization identifier
            user_id: User identifier

        Returns:
            Role ID if user is member, None otherwise
        """
        pass

    @abstractmethod
    async def user_has_permission(
        self,
        user_id: str,
        org_id: str,
        permission: str
    ) -> bool:
        """Check if user has permission in organization.

        Args:
            user_id: User identifier
            org_id: Organization identifier
            permission: Permission string (e.g., 'cases.write')

        Returns:
            True if user has permission
        """
        pass


class ITeamRepository(ABC):
    """Interface for team data persistence operations."""

    @abstractmethod
    async def create_team(self, team: Team) -> Team:
        """Create a new team.

        Args:
            team: Team object to create

        Returns:
            Created team with generated ID
        """
        pass

    @abstractmethod
    async def get_team(self, team_id: str) -> Optional[Team]:
        """Get team by ID.

        Args:
            team_id: Team identifier

        Returns:
            Team if found, None otherwise
        """
        pass

    @abstractmethod
    async def update_team(self, team: Team) -> bool:
        """Update team.

        Args:
            team: Team object with updates

        Returns:
            True if update was successful
        """
        pass

    @abstractmethod
    async def delete_team(self, team_id: str) -> bool:
        """Soft delete team.

        Args:
            team_id: Team identifier

        Returns:
            True if deletion was successful
        """
        pass

    @abstractmethod
    async def list_organization_teams(self, org_id: str) -> List[Team]:
        """List all teams in an organization.

        Args:
            org_id: Organization identifier

        Returns:
            List of teams
        """
        pass

    @abstractmethod
    async def list_user_teams(self, user_id: str, org_id: str) -> List[Team]:
        """List all teams a user belongs to in an organization.

        Args:
            user_id: User identifier
            org_id: Organization identifier

        Returns:
            List of teams
        """
        pass

    @abstractmethod
    async def add_member(
        self,
        team_id: str,
        user_id: str,
        team_role: Optional[str] = None
    ) -> bool:
        """Add user to team.

        Args:
            team_id: Team identifier
            user_id: User identifier
            team_role: Optional team-specific role ('lead', 'member')

        Returns:
            True if member was added successfully
        """
        pass

    @abstractmethod
    async def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove user from team.

        Args:
            team_id: Team identifier
            user_id: User identifier

        Returns:
            True if member was removed successfully
        """
        pass

    @abstractmethod
    async def list_team_members(self, team_id: str) -> List[TeamMember]:
        """List all members of a team.

        Args:
            team_id: Team identifier

        Returns:
            List of team members
        """
        pass

    @abstractmethod
    async def is_team_member(self, team_id: str, user_id: str) -> bool:
        """Check if user is member of team.

        Args:
            team_id: Team identifier
            user_id: User identifier

        Returns:
            True if user is team member
        """
        pass


class IAuditRepository(ABC):
    """Interface for audit log persistence operations."""

    @abstractmethod
    async def log_event(
        self,
        user_id: str,
        event_type: AuditEventType,
        event_category: AuditCategory,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        org_id: Optional[str] = None,
        success: bool = True
    ) -> bool:
        """Log an audit event.

        Args:
            user_id: User who performed the action
            event_type: Type of event
            event_category: Event category
            resource_type: Type of resource affected
            resource_id: ID of resource affected
            details: Additional event details
            ip_address: Client IP address
            user_agent: Client user agent
            session_id: Session identifier
            org_id: Organization context
            success: Whether action succeeded

        Returns:
            True if event was logged successfully
        """
        pass

    @abstractmethod
    async def get_user_audit_log(
        self,
        user_id: str,
        limit: int = 100,
        offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for a user.

        Args:
            user_id: User identifier
            limit: Maximum results to return
            offset: Pagination offset

        Returns:
            List of audit log entries
        """
        pass

    @abstractmethod
    async def get_organization_audit_log(
        self,
        org_id: str,
        limit: int = 100,
        offset: int = 0
    ) -> List[UserAuditLog]:
        """Get audit log entries for an organization.

        Args:
            org_id: Organization identifier
            limit: Maximum results to return
            offset: Pagination offset

        Returns:
            List of audit log entries
        """
        pass
