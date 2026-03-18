"""Organization Management API Routes (TASK-021)

Purpose: REST API endpoints for organization and multi-tenant management

This module provides REST API endpoints for managing organizations,
member management, settings, and role-based access control (RBAC).

Key Endpoints:
- Organization CRUD operations
- Member management (add, remove, update roles)
- Organization settings
- Permission checking
- Multi-tenant isolation

Design Reference: TASK-021 Organization Management API Endpoints
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from faultmaven.api.middleware.auth import get_current_user
from faultmaven.api.services.organization_api_service import APIOrganizationService
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ValidationException,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.models.organization import (
    Organization,
    OrganizationMember,
    OrgPlanTier,
)
from faultmaven.modules.auth.domain.services.organization_service import (
    OrganizationService,
)

# Create router
router = APIRouter(prefix="/organizations", tags=["organizations"])

# Set up logging
logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Request/Response Models
# ============================================================================


class OrganizationCreateRequest(BaseModel):
    """Request to create a new organization"""

    name: str = Field(
        ..., description="Organization name", min_length=1, max_length=100
    )
    slug: str = Field(
        ..., description="URL-friendly identifier", min_length=3, max_length=50
    )
    description: Optional[str] = Field(
        None, description="Organization description", max_length=500
    )
    plan_tier: Optional[str] = Field("free", description="Subscription plan tier")

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                "Slug must contain only lowercase letters, numbers, and hyphens"
            )
        return v

    @field_validator("plan_tier")
    @classmethod
    def validate_plan_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "free"  # Use default when null is passed
        valid_tiers = ["free", "pro", "enterprise"]
        if v.lower() not in valid_tiers:
            raise ValueError(f'Plan tier must be one of: {", ".join(valid_tiers)}')
        return v.lower()


class OrganizationUpdateRequest(BaseModel):
    """Request to update organization details"""

    name: Optional[str] = Field(
        None, description="Updated organization name", max_length=100
    )
    description: Optional[str] = Field(
        None, description="Updated description", max_length=500
    )


class OrganizationResponse(BaseModel):
    """Organization details response"""

    organization_id: str
    name: str
    slug: str
    description: Optional[str]
    plan_tier: str
    max_members: int
    current_member_count: int = 0
    owner_user_id: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OrganizationListItem(BaseModel):
    """Organization list item with user role"""

    organization_id: str
    name: str
    slug: str
    plan_tier: str
    role: str
    member_since: datetime


class OrganizationListResponse(BaseModel):
    """Response for listing user's organizations"""

    organizations: List[OrganizationListItem]
    total: int
    limit: int
    offset: int


class MemberAddRequest(BaseModel):
    """Request to add member to organization"""

    email: str = Field(..., description="Email of user to invite")
    role: Optional[str] = Field("member", description="Role to assign (member, admin)")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> str:
        if v is None:
            return "member"  # Use default when null is passed
        valid_roles = ["member", "admin"]
        if v.lower() not in valid_roles:
            raise ValueError(f'Role must be one of: {", ".join(valid_roles)}')
        return v.lower()


class MemberRoleUpdateRequest(BaseModel):
    """Request to update member role"""

    role: str = Field(..., description="New role to assign (member, admin)")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        valid_roles = ["member", "admin"]
        if v.lower() not in valid_roles:
            raise ValueError(f'Role must be one of: {", ".join(valid_roles)}')
        return v.lower()


class MemberResponse(BaseModel):
    """Organization member response"""

    user_id: str
    email: str
    full_name: str
    role: str
    joined_at: datetime

    class Config:
        from_attributes = True


class MemberListResponse(BaseModel):
    """Response for listing organization members"""

    members: List[MemberResponse]
    total: int
    limit: int
    offset: int


class MemberAddResponse(BaseModel):
    """Response for adding a member"""

    user_id: str
    email: str
    full_name: str
    role: str
    joined_at: datetime
    invitation_sent: bool


class MemberRoleUpdateResponse(BaseModel):
    """Response for updating member role"""

    user_id: str
    email: str
    full_name: str
    role: str
    joined_at: datetime
    updated_at: datetime


class SettingsResponse(BaseModel):
    """Organization settings response"""

    organization_id: str
    plan_tier: str
    max_members: int
    current_member_count: int = 0
    max_cases_per_month: Optional[int] = None
    max_storage_gb: int
    features: Dict[str, bool]
    settings: Dict[str, Any]


