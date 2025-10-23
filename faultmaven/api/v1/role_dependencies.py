"""Role-Based Authorization Dependencies

Purpose: FastAPI dependencies for role-based access control

This module provides reusable role-based authorization dependencies that can be used
across all FastAPI routes. It checks user roles and enforces access control policies.

Key Dependencies:
- require_admin: Ensures user has 'admin' role
- require_roles: Flexible role requirement checking
- check_user_has_role: Helper function for role validation

Design Principles:
- Consistent with existing auth_dependencies.py patterns
- Clear error messages for authorization failures
- Proper logging for security monitoring
- Reusable across all endpoints
"""

import uuid
import logging
from typing import List

from fastapi import HTTPException, Depends

from faultmaven.models.auth import DevUser
from faultmaven.api.v1.auth_dependencies import require_authentication

# Initialize logger
logger = logging.getLogger(__name__)


def check_user_has_role(user: DevUser, required_role: str) -> bool:
    """Check if user has a specific role

    Args:
        user: Authenticated user
        required_role: Role to check for (e.g., 'admin', 'user')

    Returns:
        True if user has the role, False otherwise
    """
    if not user.roles:
        return False

    return required_role in user.roles


def check_user_has_any_role(user: DevUser, required_roles: List[str]) -> bool:
    """Check if user has any of the required roles

    Args:
        user: Authenticated user
        required_roles: List of acceptable roles

    Returns:
        True if user has at least one of the required roles
    """
    if not user.roles:
        return False

    return any(role in user.roles for role in required_roles)


async def require_admin(
    user: DevUser = Depends(require_authentication)
) -> DevUser:
    """Require user to have 'admin' role

    Args:
        user: Authenticated user from require_authentication

    Returns:
        Authenticated user with admin role

    Raises:
        HTTPException: 403 if user does not have admin role

    Usage:
        @router.post("/admin-only-endpoint")
        async def admin_endpoint(
            current_user: DevUser = Depends(require_admin)
        ):
            # Only admins can reach this code
            ...
    """
    correlation_id = str(uuid.uuid4())

    if not check_user_has_role(user, 'admin'):
        logger.warning(
            f"Authorization denied: User {user.user_id} ({user.username}) "
            f"attempted admin-only operation without admin role (roles: {user.roles}, "
            f"correlation: {correlation_id})"
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Forbidden",
                "message": "This operation requires administrator privileges",
                "required_role": "admin",
                "user_roles": user.roles if user.roles else []
            }
        )

    logger.debug(f"Admin authorization successful for user {user.user_id} (correlation: {correlation_id})")
    return user


def require_roles(roles: List[str]):
    """Create a dependency that requires user to have any of the specified roles

    Args:
        roles: List of acceptable roles (user needs at least one)

    Returns:
        FastAPI dependency function

    Usage:
        @router.post("/editor-or-admin-endpoint")
        async def special_endpoint(
            current_user: DevUser = Depends(require_roles(['editor', 'admin']))
        ):
            # Users with 'editor' OR 'admin' role can reach this code
            ...
    """
    async def role_checker(user: DevUser = Depends(require_authentication)) -> DevUser:
        correlation_id = str(uuid.uuid4())

        if not check_user_has_any_role(user, roles):
            logger.warning(
                f"Authorization denied: User {user.user_id} ({user.username}) "
                f"attempted operation requiring roles {roles} but has {user.roles} "
                f"(correlation: {correlation_id})"
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "Forbidden",
                    "message": f"This operation requires one of the following roles: {', '.join(roles)}",
                    "required_roles": roles,
                    "user_roles": user.roles if user.roles else []
                }
            )

        logger.debug(
            f"Role authorization successful for user {user.user_id} "
            f"(required: {roles}, has: {user.roles}, correlation: {correlation_id})"
        )
        return user

    return role_checker


async def require_admin_or_owner(
    resource_user_id: str,
    current_user: DevUser = Depends(require_authentication)
) -> DevUser:
    """Require user to be either admin or the resource owner

    Args:
        resource_user_id: User ID of the resource owner
        current_user: Authenticated user

    Returns:
        Authenticated user if authorized

    Raises:
        HTTPException: 403 if user is neither admin nor owner

    Usage:
        @router.delete("/users/{user_id}/resource")
        async def delete_resource(
            user_id: str,
            current_user: DevUser = Depends(
                lambda user_id: require_admin_or_owner(user_id, current_user)
            )
        ):
            # Admins or the resource owner can reach this code
            ...
    """
    correlation_id = str(uuid.uuid4())

    # Check if user is admin or owns the resource
    is_admin = check_user_has_role(current_user, 'admin')
    is_owner = current_user.user_id == resource_user_id

    if not (is_admin or is_owner):
        logger.warning(
            f"Authorization denied: User {current_user.user_id} ({current_user.username}) "
            f"attempted to access resource owned by {resource_user_id} "
            f"(is_admin: {is_admin}, is_owner: {is_owner}, correlation: {correlation_id})"
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Forbidden",
                "message": "You can only access your own resources or must be an administrator",
                "user_id": current_user.user_id,
                "resource_user_id": resource_user_id
            }
        )

    logger.debug(
        f"Admin/owner authorization successful for user {current_user.user_id} "
        f"(is_admin: {is_admin}, is_owner: {is_owner}, correlation: {correlation_id})"
    )
    return current_user
