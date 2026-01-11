"""Admin User Management Routes (TASK-019)

Purpose: FastAPI routes for admin-only user management operations.

This module provides admin endpoints for:
- User listing with pagination and filtering
- User detail retrieval with permissions
- User activation/deactivation
- Role assignment and removal

All endpoints require admin role authentication.

Design Reference: TASK-019 Admin User Management Endpoints
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status

from faultmaven.api.middleware.auth import get_current_user, require_admin
from faultmaven.api.models import (
    AdminUserListItem,
    AdminUserListResponse,
    RoleAssignmentRequest,
    RoleAssignmentResponse,
    UserDetailResponse,
    UserStatusResponse,
)
from faultmaven.api.routes.auth import get_user_service
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.models.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - User Management"],
)


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    current_user: AuthenticatedUser = Depends(require_admin),
    is_active: Optional[bool] = Query(None, description="Filter by active/inactive status"),
    role: Optional[str] = Query(None, description="Filter by role (admin, member, viewer)"),
    search: Optional[str] = Query(None, description="Search email or full_name (case-insensitive)"),
    limit: int = Query(50, le=100, ge=1, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> AdminUserListResponse:
    """List all users in organization (admin only).

    Returns paginated list of users with filtering options.
    Admin can only see users in their own organization.

    Query Parameters:
        is_active: Filter by active/inactive status
        role: Filter by role (admin, member, viewer)
        search: Search email or full_name (case-insensitive, partial match)
        limit: Max results per page (default 50, max 100)
        offset: Pagination offset

    Returns:
        AdminUserListResponse with users, total, limit, offset

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role
        422 Unprocessable Entity: Invalid query parameters
    """
    try:
        user_service = get_user_service()

        users, total = await user_service.list_users(
            is_active=is_active,
            role=role,
            search=search,
            limit=limit,
            offset=offset,
        )

        # Convert to response model
        user_items = [
            AdminUserListItem(
                user_id=user.user_id,
                organization_id=current_user.organization_id,
                email=user.email,
                full_name=user.display_name,
                roles=user.roles if user.roles else ['member'],
                is_active=user.is_active,
                is_verified=user.is_email_verified if hasattr(user, 'is_email_verified') else False,
                last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
                created_at=user.created_at,
                updated_at=user.updated_at if hasattr(user, 'updated_at') else user.created_at,
            )
            for user in users
        ]

        return AdminUserListResponse(
            users=user_items,
            total=total,
            limit=limit,
            offset=offset,
        )

    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list users: {str(e)}",
        )


@router.get("/users/{user_id}", response_model=UserDetailResponse)
async def get_user_details(
    user_id: str = Path(..., description="User ID to retrieve"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserDetailResponse:
    """Get detailed user information (admin only).

    Returns complete user information including derived permissions.
    Admin can only view users in their own organization.

    Path Parameters:
        user_id: User ID to retrieve

    Returns:
        UserDetailResponse with full user details

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role OR user belongs to different organization
        404 Not Found: User does not exist
    """
    try:
        user_service = get_user_service()

        user_data = await user_service.get_user_with_metadata(user_id=user_id)

        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {user_id}",
            )

        return UserDetailResponse(
            user_id=user_data["user_id"],
            organization_id=user_data["organization_id"],
            email=user_data["email"],
            full_name=user_data["full_name"],
            roles=user_data["roles"],
            permissions=user_data["permissions"],
            is_active=user_data["is_active"],
            is_verified=user_data["is_verified"],
            last_login_at=user_data["last_login_at"],
            created_at=user_data["created_at"],
            updated_at=user_data["updated_at"],
            metadata=user_data["metadata"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get user details: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user details: {str(e)}",
        )


@router.post("/users/{user_id}/deactivate", response_model=UserStatusResponse)
async def deactivate_user(
    user_id: str = Path(..., description="User ID to deactivate"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserStatusResponse:
    """Deactivate user account (admin only).

    Sets user is_active=False and revokes all JWT tokens.
    Admin cannot deactivate themselves.

    Path Parameters:
        user_id: User ID to deactivate

    Returns:
        UserStatusResponse confirming deactivation

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role OR trying to deactivate self
        404 Not Found: User does not exist
        409 Conflict: User already deactivated
    """
    # Prevent self-deactivation
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot deactivate your own account",
        )

    try:
        user_service = get_user_service()

        updated_user = await user_service.deactivate_user_admin(
            user_id=user_id,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        return UserStatusResponse(
            user_id=updated_user.user_id,
            is_active=updated_user.is_active,
            updated_at=datetime.now(timezone.utc),
            message="User deactivated successfully. All JWT tokens revoked.",
        )

    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to deactivate user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate user: {str(e)}",
        )


@router.post("/users/{user_id}/activate", response_model=UserStatusResponse)
async def activate_user(
    user_id: str = Path(..., description="User ID to activate"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserStatusResponse:
    """Activate user account (admin only).

    Sets user is_active=True. User can log in after activation.

    Path Parameters:
        user_id: User ID to activate

    Returns:
        UserStatusResponse confirming activation

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role
        404 Not Found: User does not exist
        409 Conflict: User already active
    """
    try:
        user_service = get_user_service()

        updated_user = await user_service.activate_user_admin(
            user_id=user_id,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        return UserStatusResponse(
            user_id=updated_user.user_id,
            is_active=updated_user.is_active,
            updated_at=datetime.now(timezone.utc),
            message="User activated successfully.",
        )

    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to activate user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to activate user: {str(e)}",
        )


@router.post("/users/{user_id}/roles", response_model=RoleAssignmentResponse)
async def assign_role(
    user_id: str = Path(..., description="User ID to assign role to"),
    request: RoleAssignmentRequest = Body(...),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> RoleAssignmentResponse:
    """Assign role to user (admin only).

    Replaces existing roles with the new role. Revokes all JWT tokens.
    Admin cannot modify their own roles.

    Path Parameters:
        user_id: User ID to assign role to

    Request Body:
        role: Role to assign (admin, member, viewer)

    Returns:
        RoleAssignmentResponse confirming role assignment

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role OR trying to modify own roles
        404 Not Found: User does not exist
        409 Conflict: User already has this role
        422 Unprocessable Entity: Invalid role
    """
    try:
        user_service = get_user_service()

        updated_user = await user_service.assign_role(
            user_id=user_id,
            role=request.role,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        return RoleAssignmentResponse(
            user_id=updated_user.user_id,
            roles=updated_user.roles if updated_user.roles else [request.role],
            updated_at=datetime.now(timezone.utc),
            message=f"Role '{request.role}' assigned successfully. All JWT tokens revoked.",
        )

    except AuthorizationError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to assign role: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to assign role: {str(e)}",
        )


@router.delete("/users/{user_id}/roles/{role}", response_model=RoleAssignmentResponse)
async def remove_role(
    user_id: str = Path(..., description="User ID to remove role from"),
    role: str = Path(..., description="Role to remove (admin, member)"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> RoleAssignmentResponse:
    """Remove role from user (admin only).

    Downgrades user to viewer (minimum privilege). Revokes all JWT tokens.
    Admin cannot remove their own admin role.

    Path Parameters:
        user_id: User ID to remove role from
        role: Role to remove (admin, member)

    Returns:
        RoleAssignmentResponse confirming role removal

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role OR trying to remove own admin role
        404 Not Found: User does not exist OR user doesn't have this role
        422 Unprocessable Entity: Invalid role or attempting to remove viewer role
    """
    try:
        user_service = get_user_service()

        updated_user = await user_service.remove_role(
            user_id=user_id,
            role=role,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        return RoleAssignmentResponse(
            user_id=updated_user.user_id,
            roles=updated_user.roles if updated_user.roles else ['viewer'],
            updated_at=datetime.now(timezone.utc),
            message=f"Role '{role}' removed. User downgraded to 'viewer'. All JWT tokens revoked.",
        )

    except AuthorizationError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to remove role: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove role: {str(e)}",
        )
