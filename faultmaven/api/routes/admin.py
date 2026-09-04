"""Operator user-administration routes (``/api/v1/admin/users*``).

FastAPI routes for the deployment operator's view of user accounts:

- User listing with pagination and filtering
- User detail retrieval with permissions
- User activation/deactivation
- Role assignment and removal

Every route requires ``platform_admin`` — the deployment-wide operator role; the
org-scoped ``Role.ADMIN`` reaches none of them.

**Tenant confinement (#1318).** The operator role is deployment-wide; the
operator's *request* is not. Each route resolves its target through
``api/operator_user_scope`` first, so an operator bound to organization A
administers A's users and no others, and a user of B answers exactly what an
absent id answers. That predicate is the whole of the cross-tenant rule here:
unlike case content there is no grant that reaches further, because the
break-glass grant is case-scoped by construction — see the
``operator_user_scope`` module docstring, and #1318 for the audited break-glass
path (ADR-012 D9 option A) that is deliberately NOT half-built here.

Design Reference: TASK-019 Admin User Management Endpoints
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from starlette.requests import Request

from faultmaven.api.middleware.auth import get_current_user, require_platform_admin
from faultmaven.api.models import (
    AdminUserListItem,
    AdminUserListResponse,
    RoleAssignmentRequest,
    RoleAssignmentResponse,
    UserDetailResponse,
    UserStatusResponse,
)
from faultmaven.api.operator_user_scope import (
    OperatorUserScope,
    get_operator_user_scope,
    user_not_found,
)
from faultmaven.api.v1.dependencies import get_llm_provider
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


async def get_user_service(request: Request):
    """Get UserService instance from app.state (Composition Root)."""
    user_service = getattr(request.app.state, "user_service", None)
    if user_service:
        return user_service
    raise RuntimeError(
        "UserService not available from app.state. "
        "Ensure the container is properly initialized with auth_service."
    )


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - User Management"],
)


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
    is_active: Optional[bool] = Query(
        None, description="Filter by active/inactive status"
    ),
    role: Optional[str] = Query(
        None, description="Filter by role (admin, member, viewer)"
    ),
    search: Optional[str] = Query(
        None, description="Search email or full_name (case-insensitive)"
    ),
    limit: int = Query(50, le=100, ge=1, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> AdminUserListResponse:
    """List the users of the operator's own organization.

    Returns a paginated list with filtering options. Confined to the
    organization the operator's request is bound to (#1318): the page, the
    filters and ``total`` all range over that tenant, so ``total`` is a count of
    it rather than of the deployment. Under single-tenancy the deployment is the
    organization and the listing is unchanged.

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
        403 Forbidden: Caller is not a platform admin, or carries no
            organization to be confined to
        422 Unprocessable Entity: Invalid query parameters
    """
    # Resolved OUTSIDE the try below: a missing membership store is a 503 and a
    # caller with no tenant is a 403, and the blanket handler would turn either
    # into a 500 that reads like a bug in the listing.
    member_ids = await scope.member_ids(current_user)

    try:
        users, total = await user_service.list_users(
            restrict_to_user_ids=member_ids,
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
                # True by construction rather than by assumption (#1318): every
                # row here is a member of the operator's own organization, or
                # the deployment is single-tenant and there is only one. Before
                # the predicate this stamp reported another tenant's user as
                # belonging to the caller's org.
                organization_id=current_user.organization_id,
                email=user.email,
                full_name=user.display_name,
                roles=user.roles if user.roles else ["member"],
                is_active=user.is_active,
                is_verified=(
                    user.is_email_verified
                    if hasattr(user, "is_email_verified")
                    else False
                ),
                # Through to_json_compatible (fm#1129): under SQLite these
                # datetimes come back naive, and a naive datetime in a pydantic
                # field serializes suffix-less while /auth/me emits 'Z' for the
                # same row. to_json_compatible stamps 'Z' (naive = UTC here);
                # pydantic round-trips that string to an aware datetime and
                # re-emits it as 'Z', so the two endpoints agree on the wire.
                last_login_at=to_json_compatible(user.last_login_at),
                created_at=to_json_compatible(user.created_at),
                updated_at=to_json_compatible(
                    user.updated_at if hasattr(user, "updated_at") else user.created_at
                ),
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
) -> UserDetailResponse:
    """Get detailed user information (operator only).

    Returns complete user information including derived permissions. The
    operator can view users in their own organization; a user of another
    organization answers exactly what an absent id answers (#1318), so the
    refusal cannot be used to confirm that the account exists.

    Path Parameters:
        user_id: User ID to retrieve

    Returns:
        UserDetailResponse with full user details

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, or carries no
            organization to be confined to
        404 Not Found: User does not exist, or is not in the operator's
            organization — one answer for both, deliberately
    """
    # Before the fetch, and before the blanket ``except`` below: the target is
    # not this operator's to read, so no row is loaded for it at all.
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
        user_data = await user_service.get_user_with_metadata(user_id=user_id)

        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {user_id}",
            )

        return UserDetailResponse(
            user_id=user_data["user_id"],
            # The organization this read resolved within, not a value invented
            # by the service (#1318). It is the truth here by construction: the
            # predicate above admitted this user as a member of the operator's
            # organization, or the deployment is single-tenant and there is one.
            organization_id=current_user.organization_id,
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
) -> UserStatusResponse:
    """Deactivate a user account in the operator's own organization.

    Sets user is_active=False and revokes all JWT tokens. The operator cannot
    deactivate themselves, and cannot reach another organization's user (#1318):
    that answers what an absent id answers, and nothing is written.

    Path Parameters:
        user_id: User ID to deactivate

    Returns:
        UserStatusResponse confirming deactivation

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, is deactivating self, or
            carries no organization to be confined to
        404 Not Found: User does not exist, or is not in the operator's
            organization — one answer for both, deliberately
        409 Conflict: User already deactivated
    """
    # Prevent self-deactivation
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot deactivate your own account",
        )

    # Before the write. A 200 that changed a row in another tenant is the
    # failure #1318 records, and only ordering this ahead of the service call
    # prevents it.
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
) -> UserStatusResponse:
    """Activate a user account in the operator's own organization.

    Sets user is_active=True. User can log in after activation. Another
    organization's user answers what an absent id answers (#1318) — including
    in place of the 409 below, which would otherwise report that the account
    exists and is already active.

    Path Parameters:
        user_id: User ID to activate

    Returns:
        UserStatusResponse confirming activation

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, or carries no
            organization to be confined to
        404 Not Found: User does not exist, or is not in the operator's
            organization — one answer for both, deliberately
        409 Conflict: User already active
    """
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
) -> RoleAssignmentResponse:
    """Assign an organization-scoped role to a user (operator only).

    Replaces the user's organization-scoped role (`admin`, `member`, `viewer`)
    and leaves roles on other axes untouched — notably `platform_admin`, which
    is granted and revoked only by `fm-promote-platform-admin` /
    `fm-demote-platform-admin`, and the base `user` marker. Revokes all JWT
    tokens. Callers cannot modify their own roles, and cannot re-role a user of
    another organization (#1318): that answers what an absent id answers, and no
    role is written.

    Path Parameters:
        user_id: User ID to assign role to

    Request Body:
        role: Organization-scoped role to assign (admin, member, viewer)

    Returns:
        RoleAssignmentResponse confirming role assignment

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, is modifying own roles,
            or carries no organization to be confined to
        404 Not Found: User does not exist, or is not in the operator's
            organization — one answer for both, deliberately
        409 Conflict: User already has this organization-scoped role
        422 Unprocessable Entity: Invalid role
    """
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
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
            status_code=422,
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
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
) -> RoleAssignmentResponse:
    """Remove an organization-scoped role from a user (operator only).

    Drops the role from the user's organization-scoped axis; if that leaves no
    organization-scoped role, the user lands on `viewer` (minimum privilege).
    Roles on other axes are preserved — removing an org role never revokes
    `platform_admin` (use `fm-demote-platform-admin` for that). Revokes all
    JWT tokens. Callers cannot remove their own roles, and cannot re-role a user
    of another organization (#1318): that answers what an absent id answers, and
    no role is removed.

    Path Parameters:
        user_id: User ID to remove role from
        role: Organization-scoped role to remove (admin, member)

    Returns:
        RoleAssignmentResponse confirming role removal

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, is modifying own roles,
            or carries no organization to be confined to
        404 Not Found: User does not exist, does not hold this role, or is not
            in the operator's organization — one answer for all three
        422 Unprocessable Entity: Invalid role or attempting to remove viewer role
    """
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
        updated_user = await user_service.remove_role(
            user_id=user_id,
            role=role,
            organization_id=current_user.organization_id,
            admin_user_id=current_user.user_id,
        )

        return RoleAssignmentResponse(
            user_id=updated_user.user_id,
            roles=updated_user.roles if updated_user.roles else ["viewer"],
            updated_at=datetime.now(timezone.utc),
            message=(f"Role '{role}' removed. All JWT tokens revoked."),
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
            status_code=422,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to remove role: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove role: {str(e)}",
        )


@router.get("/debug/llm-routing", response_model=dict)
async def get_llm_routing_health(
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    llm_provider=Depends(get_llm_provider),
) -> dict:
    """Get LLM provider health and routing status (admin only).

    Returns detailed health metrics for all LLM providers including:
    - Current health status (HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN)
    - Consecutive failure counts
    - Average latency
    - Sticky routing status
    - Last success/failure timestamps

    Returns:
        Dict with provider health summary and routing configuration

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: User lacks admin role
        503 Service Unavailable: LLM provider not configured
    """
    if not llm_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM provider not configured",
        )

    try:
        # Get provider health summary from registry
        provider_status = llm_provider.get_provider_status()

        # Get fallback chain configuration
        fallback_chain = llm_provider.registry.get_fallback_chain()
        available_providers = llm_provider.registry.get_available_providers()

        return {
            "providers": provider_status,
            "routing": {
                "fallback_chain": fallback_chain,
                "available_providers": available_providers,
                "sticky_provider": llm_provider.registry._sticky_provider,
            },
            "cache": {
                "enabled": True,
                "size": llm_provider.cache.current_size,
                "max_size": llm_provider.cache.max_size,
            },
            "config": {
                "confidence_threshold": llm_provider.confidence_threshold,
                "request_timeout": llm_provider.request_timeout,
            },
        }

    except Exception as e:
        logger.error(f"Failed to get LLM routing health: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get LLM routing health: {str(e)}",
        )
