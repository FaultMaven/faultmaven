"""User Management API Routes (TASK-018)

Purpose: FastAPI routes for user profile and management operations.

Endpoints:
- GET    /api/v1/users/me           - Get current user's profile
- PATCH  /api/v1/users/me           - Update current user's profile
- GET    /api/v1/users/{user_id}    - Get user by ID (admin only)
- GET    /api/v1/users              - List users (admin only)
- DELETE /api/v1/users/{user_id}    - Deactivate user (admin only)
- POST   /api/v1/users/{user_id}/activate - Reactivate user (admin only)

Design Reference: TASK-018 User Management Service
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, EmailStr, Field

from faultmaven.api.middleware.auth import (
    get_current_user,
    require_admin,
)
from faultmaven.api.routes.auth import (
    UserResponse,
    get_user_service,
    _user_to_response,
)
from faultmaven.exceptions import ConflictError, NotFoundError, ValidationException
from faultmaven.models.auth import AuthenticatedUser

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/users", tags=["Users"])


# ============================================================
# Request/Response Models
# ============================================================


class UserProfileUpdateRequest(BaseModel):
    """User profile update request."""

    email: Optional[EmailStr] = Field(None, description="New email address")
    full_name: Optional[str] = Field(
        None, min_length=1, max_length=200, description="New full name"
    )


class UserListResponse(BaseModel):
    """Paginated user list response."""

    users: List[UserResponse] = Field(..., description="List of users")
    total: int = Field(..., description="Total number of users")
    limit: int = Field(..., description="Page size")
    offset: int = Field(..., description="Page offset")


# ============================================================
# Current User Endpoints
# ============================================================


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> UserResponse:
    """Get current user's profile.

    Returns the profile of the currently authenticated user.

    Headers:
        Authorization: Bearer <access_token>

    Returns:
        UserResponse with user profile details

    Example:
        GET /api/v1/users/me
        Headers: Authorization: Bearer eyJhbGc...

        Response:
        {
            "user_id": "uuid...",
            "email": "user@example.com",
            "full_name": "John Doe",
            "is_active": true,
            "is_verified": false,
            ...
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.get_user(current_user.user_id)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        return _user_to_response(user)

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error getting user profile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user profile",
        )


@router.patch("/me", response_model=UserResponse)
async def update_current_user_profile(
    request: UserProfileUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> UserResponse:
    """Update current user's profile.

    Updates the profile of the currently authenticated user.
    If email is changed, is_verified is set to false.

    Headers:
        Authorization: Bearer <access_token>

    Request Body:
        email: New email address (optional)
        full_name: New full name (optional)

    Returns:
        UserResponse with updated user profile

    Raises:
        409: Email already in use
        422: Invalid email format

    Example:
        PATCH /api/v1/users/me
        Headers: Authorization: Bearer eyJhbGc...
        {
            "full_name": "Jane Doe"
        }

        Response:
        {
            "user_id": "uuid...",
            "email": "user@example.com",
            "full_name": "Jane Doe",
            ...
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.update_user_profile(
            user_id=current_user.user_id,
            email=request.email,
            full_name=request.full_name,
        )

        logger.info(f"Profile updated for user: {user.user_id}")
        return _user_to_response(user)

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )


# ============================================================
# Admin User Management Endpoints
# ============================================================


@router.get("", response_model=UserListResponse)
async def list_users(
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserListResponse:
    """List users with pagination (admin only).

    Returns a paginated list of users. Admin access required.

    Headers:
        Authorization: Bearer <access_token> (admin)

    Query Parameters:
        limit: Maximum results (1-100, default 50)
        offset: Pagination offset (default 0)
        is_active: Filter by active status (optional)

    Returns:
        UserListResponse with paginated users

    Raises:
        403: Not admin

    Example:
        GET /api/v1/users?limit=10&offset=0&is_active=true
        Headers: Authorization: Bearer eyJhbGc... (admin token)

        Response:
        {
            "users": [...],
            "total": 42,
            "limit": 10,
            "offset": 0
        }
    """
    try:
        user_service = get_user_service()
        users, total = await user_service.list_users(
            limit=limit,
            offset=offset,
            is_active=is_active,
        )

        return UserListResponse(
            users=[_user_to_response(u) for u in users],
            total=total,
            limit=limit,
            offset=offset,
        )

    except Exception as e:
        logger.error(f"Error listing users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str = Path(..., description="User ID"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserResponse:
    """Get user by ID (admin only).

    Returns a specific user's profile. Admin access required.

    Headers:
        Authorization: Bearer <access_token> (admin)

    Path Parameters:
        user_id: User ID to retrieve

    Returns:
        UserResponse with user profile

    Raises:
        403: Not admin
        404: User not found

    Example:
        GET /api/v1/users/uuid-123
        Headers: Authorization: Bearer eyJhbGc... (admin token)

        Response:
        {
            "user_id": "uuid-123",
            "email": "user@example.com",
            "full_name": "John Doe",
            ...
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.get_user(user_id)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        return _user_to_response(user)

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error getting user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user",
        )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: str = Path(..., description="User ID to deactivate"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> None:
    """Deactivate user account (admin only).

    Soft-deletes a user account. The user will not be able to log in
    and all their tokens are revoked. Admin access required.

    Headers:
        Authorization: Bearer <access_token> (admin)

    Path Parameters:
        user_id: User ID to deactivate

    Returns:
        204 No Content on success

    Raises:
        403: Not admin, or trying to deactivate self
        404: User not found

    Example:
        DELETE /api/v1/users/uuid-123
        Headers: Authorization: Bearer eyJhbGc... (admin token)
    """
    # Prevent self-deactivation
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot deactivate your own account",
        )

    try:
        user_service = get_user_service()
        await user_service.deactivate_user_admin(
            user_id=user_id,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )
        logger.info(f"User deactivated by admin {current_user.user_id}: {user_id}")

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    except Exception as e:
        logger.error(f"Error deactivating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to deactivate user",
        )


@router.post("/{user_id}/activate", response_model=UserResponse)
async def activate_user(
    user_id: str = Path(..., description="User ID to activate"),
    current_user: AuthenticatedUser = Depends(require_admin),
) -> UserResponse:
    """Reactivate user account (admin only).

    Reactivates a previously deactivated user account.
    Admin access required.

    Headers:
        Authorization: Bearer <access_token> (admin)

    Path Parameters:
        user_id: User ID to activate

    Returns:
        UserResponse with activated user profile

    Raises:
        403: Not admin
        404: User not found

    Example:
        POST /api/v1/users/uuid-123/activate
        Headers: Authorization: Bearer eyJhbGc... (admin token)

        Response:
        {
            "user_id": "uuid-123",
            "is_active": true,
            ...
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.activate_user_admin(
            user_id=user_id,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        logger.info(f"User activated by admin {current_user.user_id}: {user_id}")
        return _user_to_response(user)

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    except Exception as e:
        logger.error(f"Error activating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to activate user",
        )