class SettingsUpdateRequest(BaseModel):
    """Request to update organization settings"""

    allow_public_cases: Optional[bool] = None
    require_2fa: Optional[bool] = None
    session_timeout_minutes: Optional[int] = Field(None, ge=15, le=480)
    default_case_priority: Optional[str] = None

    @field_validator("default_case_priority")
    @classmethod
    def validate_priority(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            valid_priorities = ["low", "medium", "high", "critical"]
            if v.lower() not in valid_priorities:
                raise ValueError(
                    f'Priority must be one of: {", ".join(valid_priorities)}'
                )
            return v.lower()
        return v


class SettingsUpdateResponse(BaseModel):
    """Response for updating organization settings"""

    organization_id: str
    settings: Dict[str, Any]
    updated_at: datetime


class PermissionCheckRequest(BaseModel):
    """Request to check user permission"""

    permission: str = Field(
        ..., description="Permission to check (e.g., 'cases.write')"
    )


class PermissionCheckResponse(BaseModel):
    """Permission check result"""

    has_permission: bool
    permission: str
    user_id: str
    organization_id: str


class DeleteResponse(BaseModel):
    """Generic delete response"""

    message: str
    organization_id: Optional[str] = None
    user_id: Optional[str] = None


# ============================================================================
# Dependencies
# ============================================================================


async def get_api_organization_service(request: Request) -> APIOrganizationService:
    """Get APIOrganizationService instance from app.state (Composition Root)"""
    org_service = getattr(request.app.state, "organization_service", None)
    if org_service is None:
        raise HTTPException(
            status_code=503, detail="Organization service not available"
        )

    # Get optional services from app.state
    user_service = getattr(request.app.state, "user_service", None)
    auth_service = getattr(request.app.state, "auth_service", None)

    return APIOrganizationService(
        organization_service=org_service,
        user_service=user_service,
        auth_service=auth_service,
    )


async def get_organization_service(request: Request) -> OrganizationService:
    """Get OrganizationService instance from app.state (Composition Root)"""
    service = getattr(request.app.state, "organization_service", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="Organization service not available"
        )
    return service


# ============================================================================
# Organization CRUD Endpoints
# ============================================================================


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Organization",
    description="Create a new organization. The creator becomes the organization owner.",
)
async def create_organization(
    request: OrganizationCreateRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> OrganizationResponse:
    """Create a new organization with the authenticated user as owner."""
    try:
        org = await service.create_organization(
            name=request.name,
            slug=request.slug,
            creator_user_id=current_user.user_id,
            description=request.description,
            plan_tier=request.plan_tier,
        )

        logger.info(
            f"Organization created: {org.organization_id} by user {current_user.user_id}"
        )

        # Get member count
        members = await service.organization_service.list_organization_members(
            org.organization_id
        )

        return OrganizationResponse(
            organization_id=org.organization_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=(
                org.plan_tier.value
                if isinstance(org.plan_tier, OrgPlanTier)
                else org.plan_tier
            ),
            max_members=org.max_members,
            current_member_count=len(members),
            owner_user_id=current_user.user_id,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at,
        )

    except ValidationException as e:
        if "already exists" in str(e).lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ServiceException as e:
        logger.error(f"Service error creating organization: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.get(
    "",
    response_model=OrganizationListResponse,
    summary="List User Organizations",
    description="List all organizations the authenticated user belongs to.",
)
async def list_user_organizations(
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> OrganizationListResponse:
    """List all organizations the user belongs to."""
    try:
        orgs, total = await service.list_user_organizations(
            user_id=current_user.user_id, limit=limit, offset=offset
        )

        return OrganizationListResponse(
            organizations=[
                OrganizationListItem(
                    organization_id=org["organization_id"],
                    name=org["name"],
                    slug=org["slug"],
                    plan_tier=org["plan_tier"],
                    role=org["role"],
                    member_since=org["member_since"],
                )
                for org in orgs
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    except Exception as e:
        logger.error(
            f"Error listing organizations for user {current_user.user_id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Get Organization",
    description="Get organization details by ID. Requires organization membership.",
)
async def get_organization(
    organization_id: str = Path(..., description="Organization ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> OrganizationResponse:
    """Get organization details."""
    try:
        org = await service.get_organization(organization_id, current_user.user_id)

        # Get member count and owner
        members = await service.organization_service.list_organization_members(
            organization_id
        )
        owner = next((m for m in members if m.role_id == "role_org_owner"), None)

        return OrganizationResponse(
            organization_id=org.organization_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=(
                org.plan_tier.value
                if isinstance(org.plan_tier, OrgPlanTier)
                else org.plan_tier
            ),
            max_members=org.max_members,
            current_member_count=len(members),
            owner_user_id=owner.user_id if owner else None,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at,
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.get(
    "/by-slug/{slug}",
    response_model=OrganizationResponse,
    summary="Get Organization by Slug",
    description="Get organization details by slug. Requires organization membership.",
)
async def get_organization_by_slug(
    slug: str = Path(..., description="Organization slug"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: OrganizationService = Depends(get_organization_service),
    api_service: APIOrganizationService = Depends(get_api_organization_service),
) -> OrganizationResponse:
    """Get organization details by slug."""
    try:
        org = await org_service.get_organization_by_slug(slug)

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization with slug '{slug}' not found",
            )

        # Check membership using API service
        try:
            await api_service.get_organization(
                org.organization_id, current_user.user_id
            )
        except AuthorizationError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization membership required",
            )

        # Get member count and owner
        members = await org_service.list_organization_members(org.organization_id)
        owner = next((m for m in members if m.role_id == "role_org_owner"), None)

        return OrganizationResponse(
            organization_id=org.organization_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=(
                org.plan_tier.value
                if isinstance(org.plan_tier, OrgPlanTier)
                else org.plan_tier
            ),
            max_members=org.max_members,
            current_member_count=len(members),
            owner_user_id=owner.user_id if owner else None,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting organization by slug {slug}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Update Organization",
    description="Update organization details. Requires owner permission.",
)
async def update_organization(
    organization_id: str = Path(..., description="Organization ID"),
    request: OrganizationUpdateRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> OrganizationResponse:
    """Update organization details."""
    try:
        org = await service.update_organization(
            organization_id=organization_id,
            user_id=current_user.user_id,
            name=request.name,
            description=request.description,
        )

        # Get member count and owner
        members = await service.organization_service.list_organization_members(
            organization_id
        )
        owner = next((m for m in members if m.role_id == "role_org_owner"), None)

        logger.info(
            f"Organization updated: {organization_id} by user {current_user.user_id}"
        )

        return OrganizationResponse(
            organization_id=org.organization_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            plan_tier=(
                org.plan_tier.value
                if isinstance(org.plan_tier, OrgPlanTier)
                else org.plan_tier
            ),
            max_members=org.max_members,
            current_member_count=len(members),
            owner_user_id=owner.user_id if owner else None,
            settings=org.settings or {},
            created_at=org.created_at,
            updated_at=org.updated_at,
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.delete(
    "/{organization_id}",
    response_model=DeleteResponse,
    summary="Delete Organization",
    description="Soft delete an organization. Requires owner permission.",
)
async def delete_organization(
    organization_id: str = Path(..., description="Organization ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> DeleteResponse:
    """Soft delete an organization."""
    try:
        success = await service.delete_organization(
            organization_id, current_user.user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization {organization_id} not found",
            )

        logger.info(
            f"Organization deleted: {organization_id} by user {current_user.user_id}"
        )

        return DeleteResponse(
            message="Organization deleted successfully", organization_id=organization_id
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


# ============================================================================
# Member Management Endpoints
# ============================================================================


@router.get(
    "/{organization_id}/members",
    response_model=MemberListResponse,
    summary="List Organization Members",
    description="List all members of an organization. Requires organization membership.",
)
async def list_organization_members(
    organization_id: str = Path(..., description="Organization ID"),
    role: Optional[str] = Query(
        None, description="Filter by role: owner, admin, member"
    ),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> MemberListResponse:
    """List all members of an organization."""
    try:
        members, total = await service.list_organization_members(
            organization_id=organization_id,
            user_id=current_user.user_id,
            role_filter=role,
            limit=limit,
            offset=offset,
        )

        return MemberListResponse(
            members=[
                MemberResponse(
                    user_id=m["user_id"],
                    email=m["email"],
                    full_name=m["full_name"],
                    role=m["role"],
                    joined_at=m["joined_at"],
                )
                for m in members
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing members for organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.post(
    "/{organization_id}/members",
    response_model=MemberAddResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add Member",
    description="Add user to organization by email. Requires owner or admin permission.",
)
async def add_member(
    organization_id: str = Path(..., description="Organization ID"),
    request: MemberAddRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> MemberAddResponse:
    """Add member to organization."""
    try:
        result = await service.add_member(
            organization_id=organization_id,
            requesting_user_id=current_user.user_id,
            email=request.email,
            role=request.role,
        )

        logger.info(
            f"Member added to organization: {request.email} to org {organization_id}"
        )

        return MemberAddResponse(
            user_id=result["user_id"],
            email=result["email"],
            full_name=result["full_name"],
            role=result["role"],
            joined_at=result["joined_at"],
            invitation_sent=result.get("invitation_sent", True),
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ConflictError as e:
        if "max_members" in str(e).lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error adding member to organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.delete(
    "/{organization_id}/members/{user_id}",
    response_model=DeleteResponse,
    summary="Remove Member",
    description="Remove user from organization. Owner can remove anyone except self, admin can remove members only.",
)
async def remove_member(
    organization_id: str = Path(..., description="Organization ID"),
    user_id: str = Path(..., description="User ID to remove"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> DeleteResponse:
    """Remove member from organization."""
    try:
        success = await service.remove_member(
            organization_id=organization_id,
            requesting_user_id=current_user.user_id,
            target_user_id=user_id,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Member {user_id} not found in organization {organization_id}",
            )

        logger.info(
            f"Member removed from organization: user {user_id} from org {organization_id}"
        )

        return DeleteResponse(message="Member removed successfully", user_id=user_id)

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.error(f"Error removing member from organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.patch(
    "/{organization_id}/members/{user_id}",
    response_model=MemberRoleUpdateResponse,
    summary="Update Member Role",
    description="Update user's role in organization. Requires owner permission.",
)
async def update_member_role(
    organization_id: str = Path(..., description="Organization ID"),
    user_id: str = Path(..., description="User ID"),
    request: MemberRoleUpdateRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> MemberRoleUpdateResponse:
    """Update member's role in organization."""
    try:
        result = await service.update_member_role(
            organization_id=organization_id,
            requesting_user_id=current_user.user_id,
            target_user_id=user_id,
            role=request.role,
        )

        logger.info(
            f"Member role updated: user {user_id} in org {organization_id} to {request.role}"
        )

        return MemberRoleUpdateResponse(
            user_id=result["user_id"],
            email=result["email"],
            full_name=result["full_name"],
            role=result["role"],
            joined_at=result["joined_at"],
            updated_at=result["updated_at"],
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    except Exception as e:
        logger.error(
            f"Error updating member role in organization {organization_id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


# ============================================================================
# Organization Settings Endpoints
# ============================================================================


@router.get(
    "/{organization_id}/settings",
    response_model=SettingsResponse,
    summary="Get Organization Settings",
    description="Get organization settings and plan limits. Requires organization membership.",
)
async def get_organization_settings(
    organization_id: str = Path(..., description="Organization ID"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> SettingsResponse:
    """Get organization settings."""
    try:
        settings = await service.get_organization_settings(
            organization_id, current_user.user_id
        )

        return SettingsResponse(
            organization_id=settings["organization_id"],
            plan_tier=settings["plan_tier"],
            max_members=settings["max_members"],
            current_member_count=settings["current_member_count"],
            max_cases_per_month=settings["max_cases_per_month"],
            max_storage_gb=settings["max_storage_gb"],
            features=settings["features"],
            settings=settings["settings"],
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting settings for organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.patch(
    "/{organization_id}/settings",
    response_model=SettingsUpdateResponse,
    summary="Update Organization Settings",
    description="Update organization settings. Requires owner permission.",
)
async def update_organization_settings(
    organization_id: str = Path(..., description="Organization ID"),
    request: SettingsUpdateRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: APIOrganizationService = Depends(get_api_organization_service),
) -> SettingsUpdateResponse:
    """Update organization settings."""
    try:
        # Convert request to dict, excluding None values
        settings_update = {
            k: v for k, v in request.model_dump().items() if v is not None
        }

        result = await service.update_organization_settings(
            organization_id=organization_id,
            user_id=current_user.user_id,
            settings=settings_update,
        )

        logger.info(
            f"Organization settings updated: {organization_id} by user {current_user.user_id}"
        )

        return SettingsUpdateResponse(
            organization_id=result["organization_id"],
            settings=result["settings"],
            updated_at=result["updated_at"],
        )

    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AuthorizationError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error updating settings for organization {organization_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


# ============================================================================
# Permission Checking Endpoints
# ============================================================================


@router.post(
    "/{organization_id}/permissions/check",
    response_model=PermissionCheckResponse,
    summary="Check Permission",
    description="Check if user has specific permission in organization.",
)
async def check_permission(
    organization_id: str = Path(..., description="Organization ID"),
    request: PermissionCheckRequest = Body(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: OrganizationService = Depends(get_organization_service),
) -> PermissionCheckResponse:
    """Check if user has permission in organization."""
    try:
        has_permission = await service.user_has_permission(
            user_id=current_user.user_id,
            organization_id=organization_id,
            permission=request.permission,
        )

        return PermissionCheckResponse(
            has_permission=has_permission,
            permission=request.permission,
            user_id=current_user.user_id,
            organization_id=organization_id,
        )

    except Exception as e:
        logger.error(
            f"Error checking permission for user {current_user.user_id} in org {organization_id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
