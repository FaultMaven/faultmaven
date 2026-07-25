"""Role-Based Authorization Dependencies

Purpose: FastAPI dependencies for role-based access control

This module provides reusable role-based authorization dependencies that can be used
across all FastAPI routes. It checks user roles and enforces access control policies.

Key Dependencies:
- require_platform_admin: Ensures user has the cross-tenant operator role
- check_user_has_role: Helper function for role validation

Design Principles:
- Consistent with existing auth_dependencies.py patterns
- Clear error messages for authorization failures
- Proper logging for security monitoring
- Reusable across all endpoints
"""

import logging
import uuid

from fastapi import Depends, HTTPException

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.auth.contracts import PLATFORM_ADMIN_ROLE
from faultmaven.modules.auth.domain.models.auth import DevUser

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


async def require_platform_admin(
    user: DevUser = Depends(require_authentication),
) -> DevUser:
    """Require the cross-tenant operator role (ADR-012 D9)

    This is the DEPLOYMENT-scoped operator role, not the organization-scoped
    ``Role.ADMIN``. An org admin governs their own tenant and must not reach
    endpoints guarded by this dependency.

    Args:
        user: Authenticated user from require_authentication

    Returns:
        Authenticated user holding the platform_admin role

    Raises:
        HTTPException: 403 if user does not have the platform_admin role

    Usage:
        @router.post("/operator-only-endpoint")
        async def operator_endpoint(
            current_user: DevUser = Depends(require_platform_admin)
        ):
            # Only platform admins can reach this code
            ...
    """
    correlation_id = str(uuid.uuid4())

    if not check_user_has_role(user, PLATFORM_ADMIN_ROLE):
        logger.warning(
            f"Authorization denied: User {user.user_id} ({user.username}) "
            f"attempted operator-only operation without the {PLATFORM_ADMIN_ROLE} role "
            f"(roles: {user.roles}, correlation: {correlation_id})"
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Forbidden",
                "message": "This operation requires platform administrator privileges",
                "required_role": PLATFORM_ADMIN_ROLE,
                "user_roles": user.roles if user.roles else [],
            },
        )

    logger.debug(
        f"Platform admin authorization successful for user {user.user_id} "
        f"(correlation: {correlation_id})"
    )
    return user
