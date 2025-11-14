"""Organization Management API Routes

Purpose: REST API endpoints for organization and RBAC management

This module provides REST API endpoints for managing organizations,
team collaboration, and role-based access control (RBAC).

Key Endpoints:
- Organization CRUD operations
- Member management with roles
- Permission checking
- Organization discovery
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from faultmaven.models.interfaces_user import (
    Organization,
    OrganizationMember,
    OrgPlanTier
)
from faultmaven.services.domain.organization_service import OrganizationService
from faultmaven.api.v1.auth_dependencies import (
    require_authentication,
    get_current_user_id
)
from faultmaven.exceptions import (
    ValidationException,
    ServiceException,
    NotFoundException,
    PermissionDeniedException
)
from faultmaven.container import container

# Create router
router = APIRouter(prefix="/organizations", tags=["organizations"])

# Set up logging
logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models
# ============================================================================

class OrganizationCreateRequest(BaseModel):
    """Request to create a new organization"""
    name: str = Field(..., description="Organization name", min_length=1, max_length=200)
    slug: str = Field(..., description="URL-friendly identifier", min_length=1, max_length=100)
    description: Optional[str] = Field(None, description="Organization description", max_length=1000)
    plan_tier: OrgPlanTier = Field(OrgPlanTier.FREE, description="Subscription plan tier")


class OrganizationUpdateRequest(BaseModel):
    """Request to update organization details"""
    name: Optional[str] = Field(None, description="Updated organization name", max_length=200)
    description: Optional[str] = Field(None, description="Updated description", max_length=1000)
    settings: Optional[Dict[str, Any]] = Field(None, description="Organization settings")


class OrganizationResponse(BaseModel):
    """Organization details response"""
    org_id: str
    name: str
    slug: str
    description: Optional[str]
    plan_tier: OrgPlanTier
    max_members: int
    max_cases: Optional[int]
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MemberAddRequest(BaseModel):
    """Request to add member to organization"""
    user_id: str = Field(..., description="User ID to add")
    role_id: str = Field(..., description="Role to assign (e.g., 'role_org_member')")


class MemberRoleUpdateRequest(BaseModel):
    """Request to update member role"""
    role_id: str = Field(..., description="New role to assign")


class MemberResponse(BaseModel):
    """Organization member response"""
    user_id: str
    org_id: str
    role_id: str
    joined_at: datetime
    last_active_at: Optional[datetime]

    class Config:
        from_attributes = True


class PermissionCheckRequest(BaseModel):
    """Request to check user permission"""
    permission: str = Field(..., description="Permission to check (e.g., 'cases.write')")


class PermissionCheckResponse(BaseModel):
    """Permission check result"""
    has_permission: bool
    permission: str
    user_id: str
    org_id: str


# ============================================================================
# Dependencies
# ============================================================================

async def get_organization_service() -> OrganizationService:
    """Get OrganizationService instance from container"""
    service = container.get_organization_service()
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Organization service not available"
        )
    return service


# ============================================================================
# Organization Endpoints
# ============================================================================

@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Organization",
    description="Create a new organization. The creator becomes the organization owner."
)
async def create_organization(
    request: OrganizationCreateRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> OrganizationResponse:
    """Create a new organization with the authenticated user as owner."""
    try:
        org = await service.create_organization(
            name=request.name,
            slug=request.slug,
            creator_user_id=user_id,
            description=request.description,
            plan_tier=request.plan_tier
        )

        logger.info(f"Organization created: {org.org_id} by user {user_id}")

        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=org.plan_tier,
            max_members=org.max_members,
            max_cases=org.max_cases,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ServiceException as e:
        logger.error(f"Service error creating organization: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Get Organization",
    description="Get organization details by ID."
)
async def get_organization(
    org_id: str = Path(..., description="Organization ID"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> OrganizationResponse:
    """Get organization details."""
    try:
        org = await service.get_organization(org_id)

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization {org_id} not found"
            )

        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=org.plan_tier,
            max_members=org.max_members,
            max_cases=org.max_cases,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/by-slug/{slug}",
    response_model=OrganizationResponse,
    summary="Get Organization by Slug",
    description="Get organization details by slug (URL-friendly identifier)."
)
async def get_organization_by_slug(
    slug: str = Path(..., description="Organization slug"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> OrganizationResponse:
    """Get organization details by slug."""
    try:
        org = await service.get_organization_by_slug(slug)

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization with slug '{slug}' not found"
            )

        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=org.plan_tier,
            max_members=org.max_members,
            max_cases=org.max_cases,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting organization by slug {slug}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Update Organization",
    description="Update organization details. Requires 'organization.write' permission."
)
async def update_organization(
    org_id: str = Path(..., description="Organization ID"),
    request: OrganizationUpdateRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> OrganizationResponse:
    """Update organization details."""
    try:
        success = await service.update_organization(
            org_id=org_id,
            user_id=user_id,
            name=request.name,
            description=request.description,
            settings=request.settings
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization {org_id} not found"
            )

        # Fetch updated organization
        org = await service.get_organization(org_id)

        logger.info(f"Organization updated: {org_id} by user {user_id}")

        return OrganizationResponse(
            org_id=org.org_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=org.plan_tier,
            max_members=org.max_members,
            max_cases=org.max_cases,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Organization",
    description="Soft delete an organization. Requires 'organization.manage' permission (owner only)."
)
async def delete_organization(
    org_id: str = Path(..., description="Organization ID"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
):
    """Soft delete an organization."""
    try:
        success = await service.delete_organization(org_id, user_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization {org_id} not found"
            )

        logger.info(f"Organization deleted: {org_id} by user {user_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "",
    response_model=List[OrganizationResponse],
    summary="List User Organizations",
    description="List all organizations the authenticated user belongs to."
)
async def list_user_organizations(
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> List[OrganizationResponse]:
    """List all organizations the user belongs to."""
    try:
        orgs = await service.list_user_organizations(user_id)

        return [
            OrganizationResponse(
                org_id=org.org_id,
                name=org.name,
                slug=org.slug,
                description=org.description,
                plan_tier=org.plan_tier,
                max_members=org.max_members,
                max_cases=org.max_cases,
                settings=org.settings or {},
                created_at=org.created_at,
                updated_at=org.updated_at
            )
            for org in orgs
        ]

    except Exception as e:
        logger.error(f"Error listing organizations for user {user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ============================================================================
# Member Management Endpoints
# ============================================================================

@router.post(
    "/{org_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add Member",
    description="Add user to organization with role. Requires 'users.write' permission."
)
async def add_member(
    org_id: str = Path(..., description="Organization ID"),
    request: MemberAddRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> MemberResponse:
    """Add member to organization."""
    try:
        success = await service.add_member(
            org_id=org_id,
            user_id=request.user_id,
            role_id=request.role_id,
            added_by=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add member"
            )

        # Fetch the added member
        members = await service.list_organization_members(org_id)
        member = next((m for m in members if m.user_id == request.user_id), None)

        if not member:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Member added but not found"
            )

        logger.info(f"Member added to organization: user {request.user_id} to org {org_id}")

        return MemberResponse(
            user_id=member.user_id,
            org_id=member.org_id,
            role_id=member.role_id,
            joined_at=member.joined_at,
            last_active_at=member.last_active_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding member to organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete(
    "/{org_id}/members/{target_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove Member",
    description="Remove user from organization. Requires 'users.manage' permission."
)
async def remove_member(
    org_id: str = Path(..., description="Organization ID"),
    target_user_id: str = Path(..., description="User ID to remove"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
):
    """Remove member from organization."""
    try:
        success = await service.remove_member(
            org_id=org_id,
            user_id=target_user_id,
            removed_by=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Member {target_user_id} not found in organization {org_id}"
            )

        logger.info(f"Member removed from organization: user {target_user_id} from org {org_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing member from organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put(
    "/{org_id}/members/{target_user_id}/role",
    response_model=MemberResponse,
    summary="Update Member Role",
    description="Update user's role in organization. Requires 'users.manage' permission."
)
async def update_member_role(
    org_id: str = Path(..., description="Organization ID"),
    target_user_id: str = Path(..., description="User ID"),
    request: MemberRoleUpdateRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> MemberResponse:
    """Update member's role in organization."""
    try:
        success = await service.update_member_role(
            org_id=org_id,
            user_id=target_user_id,
            role_id=request.role_id,
            updated_by=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Member {target_user_id} not found in organization {org_id}"
            )

        # Fetch updated member
        members = await service.list_organization_members(org_id)
        member = next((m for m in members if m.user_id == target_user_id), None)

        if not member:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Member updated but not found"
            )

        logger.info(f"Member role updated: user {target_user_id} in org {org_id} to {request.role_id}")

        return MemberResponse(
            user_id=member.user_id,
            org_id=member.org_id,
            role_id=member.role_id,
            joined_at=member.joined_at,
            last_active_at=member.last_active_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating member role in organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{org_id}/members",
    response_model=List[MemberResponse],
    summary="List Organization Members",
    description="List all members of an organization."
)
async def list_organization_members(
    org_id: str = Path(..., description="Organization ID"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> List[MemberResponse]:
    """List all members of an organization."""
    try:
        members = await service.list_organization_members(org_id)

        return [
            MemberResponse(
                user_id=member.user_id,
                org_id=member.org_id,
                role_id=member.role_id,
                joined_at=member.joined_at,
                last_active_at=member.last_active_at
            )
            for member in members
        ]

    except Exception as e:
        logger.error(f"Error listing members for organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{org_id}/members/{target_user_id}/role",
    response_model=Dict[str, str],
    summary="Get Member Role",
    description="Get user's role in organization."
)
async def get_member_role(
    org_id: str = Path(..., description="Organization ID"),
    target_user_id: str = Path(..., description="User ID"),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> Dict[str, str]:
    """Get member's role in organization."""
    try:
        role_id = await service.get_member_role(org_id, target_user_id)

        if not role_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found in organization {org_id}"
            )

        return {
            "user_id": target_user_id,
            "org_id": org_id,
            "role_id": role_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting member role for {target_user_id} in org {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ============================================================================
# Permission Checking Endpoints
# ============================================================================

@router.post(
    "/{org_id}/permissions/check",
    response_model=PermissionCheckResponse,
    summary="Check Permission",
    description="Check if user has specific permission in organization."
)
async def check_permission(
    org_id: str = Path(..., description="Organization ID"),
    request: PermissionCheckRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: OrganizationService = Depends(get_organization_service)
) -> PermissionCheckResponse:
    """Check if user has permission in organization."""
    try:
        has_permission = await service.user_has_permission(
            user_id=user_id,
            org_id=org_id,
            permission=request.permission
        )

        return PermissionCheckResponse(
            has_permission=has_permission,
            permission=request.permission,
            user_id=user_id,
            org_id=org_id
        )

    except Exception as e:
        logger.error(f"Error checking permission for user {user_id} in org {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
