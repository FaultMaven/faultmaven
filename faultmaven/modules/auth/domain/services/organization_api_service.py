"""API Organization Service (TASK-021)

Purpose: API-specific service layer for organization management.

This service wraps the domain OrganizationService with API-specific logic:
- Authorization checks (owner, admin, member roles)
- Multi-tenant isolation
- Response transformation
- JWT token revocation on role changes
- Plan tier limit enforcement

Design Reference: TASK-021 Organization Management API Endpoints
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.models.interfaces_user import (
    Organization,
    OrganizationMember,
    OrgPlanTier,
)
from faultmaven.modules.auth.domain.services.organization_service import (
    OrganizationService,
)
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.services.base import BaseService
from faultmaven.modules.auth.domain.services.user_service import UserService

logger = logging.getLogger(__name__)


# Organization role constants
ROLE_OWNER = "role_org_owner"
ROLE_ADMIN = "role_org_admin"
ROLE_MEMBER = "role_org_member"

# Plan tier max members
PLAN_MAX_MEMBERS = {
    OrgPlanTier.FREE: 5,
    OrgPlanTier.PRO: 50,
    OrgPlanTier.ENTERPRISE: 999999,  # Unlimited
}


class APIOrganizationService(BaseService):
    """API service layer for organization management.

    Handles all organization API operations including authorization,
    multi-tenant isolation, and response transformation.

    Attributes:
        organization_service: Domain service for organization operations
        user_service: User service for user lookup
        auth_service: Auth service for token operations
    """

    def __init__(
        self,
        organization_service: OrganizationService,
        user_service: Optional[UserService] = None,
        auth_service: Optional[AuthService] = None,
    ):
        """Initialize API organization service.

        Args:
            organization_service: Domain service for organization operations
            user_service: User service for user lookup (optional)
            auth_service: Auth service for token revocation (optional)
        """
        super().__init__("api_organization_service")
        self.organization_service = organization_service
        self.user_service = user_service
        self.auth_service = auth_service

    # ============================================================
    # Organization CRUD
    # ============================================================

    async def create_organization(
        self,
        name: str,
        slug: str,
        creator_user_id: str,
        description: Optional[str] = None,
        plan_tier: str = "free",
    ) -> Organization:
        """Create organization and add creator as owner.

        Args:
            name: Organization name (1-100 chars)
            slug: URL-friendly identifier (must be unique)
            creator_user_id: User creating the organization (becomes owner)
            description: Optional description
            plan_tier: Subscription plan tier (free, pro, enterprise)

        Returns:
            Created organization

        Raises:
            ValidationException: Invalid slug format or already exists
        """
        # Validate slug format (lowercase, hyphens only)
        if not self._is_valid_slug(slug):
            raise ValidationException(
                "Slug must contain only lowercase letters, numbers, and hyphens"
            )

        # Convert plan tier string to enum
        try:
            plan_tier_enum = OrgPlanTier(plan_tier.lower())
        except ValueError:
            raise ValidationException(
                f"Invalid plan tier: {plan_tier}. Valid values: free, pro, enterprise"
            )

        # Create organization via domain service
        org = await self.organization_service.create_organization(
            name=name,
            slug=slug,
            creator_user_id=creator_user_id,
            description=description,
            plan_tier=plan_tier_enum,
        )

        self.logger.info(
            f"Organization created: {org.organization_id} by user {creator_user_id}"
        )
        return org

    async def get_organization(
        self,
        organization_id: str,
        user_id: str,
    ) -> Organization:
        """Get organization details (check member access).

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID

        Returns:
            Organization details

        Raises:
            NotFoundError: Organization doesn't exist
            AuthorizationError: User not a member
        """
        # Get organization
        org = await self.organization_service.get_organization(organization_id)
        if not org:
            raise NotFoundError("Organization", organization_id)

        # Check membership
        await self._require_org_member(organization_id, user_id)

        return org

    async def list_user_organizations(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List organizations user is a member of.

        Args:
            user_id: User ID
            limit: Pagination limit (max 100)
            offset: Pagination offset

        Returns:
            Tuple of (organization list with role info, total count)
        """
        # Get organizations from domain service
        orgs = await self.organization_service.list_user_organizations(user_id)

        total = len(orgs)

        # Get user's role in each organization
        org_list = []
        for org in orgs:
            role_id = await self.organization_service.get_member_role(
                org.organization_id, user_id
            )
            role = self._role_id_to_name(role_id) if role_id else "member"

            # Get member record for joined_at
            members = await self.organization_service.list_organization_members(
                org.organization_id
            )
            member = next((m for m in members if m.user_id == user_id), None)
            member_since = member.joined_at if member else org.created_at

            org_list.append(
                {
                    "organization_id": org.organization_id,
                    "name": org.name,
                    "slug": org.slug,
                    "plan_tier": (
                        org.plan_tier.value
                        if isinstance(org.plan_tier, OrgPlanTier)
                        else org.plan_tier
                    ),
                    "role": role,
                    "member_since": member_since,
                }
            )

        # Apply pagination
        paginated = org_list[offset : offset + limit]

        return paginated, total

    async def update_organization(
        self,
        organization_id: str,
        user_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Organization:
        """Update organization (owner only).

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID
            name: New name (optional)
            description: New description (optional)

        Returns:
            Updated organization

        Raises:
            NotFoundError: Organization doesn't exist
            AuthorizationError: User not owner
        """
        # Verify organization exists
        org = await self.organization_service.get_organization(organization_id)
        if not org:
            raise NotFoundError("Organization", organization_id)

        # Check owner permission
        await self._require_org_owner(organization_id, user_id)

        # Update via domain service
        success = await self.organization_service.update_organization(
            organization_id=organization_id,
            user_id=user_id,
            name=name,
            description=description,
        )

        if not success:
            raise NotFoundError("Organization", organization_id)

        # Get updated organization
        updated_org = await self.organization_service.get_organization(organization_id)
        return updated_org

    async def delete_organization(
        self,
        organization_id: str,
        user_id: str,
    ) -> bool:
        """Delete organization (owner only, soft delete).

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID

        Returns:
            True if deleted

        Raises:
            NotFoundError: Organization doesn't exist
            AuthorizationError: User not owner
            ConflictError: Organization has active cases
        """
        # Verify organization exists
        org = await self.organization_service.get_organization(organization_id)
        if not org:
            raise NotFoundError("Organization", organization_id)

        # Check owner permission
        await self._require_org_owner(organization_id, user_id)

        # TODO: Check for active cases (would need case repository)
        # For now, just delete

        # Delete via domain service (soft delete)
        success = await self.organization_service.delete_organization(
            organization_id=organization_id,
            user_id=user_id,
        )

        if success:
            self.logger.info(
                f"Organization deleted: {organization_id} by user {user_id}"
            )

        return success

    # ============================================================
    # Member Management
    # ============================================================

    async def list_organization_members(
        self,
        organization_id: str,
        user_id: str,
        role_filter: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List all members of an organization.

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID
            role_filter: Optional role filter (owner, admin, member)
            limit: Pagination limit
            offset: Pagination offset

        Returns:
            Tuple of (member list, total count)

        Raises:
            AuthorizationError: User not a member
        """
        # Check membership
        await self._require_org_member(organization_id, user_id)

        # Get members
        members = await self.organization_service.list_organization_members(
            organization_id
        )

        # Filter by role if specified
        if role_filter:
            role_id = self._role_name_to_id(role_filter)
            members = [m for m in members if m.role_id == role_id]

        total = len(members)

        # Build response with user details
        member_list = []
        for member in members:
            user_info = await self._get_user_info(member.user_id)
            member_list.append(
                {
                    "user_id": member.user_id,
                    "email": user_info.get("email", ""),
                    "full_name": user_info.get("full_name", ""),
                    "role": self._role_id_to_name(member.role_id),
                    "joined_at": member.joined_at,
                }
            )

        # Apply pagination
        paginated = member_list[offset : offset + limit]

        return paginated, total

    async def add_member(
        self,
        organization_id: str,
        requesting_user_id: str,
        email: str,
        role: str = "member",
    ) -> Dict[str, Any]:
        """Add member to organization (owner/admin only).

        Args:
            organization_id: Organization ID
            requesting_user_id: User performing the action
            email: Email of user to add
            role: Role to assign (member, admin)

        Returns:
            Added member info

        Raises:
            AuthorizationError: User lacks permission
            NotFoundError: User with email doesn't exist
            ConflictError: User already a member or max members reached
        """
        # Check admin or owner permission
        await self._require_org_admin(organization_id, requesting_user_id)

        # Get requesting user's role
        requester_role_id = await self.organization_service.get_member_role(
            organization_id, requesting_user_id
        )
        requester_role = self._role_id_to_name(requester_role_id)

        # Admins cannot add other admins (only owners can)
        if requester_role == "admin" and role == "admin":
            raise AuthorizationError("Only owners can add admin members")

        # Find user by email
        target_user = await self._get_user_by_email(email)
        if not target_user:
            raise NotFoundError("User", f"email:{email}")

        target_user_id = target_user.get("user_id")

        # Check if already a member
        existing_role = await self.organization_service.get_member_role(
            organization_id, target_user_id
        )
        if existing_role:
            raise ConflictError(
                "User is already a member of this organization",
                resource_type="OrganizationMember",
                resource_id=target_user_id,
                conflict_reason="already_member",
            )

        # Check max members limit
        org = await self.organization_service.get_organization(organization_id)
        members = await self.organization_service.list_organization_members(
            organization_id
        )
        max_members = org.max_members if org else 5
        if len(members) >= max_members:
            raise ConflictError(
                f"Organization has reached maximum member limit ({max_members})",
                resource_type="Organization",
                resource_id=organization_id,
                conflict_reason="max_members_reached",
            )

        # Add member via domain service
        role_id = self._role_name_to_id(role)
        success = await self.organization_service.add_member(
            organization_id=organization_id,
            user_id=target_user_id,
            role_id=role_id,
            added_by=requesting_user_id,
        )

        if not success:
            raise ConflictError("Failed to add member")

        self.logger.info(
            f"Member added: {target_user_id} to org {organization_id} as {role}"
        )

        return {
            "user_id": target_user_id,
            "email": email,
            "full_name": target_user.get("full_name", ""),
            "role": role,
            "joined_at": datetime.now(timezone.utc),
            "invitation_sent": True,
        }

    async def remove_member(
        self,
        organization_id: str,
        requesting_user_id: str,
        target_user_id: str,
    ) -> bool:
        """Remove member from organization.

        Args:
            organization_id: Organization ID
            requesting_user_id: User performing the action
            target_user_id: User to remove

        Returns:
            True if removed

        Raises:
            AuthorizationError: Cannot remove owner, or lacks permission
            NotFoundError: User not a member
        """
        # Check admin or owner permission
        await self._require_org_admin(organization_id, requesting_user_id)

        # Get target user's role
        target_role_id = await self.organization_service.get_member_role(
            organization_id, target_user_id
        )
        if not target_role_id:
            raise NotFoundError("OrganizationMember", target_user_id)

        target_role = self._role_id_to_name(target_role_id)

        # Cannot remove owner
        if target_role == "owner":
            raise AuthorizationError("Cannot remove organization owner")

        # Get requester's role
        requester_role_id = await self.organization_service.get_member_role(
            organization_id, requesting_user_id
        )
        requester_role = self._role_id_to_name(requester_role_id)

        # Admins cannot remove other admins
        if requester_role == "admin" and target_role == "admin":
            raise AuthorizationError("Admins cannot remove other admins")

        # Remove member via domain service
        success = await self.organization_service.remove_member(
            organization_id=organization_id,
            user_id=target_user_id,
            removed_by=requesting_user_id,
        )

        if success:
            self.logger.info(
                f"Member removed: {target_user_id} from org {organization_id}"
            )
            # Revoke user's tokens
            if self.auth_service:
                await self.auth_service.revoke_user_tokens(target_user_id)

        return success

    async def update_member_role(
        self,
        organization_id: str,
        requesting_user_id: str,
        target_user_id: str,
        role: str,
    ) -> Dict[str, Any]:
        """Update member's role (owner only).

        Args:
            organization_id: Organization ID
            requesting_user_id: User performing the action
            target_user_id: User whose role to update
            role: New role (member, admin)

        Returns:
            Updated member info

        Raises:
            AuthorizationError: Not owner or invalid role change
            NotFoundError: User not a member
            ValidationException: Invalid role
        """
        # Only owner can update roles
        await self._require_org_owner(organization_id, requesting_user_id)

        # Validate role
        valid_roles = ["member", "admin"]
        if role not in valid_roles:
            raise ValidationException(
                f"Invalid role: {role}. Valid roles: {', '.join(valid_roles)}"
            )

        # Cannot change role to owner via this endpoint
        if role == "owner":
            raise ValidationException(
                "Cannot assign owner role. Use transfer ownership endpoint."
            )

        # Check target is a member
        current_role_id = await self.organization_service.get_member_role(
            organization_id, target_user_id
        )
        if not current_role_id:
            raise NotFoundError("OrganizationMember", target_user_id)

        current_role = self._role_id_to_name(current_role_id)

        # Cannot change owner's role
        if current_role == "owner":
            raise AuthorizationError("Cannot change owner's role")

        # Update role via domain service
        new_role_id = self._role_name_to_id(role)
        success = await self.organization_service.update_member_role(
            organization_id=organization_id,
            user_id=target_user_id,
            role_id=new_role_id,
            updated_by=requesting_user_id,
        )

        if not success:
            raise ConflictError("Failed to update role")

        # Revoke user's tokens (role changed)
        if self.auth_service:
            await self.auth_service.revoke_user_tokens(target_user_id)

        self.logger.info(
            f"Member role updated: {target_user_id} in org {organization_id} to {role}"
        )

        # Get user info for response
        user_info = await self._get_user_info(target_user_id)
        members = await self.organization_service.list_organization_members(
            organization_id
        )
        member = next((m for m in members if m.user_id == target_user_id), None)

        return {
            "user_id": target_user_id,
            "email": user_info.get("email", ""),
            "full_name": user_info.get("full_name", ""),
            "role": role,
            "joined_at": member.joined_at if member else datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    async def get_member(
        self,
        organization_id: str,
        user_id: str,
    ) -> Optional[OrganizationMember]:
        """Get organization member record.

        Args:
            organization_id: Organization ID
            user_id: User ID

        Returns:
            OrganizationMember if found, None otherwise
        """
        members = await self.organization_service.list_organization_members(
            organization_id
        )
        return next((m for m in members if m.user_id == user_id), None)

    # ============================================================
    # Organization Settings
    # ============================================================

    async def get_organization_settings(
        self,
        organization_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Get organization settings.

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID

        Returns:
            Organization settings dict

        Raises:
            AuthorizationError: User not a member
            NotFoundError: Organization doesn't exist
        """
        # Check membership
        await self._require_org_member(organization_id, user_id)

        # Get organization
        org = await self.organization_service.get_organization(organization_id)
        if not org:
            raise NotFoundError("Organization", organization_id)

        # Get member count
        members = await self.organization_service.list_organization_members(
            organization_id
        )

        # Build settings response
        plan_tier = (
            org.plan_tier
            if isinstance(org.plan_tier, OrgPlanTier)
            else OrgPlanTier(org.plan_tier)
        )

        return {
            "organization_id": org.organization_id,
            "plan_tier": plan_tier.value,
            "max_members": org.max_members,
            "current_member_count": len(members),
            "max_cases_per_month": org.max_cases,
            "max_storage_gb": self._get_plan_storage(plan_tier),
            "features": self._get_plan_features(plan_tier),
            "settings": org.settings
            or {
                "allow_public_cases": False,
                "require_2fa": False,
                "session_timeout_minutes": 60,
                "default_case_priority": "medium",
            },
        }

    async def update_organization_settings(
        self,
        organization_id: str,
        user_id: str,
        settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update organization settings (owner only).

        Args:
            organization_id: Organization ID
            user_id: Requesting user ID
            settings: Settings to update

        Returns:
            Updated settings

        Raises:
            AuthorizationError: User not owner
            ValidationException: Invalid setting value
        """
        # Check owner permission
        await self._require_org_owner(organization_id, user_id)

        # Get current organization
        org = await self.organization_service.get_organization(organization_id)
        if not org:
            raise NotFoundError("Organization", organization_id)

        # Validate settings
        self._validate_settings(settings)

        # Merge with existing settings
        current_settings = org.settings or {}
        updated_settings = {**current_settings, **settings}

        # Update via domain service
        success = await self.organization_service.update_organization(
            organization_id=organization_id,
            user_id=user_id,
            settings=updated_settings,
        )

        if not success:
            raise ConflictError("Failed to update settings")

        return {
            "organization_id": organization_id,
            "settings": updated_settings,
            "updated_at": datetime.now(timezone.utc),
        }

    # ============================================================
    # Authorization Helpers
    # ============================================================

    async def _require_org_owner(
        self,
        organization_id: str,
        user_id: str,
    ) -> None:
        """Require user to be organization owner.

        Raises:
            AuthorizationError: User not owner
        """
        role_id = await self.organization_service.get_member_role(
            organization_id, user_id
        )
        if not role_id or role_id != ROLE_OWNER:
            raise AuthorizationError("Organization owner access required")

    async def _require_org_admin(
        self,
        organization_id: str,
        user_id: str,
    ) -> None:
        """Require user to be organization owner or admin.

        Raises:
            AuthorizationError: User not owner or admin
        """
        role_id = await self.organization_service.get_member_role(
            organization_id, user_id
        )
        if not role_id or role_id not in (ROLE_OWNER, ROLE_ADMIN):
            raise AuthorizationError("Organization admin access required")

    async def _require_org_member(
        self,
        organization_id: str,
        user_id: str,
    ) -> None:
        """Require user to be organization member.

        Raises:
            AuthorizationError: User not a member
        """
        role_id = await self.organization_service.get_member_role(
            organization_id, user_id
        )
        if not role_id:
            raise AuthorizationError("Organization membership required")

    # ============================================================
    # Helper Methods
    # ============================================================

    def _is_valid_slug(self, slug: str) -> bool:
        """Check if slug format is valid (lowercase, numbers, hyphens)."""
        import re

        return bool(re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", slug))

    def _role_id_to_name(self, role_id: str) -> str:
        """Convert role ID to display name."""
        role_map = {
            ROLE_OWNER: "owner",
            ROLE_ADMIN: "admin",
            ROLE_MEMBER: "member",
        }
        return role_map.get(role_id, "member")

    def _role_name_to_id(self, role_name: str) -> str:
        """Convert role name to role ID."""
        role_map = {
            "owner": ROLE_OWNER,
            "admin": ROLE_ADMIN,
            "member": ROLE_MEMBER,
        }
        return role_map.get(role_name, ROLE_MEMBER)

    async def _get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Get user info by ID."""
        if self.user_service:
            user = await self.user_service.get_user(user_id)
            if user:
                return {
                    "user_id": user.user_id,
                    "email": user.email,
                    "full_name": user.display_name or "",
                }
        return {"user_id": user_id, "email": "", "full_name": ""}

    async def _get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user info by email."""
        if self.user_service:
            user = await self.user_service.get_user_by_email(email)
            if user:
                return {
                    "user_id": user.user_id,
                    "email": user.email,
                    "full_name": user.display_name or "",
                }
        return None

    def _get_plan_features(self, plan_tier: OrgPlanTier) -> Dict[str, bool]:
        """Get features available for plan tier."""
        base_features = {
            "knowledge_base": True,
            "ai_agents": False,
            "advanced_analytics": False,
            "priority_support": False,
            "sso": False,
        }

        if plan_tier == OrgPlanTier.PRO:
            return {
                **base_features,
                "ai_agents": True,
                "advanced_analytics": True,
            }
        elif plan_tier == OrgPlanTier.ENTERPRISE:
            return {
                **base_features,
                "ai_agents": True,
                "advanced_analytics": True,
                "priority_support": True,
                "sso": True,
            }

        return base_features

    def _get_plan_storage(self, plan_tier: OrgPlanTier) -> int:
        """Get storage limit in GB for plan tier."""
        storage_map = {
            OrgPlanTier.FREE: 10,
            OrgPlanTier.PRO: 100,
            OrgPlanTier.ENTERPRISE: 1000,
        }
        return storage_map.get(plan_tier, 10)

    def _validate_settings(self, settings: Dict[str, Any]) -> None:
        """Validate settings values.

        Raises:
            ValidationException: Invalid setting value
        """
        valid_keys = {
            "allow_public_cases",
            "require_2fa",
            "session_timeout_minutes",
            "default_case_priority",
        }

        for key in settings:
            if key not in valid_keys:
                raise ValidationException(f"Unknown setting: {key}")

        # Validate session_timeout_minutes
        if "session_timeout_minutes" in settings:
            timeout = settings["session_timeout_minutes"]
            if not isinstance(timeout, int) or timeout < 15 or timeout > 480:
                raise ValidationException(
                    "session_timeout_minutes must be between 15 and 480"
                )

        # Validate default_case_priority
        if "default_case_priority" in settings:
            priority = settings["default_case_priority"]
            valid_priorities = ["low", "medium", "high", "critical"]
            if priority not in valid_priorities:
                raise ValidationException(
                    f"Invalid default_case_priority. Valid values: {', '.join(valid_priorities)}"
                )

        # Validate boolean fields
        for key in ["allow_public_cases", "require_2fa"]:
            if key in settings and not isinstance(settings[key], bool):
                raise ValidationException(f"{key} must be a boolean")
