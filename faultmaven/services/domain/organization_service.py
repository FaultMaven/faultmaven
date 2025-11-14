"""Organization Service Module

Purpose: Enterprise organization and team management service

This service provides business logic for managing multi-tenant organizations,
team collaboration, and RBAC (Role-Based Access Control).

Core Responsibilities:
- Organization lifecycle management (create, update, delete)
- Member management and role assignments
- Permission checking and access control
- Organization settings and configuration
- Multi-tenancy isolation
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import uuid

from faultmaven.services.base import BaseService
from faultmaven.models.interfaces_user import (
    IOrganizationRepository,
    Organization,
    OrganizationMember,
    OrgPlanTier,
    AuditEventType,
    AuditCategory
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException


class OrganizationService(BaseService):
    """Service for organization management and RBAC."""

    def __init__(
        self,
        organization_repository: IOrganizationRepository,
        audit_repository: Optional[Any] = None,
        settings: Optional[Any] = None
    ):
        """
        Initialize the Organization Service.

        Args:
            organization_repository: Repository for org persistence
            audit_repository: Optional audit repository for logging
            settings: Configuration settings for the service
        """
        super().__init__("organization_service")
        self.repository = organization_repository
        self.audit_repository = audit_repository
        self._settings = settings

    @trace("org_service_create_organization")
    async def create_organization(
        self,
        name: str,
        slug: str,
        creator_user_id: str,
        description: Optional[str] = None,
        plan_tier: OrgPlanTier = OrgPlanTier.FREE
    ) -> Organization:
        """
        Create a new organization.

        Args:
            name: Organization name
            slug: URL-friendly identifier (must be unique)
            creator_user_id: User creating the organization (becomes owner)
            description: Optional description
            plan_tier: Subscription plan tier

        Returns:
            Created organization

        Raises:
            ValidationException: If slug is invalid or already exists
        """
        # Validate slug format (lowercase, hyphens only)
        if not slug.replace('-', '').replace('_', '').isalnum():
            raise ValidationException(
                "Slug must contain only lowercase letters, numbers, and hyphens"
            )

        # Check if slug already exists
        existing = await self.repository.get_organization_by_slug(slug)
        if existing:
            raise ValidationException(f"Organization with slug '{slug}' already exists")

        # Create organization
        org_id = f"org_{uuid.uuid4().hex[:17]}"
        now = datetime.now(timezone.utc)

        # Set max members based on plan
        max_members = {
            OrgPlanTier.FREE: 5,
            OrgPlanTier.PRO: 50,
            OrgPlanTier.ENTERPRISE: 999999  # Unlimited
        }.get(plan_tier, 5)

        org = Organization(
            org_id=org_id,
            name=name,
            slug=slug,
            description=description,
            plan_tier=plan_tier,
            max_members=max_members,
            max_cases=None,  # Unlimited for now
            settings={},
            created_at=now,
            updated_at=now
        )

        created_org = await self.repository.create_organization(org)

        # Add creator as owner
        await self.repository.add_member(org_id, creator_user_id, "role_org_owner")

        # Audit log
        if self.audit_repository:
            await self.audit_repository.log_event(
                user_id=creator_user_id,
                event_type=AuditEventType.ORG_CREATED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="organization",
                resource_id=org_id,
                org_id=org_id,
                details={"name": name, "slug": slug, "plan_tier": plan_tier.value}
            )

        self.logger.info(f"Created organization {org_id} ({name}) by user {creator_user_id}")
        return created_org

    @trace("org_service_get_organization")
    async def get_organization(self, org_id: str) -> Optional[Organization]:
        """Get organization by ID."""
        return await self.repository.get_organization(org_id)

    @trace("org_service_get_organization_by_slug")
    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug."""
        return await self.repository.get_organization_by_slug(slug)

    @trace("org_service_update_organization")
    async def update_organization(
        self,
        org_id: str,
        user_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Update organization details.

        Args:
            org_id: Organization ID
            user_id: User performing the update
            name: Optional new name
            description: Optional new description
            settings: Optional settings updates

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Check permission
        has_permission = await self.repository.user_has_permission(
            user_id, org_id, "organization.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to update organization")

        # Get current org
        org = await self.repository.get_organization(org_id)
        if not org:
            raise ValidationException(f"Organization {org_id} not found")

        # Update fields
        if name:
            org.name = name
        if description is not None:
            org.description = description
        if settings is not None:
            org.settings = settings

        success = await self.repository.update_organization(org)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=user_id,
                event_type=AuditEventType.ORG_SETTINGS_CHANGED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="organization",
                resource_id=org_id,
                org_id=org_id,
                details={"name": name, "description": description}
            )

        return success

    @trace("org_service_delete_organization")
    async def delete_organization(self, org_id: str, user_id: str) -> bool:
        """
        Soft delete an organization.

        Args:
            org_id: Organization ID
            user_id: User performing the deletion

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission
        """
        # Check permission (only owners can delete)
        has_permission = await self.repository.user_has_permission(
            user_id, org_id, "organization.manage"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to delete organization")

        success = await self.repository.delete_organization(org_id)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=user_id,
                event_type=AuditEventType.ACCOUNT_DELETED,
                event_category=AuditCategory.ADMINISTRATION,
                resource_type="organization",
                resource_id=org_id,
                org_id=org_id
            )

        return success

    @trace("org_service_add_member")
    async def add_member(
        self,
        org_id: str,
        user_id: str,
        role_id: str,
        added_by: str
    ) -> bool:
        """
        Add user to organization with role.

        Args:
            org_id: Organization ID
            user_id: User to add
            role_id: Role to assign (e.g., 'role_org_member')
            added_by: User performing the action

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission or org is at capacity
        """
        # Check permission
        has_permission = await self.repository.user_has_permission(
            added_by, org_id, "users.write"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to add members")

        # Check capacity
        org = await self.repository.get_organization(org_id)
        if not org:
            raise ValidationException(f"Organization {org_id} not found")

        members = await self.repository.list_organization_members(org_id)
        if len(members) >= org.max_members:
            raise ValidationException(
                f"Organization at capacity ({org.max_members} members)"
            )

        success = await self.repository.add_member(org_id, user_id, role_id)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=added_by,
                event_type=AuditEventType.ROLE_ASSIGNED,
                event_category=AuditCategory.AUTHORIZATION,
                resource_type="organization_member",
                resource_id=user_id,
                org_id=org_id,
                details={"target_user_id": user_id, "role_id": role_id}
            )

        return success

    @trace("org_service_remove_member")
    async def remove_member(
        self,
        org_id: str,
        user_id: str,
        removed_by: str
    ) -> bool:
        """
        Remove user from organization.

        Args:
            org_id: Organization ID
            user_id: User to remove
            removed_by: User performing the action

        Returns:
            True if successful

        Raises:
            ValidationException: If user lacks permission or trying to remove last owner
        """
        # Check permission
        has_permission = await self.repository.user_has_permission(
            removed_by, org_id, "users.manage"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to remove members")

        # Prevent removing the last owner
        members = await self.repository.list_organization_members(org_id)
        owners = [m for m in members if m.role_id == "role_org_owner"]
        if len(owners) == 1 and owners[0].user_id == user_id:
            raise ValidationException("Cannot remove the last owner from organization")

        success = await self.repository.remove_member(org_id, user_id)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=removed_by,
                event_type=AuditEventType.ROLE_REMOVED,
                event_category=AuditCategory.AUTHORIZATION,
                resource_type="organization_member",
                resource_id=user_id,
                org_id=org_id,
                details={"target_user_id": user_id}
            )

        return success

    @trace("org_service_update_member_role")
    async def update_member_role(
        self,
        org_id: str,
        user_id: str,
        role_id: str,
        updated_by: str
    ) -> bool:
        """
        Update user's role in organization.

        Args:
            org_id: Organization ID
            user_id: User whose role to update
            role_id: New role to assign
            updated_by: User performing the action

        Returns:
            True if successful
        """
        # Check permission
        has_permission = await self.repository.user_has_permission(
            updated_by, org_id, "users.manage"
        )
        if not has_permission:
            raise ValidationException("User lacks permission to update member roles")

        success = await self.repository.update_member_role(org_id, user_id, role_id)

        # Audit log
        if self.audit_repository and success:
            await self.audit_repository.log_event(
                user_id=updated_by,
                event_type=AuditEventType.ROLE_ASSIGNED,
                event_category=AuditCategory.AUTHORIZATION,
                resource_type="organization_member",
                resource_id=user_id,
                org_id=org_id,
                details={"target_user_id": user_id, "role_id": role_id}
            )

        return success

    @trace("org_service_list_user_organizations")
    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to."""
        return await self.repository.list_user_organizations(user_id)

    @trace("org_service_list_organization_members")
    async def list_organization_members(self, org_id: str) -> List[OrganizationMember]:
        """List all members of an organization."""
        return await self.repository.list_organization_members(org_id)

    @trace("org_service_get_member_role")
    async def get_member_role(self, org_id: str, user_id: str) -> Optional[str]:
        """Get user's role in organization."""
        return await self.repository.get_member_role(org_id, user_id)

    @trace("org_service_user_has_permission")
    async def user_has_permission(
        self,
        user_id: str,
        org_id: str,
        permission: str
    ) -> bool:
        """
        Check if user has permission in organization.

        Args:
            user_id: User ID
            org_id: Organization ID
            permission: Permission string (e.g., 'cases.write')

        Returns:
            True if user has permission
        """
        return await self.repository.user_has_permission(user_id, org_id, permission)
