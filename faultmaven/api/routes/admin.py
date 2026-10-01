"""Operator user-administration routes (``/api/v1/admin/users*``).

FastAPI routes for the deployment operator's view of user accounts:

- User listing with pagination and filtering
- User detail retrieval with permissions
- User activation/deactivation
- Role assignment and removal

Every route requires ``platform_admin`` — the deployment-wide operator role; the
org-scoped ``Role.ADMIN`` reaches none of them.

**Administration is confined (#1318); the listing is not.** The operator role
is deployment-wide; the operator's *request* is not. Every route that reads one
account or changes one resolves its target through ``api/operator_user_scope``
first, so an operator bound to enterprise A administers A's accounts and no
others, and an account of B answers exactly what an absent id answers. That
predicate is the whole of the cross-tenant rule for administration: unlike case
content there is no grant that reaches further, because the break-glass grant is
case-scoped by construction — see the ``operator_user_scope`` module docstring,
and #1318 for the audited break-glass path (ADR-012 D9 option A) that is
deliberately NOT half-built here.

The LISTING spans every enterprise under ``TENANT_PROVIDER=multi``, email and
display name included. Account records — who holds an account, in which
enterprise, of which kind, whether it is active — are the service's own
operational data about its users; case content, by contrast, is held on a
customer's behalf and stays behind break-glass. ``users`` is outside row-level
security, so the rows come from an ordinary read of the account store
(``IAccountDirectory.list_account_metadata``) that selects eleven columns and no
credential, SSO subject or role, and that this route alone calls; the access is
recorded in the operator trail before it is served; and each row says whether
the operator can administer it (``manageable``). Roles are the organization's
management vocabulary, so they are reported only for the manageable rows, read
in one query confined to the operator's enterprise.

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
from faultmaven.api.operator_audit import (
    get_operator_audit_repository,
    record_operator_access,
)
from faultmaven.api.operator_grants import resolved_deployment_mode
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
from faultmaven.models.api_models import MAX_IDENTIFIER_LENGTH
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.modules.auth.contracts import (
    AccountMetadata,
    IAccountDirectory,
    effective_roles,
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


async def get_account_directory(request: Request) -> Optional[IAccountDirectory]:
    """The account store's directory reads, or ``None`` when it is unwired.

    The account store's repository (``app.state.user_store``), which the
    operator user scope consults too. Under ``TENANT_PROVIDER=multi`` the list
    refuses without it rather than answering from anywhere else.
    """
    store = getattr(request.app.state, "user_store", None)
    return getattr(store, "user_repository", None)


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - User Management"],
)


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_platform_admin),
    user_service=Depends(get_user_service),
    scope: OperatorUserScope = Depends(get_operator_user_scope),
    directory: Optional[IAccountDirectory] = Depends(get_account_directory),
    is_active: Optional[bool] = Query(
        None, description="Filter by active/inactive status"
    ),
    role: Optional[str] = Query(
        None,
        description=(
            "Filter by role (admin, member, viewer). Not available on the "
            "cross-enterprise list (TENANT_PROVIDER=multi), which reports roles "
            "only for the operator's own enterprise: refused there with 422"
        ),
    ),
    search: Optional[str] = Query(
        None, description="Search email or full_name (case-insensitive)"
    ),
    enterprise_id: Optional[str] = Query(
        None, description="Only accounts anchored to this enterprise"
    ),
    limit: int = Query(50, le=100, ge=1, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> AdminUserListResponse:
    """List user accounts for the operator.

    Under ``TENANT_PROVIDER=multi`` the list spans every enterprise: account
    records — who holds an account, in which enterprise, whether it is active —
    are the service's own operational data about its users. Each row says
    whether the operator can administer it (``manageable``): only accounts in
    the operator's own enterprise are, and only those rows carry their
    ``roles``; elsewhere ``roles`` is ``[]`` (not reported). The read is
    recorded in the operator access trail before anything is served. Under
    single-tenancy the deployment is one enterprise and every row is
    manageable.

    Query Parameters:
        is_active: Filter by active/inactive status
        role: Filter by role (admin, member, viewer); single-tenant only
        search: Search email or full_name (case-insensitive, partial match);
            an empty search filters nothing
        enterprise_id: Only accounts anchored to this enterprise
        limit: Max results per page (default 50, max 100)
        offset: Pagination offset

    Returns:
        AdminUserListResponse with users, total, limit, offset. ``total`` counts
        every match the list ranges over — every enterprise under multi.

    Raises:
        401 Unauthorized: No valid JWT token
        403 Forbidden: Caller is not a platform admin, or carries no
            enterprise to act within
        422 Unprocessable Entity: Invalid query parameters, or a ``role``
            filter on the cross-enterprise list
        500 Internal Server Error: The read failed after the access was
            recorded
        503 Service Unavailable: Under multi, the access could not be
            recorded, or the account store is not composed — refused before
            anything is read
    """
    # Resolved OUTSIDE the try below: a caller with no tenant is a 403, and the
    # blanket handler would turn it into a 500 that reads like a bug in the
    # listing. Under multi this is the enterprise whose accounts the operator
    # administers; under single it is None, the whole deployment.
    own_enterprise = scope.listing_enterprise(current_user)
    if enterprise_id is not None:
        _refuse_unusable_enterprise_filter(enterprise_id)
    # An empty search filters nothing; it is no search, for the read and for
    # the access record alike.
    search = search or None

    if scope.confined:
        return await _list_across_enterprises(
            request,
            directory=directory,
            operator=current_user,
            own_enterprise=own_enterprise,
            is_active=is_active,
            role=role,
            search=search,
            enterprise_id=enterprise_id,
            limit=limit,
            offset=offset,
        )

    try:
        users, total = await user_service.list_users(
            enterprise_id=enterprise_id,
            is_active=is_active,
            role=role,
            search=search,
            limit=limit,
            offset=offset,
        )

        return AdminUserListResponse(
            users=[
                # The stamp is true by construction rather than by assumption
                # (#1318): the deployment is single-tenant and every account is
                # anchored to its one enterprise.
                _manageable_item(user, enterprise_id=current_user.enterprise_id)
                for user in users
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        )


_ROLE_FILTER_REFUSED_DETAIL = (
    "The role filter is not available on the cross-enterprise account list: "
    "roles are reported only for accounts in the operator's own enterprise"
)
_NO_ACCOUNT_STORE_DETAIL = "Account store not available; the account list was refused"


def _refuse_unusable_enterprise_filter(enterprise_id: str) -> None:
    """422 for an ``enterprise_id`` filter no enterprise could carry.

    Enterprise ids are at most ``MAX_IDENTIFIER_LENGTH`` characters, so a longer
    value names no enterprise — and under multi the filter is recorded as the
    access's target, a column of that width. A NUL character cannot be stored
    at all. Both are refused before anything is read or recorded.
    """
    if not enterprise_id.strip():
        detail = "enterprise_id must not be empty"
    elif len(enterprise_id) > MAX_IDENTIFIER_LENGTH:
        detail = (
            f"enterprise_id is longer than an enterprise id can be "
            f"({MAX_IDENTIFIER_LENGTH} characters)"
        )
    elif "\x00" in enterprise_id:
        # The account store answers a NUL in any string filter as "matches
        # nothing" (``PostgreSQLUserRepository._account_filters``). This one
        # is refused instead: under multi it is written to the access record
        # before the read, and a NUL makes that write fail.
        detail = "enterprise_id must not contain a NUL character"
    else:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
    )


async def _list_across_enterprises(
    request: Request,
    *,
    directory: Optional[IAccountDirectory],
    operator: AuthenticatedUser,
    own_enterprise: str,
    is_active: Optional[bool],
    role: Optional[str],
    search: Optional[str],
    enterprise_id: Optional[str],
    limit: int,
    offset: int,
) -> AdminUserListResponse:
    """The ``TENANT_PROVIDER=multi`` arm — the one caller of
    ``list_account_metadata``.

    Refusals before the access is recorded: a ``role`` filter (422), no account
    store (503), no audit trail or a failed record (503). After it, any failure
    is one logged event and a 500.
    """
    if role is not None:
        # Roles are not part of the cross-enterprise read, so a role filter
        # could only be applied to the operator's own enterprise — a confined
        # list under a cross-enterprise name. Refused before anything is read.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_ROLE_FILTER_REFUSED_DETAIL,
        )
    if directory is None:
        # Fail closed, before recording: nothing was read.
        logger.error("admin_user_list_unwired: no account store is composed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_NO_ACCOUNT_STORE_DETAIL,
        )

    # Record the access BEFORE serving it, and fail closed (503) when it cannot
    # be recorded: a cross-enterprise read with no row behind it is the failure
    # the trail exists to prevent. ``surface`` tells this list from the case
    # list, which records the same metadata action. ``target_enterprise_id`` is
    # the enterprise filter, NULL when the read spans every enterprise. The
    # search text itself is NOT recorded — only whether there was one: it is
    # often an email address, and this row is append-only, so it could never be
    # erased.
    await record_operator_access(
        audit_repo=await get_operator_audit_repository(request),
        operator=operator,
        action=OperatorAction.LIST,
        deployment_mode=resolved_deployment_mode(),
        target_enterprise_id=enterprise_id,
        details={
            "surface": "accounts",
            "view": "metadata",
            "is_active_filter": is_active,
            "search_present": search is not None,
            "limit": limit,
            "offset": offset,
        },
    )

    try:
        accounts, total = await directory.list_account_metadata(
            is_active=is_active,
            search=search,
            enterprise_id=enterprise_id,
            limit=limit,
            offset=offset,
        )
        own_ids = [a.user_id for a in accounts if a.enterprise_id == own_enterprise]
        # Roles for the page's own-enterprise rows, in one read confined to the
        # operator's enterprise. Nothing else on the page is asked about.
        managed = (
            {
                user.user_id: user
                for user in await directory.get_many_in_enterprise(
                    own_enterprise, own_ids
                )
            }
            if own_ids
            else {}
        )
    except Exception:
        # One event, with the traceback for the operator's logs; the caller
        # gets fixed text, never the error.
        logger.exception("admin_user_list_read_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        )

    rows = []
    for account in accounts:
        user = managed.get(account.user_id)
        if user is None and account.enterprise_id == own_enterprise:
            # Listed by the cross-enterprise read, gone from the confined one a
            # moment later (deleted, or no longer anchored here). Served as not
            # manageable: an administration call would answer 404.
            logger.warning(
                "admin_user_list_account_vanished: account %s was listed in the "
                "operator's enterprise but not found by the confined read",
                account.user_id,
            )
        rows.append(_cross_enterprise_item(account, user))

    logger.info(
        "admin_user_list_access",
        extra={
            "admin_user_id": operator.user_id,
            "result_count": len(rows),
            "total_count": total,
        },
    )
    return AdminUserListResponse(users=rows, total=total, limit=limit, offset=offset)


def _manageable_item(user, *, enterprise_id: str) -> AdminUserListItem:
    """An account the operator administers, from the confined account read."""
    return AdminUserListItem(
        user_id=user.user_id,
        enterprise_id=enterprise_id,
        email=user.email,
        full_name=user.display_name,
        roles=effective_roles(user.roles),
        account_kind=user.account_kind,
        service_channel=user.service_channel,
        is_active=user.is_active,
        is_verified=user.is_email_verified,
        # Through to_json_compatible (fm#1129): under SQLite these datetimes
        # come back naive, and a naive datetime in a pydantic field serializes
        # suffix-less while /auth/me emits 'Z' for the same row.
        # to_json_compatible stamps 'Z' (naive = UTC here); pydantic round-trips
        # that string to an aware datetime and re-emits it as 'Z', so the two
        # endpoints agree on the wire.
        last_login_at=to_json_compatible(user.last_login_at),
        created_at=to_json_compatible(user.created_at),
        updated_at=to_json_compatible(user.updated_at),
        manageable=True,
    )


def _cross_enterprise_item(account: AccountMetadata, user) -> AdminUserListItem:
    """One row of the cross-enterprise list.

    ``user`` is the account as the confined read returned it — present only for
    an account in the operator's own enterprise. With it the row is manageable
    and carries the account's roles; without it ``roles`` is ``[]`` (not
    reported) and the row is not manageable. Every other field is the
    cross-enterprise read's, which a parity test holds equal to the confined
    listing's for the operator's own enterprise.
    """
    return AdminUserListItem(
        user_id=account.user_id,
        enterprise_id=account.enterprise_id,
        email=account.email,
        full_name=account.display_name,
        roles=effective_roles(user.roles) if user is not None else [],
        account_kind=account.account_kind,
        service_channel=account.service_channel,
        is_active=account.is_active,
        is_verified=account.is_email_verified,
        last_login_at=to_json_compatible(account.last_login_at),
        created_at=to_json_compatible(account.created_at),
        updated_at=to_json_compatible(account.updated_at),
        manageable=user is not None,
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
            enterprise to be confined to
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
            enterprise_id=current_user.enterprise_id,
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
            detail="Failed to get user details",
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
            enterprise_id=current_user.enterprise_id,
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
            detail="Failed to deactivate user",
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
            enterprise to be confined to
        404 Not Found: User does not exist, or is not in the operator's
            organization — one answer for both, deliberately
        409 Conflict: User already active
    """
    if not await scope.admits(current_user, user_id):
        raise user_not_found(user_id)

    try:
        updated_user = await user_service.activate_user_admin(
            user_id=user_id,
            enterprise_id=current_user.enterprise_id,
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
            detail="Failed to activate user",
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
            enterprise_id=current_user.enterprise_id,
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
            detail="Failed to assign role",
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
            enterprise_id=current_user.enterprise_id,
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
            detail="Failed to remove role",
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
            detail="Failed to get LLM routing health",
        )
