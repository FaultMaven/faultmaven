"""JWT Authentication Middleware (TASK-017)

Purpose: FastAPI dependencies for JWT-based authentication and authorization.

This module provides reusable authentication dependencies:
- get_current_user: Extract and verify JWT from Authorization header
- get_current_user_optional: Optional authentication (returns None if no token)
- require_permission: Check if user has specific permission
- require_role: Check if user has specific role
- require_admin: Shortcut for requiring admin role

Usage:
    from faultmaven.api.middleware.auth import get_current_user, require_permission

    @router.get("/cases")
    async def list_cases(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ):
        # current_user.user_id, current_user.organization_id verified
        ...

    @router.delete("/cases/{id}", dependencies=[Depends(require_role("admin"))])
    async def delete_case(...):
        # Only admins can reach this endpoint
        ...

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

import logging
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from faultmaven.models.auth import AuthenticatedUser
from faultmaven.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)

logger = logging.getLogger(__name__)

# HTTP Bearer security scheme for OpenAPI documentation
bearer_scheme = HTTPBearer(auto_error=False)

# Module-level test service for backward compatibility with tests
_test_auth_service: Optional[AuthService] = None


async def get_auth_service(request: Optional[Request] = None) -> AuthService:
    """Get AuthService instance from app.state (Composition Root).

    Args:
        request: FastAPI request object (optional for backward compatibility)

    Returns:
        AuthService instance from app.state (properly configured with Redis if available)

    Note:
        Uses app.state which is initialized at app startup (Composition Root pattern).
        Falls back to test service or creating a new instance if not available.
    """
    # Check test service first (for unit tests)
    global _test_auth_service
    if _test_auth_service is not None:
        return _test_auth_service

    # Try to get from app.state if request is provided
    if request is not None:
        try:
            auth_service = getattr(request.app.state, 'auth_service', None)
            if auth_service is not None:
                return auth_service
        except Exception as e:
            logger.warning(f"Failed to get AuthService from app.state: {e}")

    # Fallback: create a minimal AuthService without Redis
    logger.debug("Creating fallback AuthService (no Redis for token revocation)")
    return AuthService()


def set_auth_service(service: Optional[AuthService], request: Optional[Request] = None) -> None:
    """Set the AuthService instance (for testing/DI).

    Args:
        service: AuthService instance to use (or None to clear)
        request: Optional FastAPI request object (if provided, sets on app.state)

    Note:
        For testing, can be called without request to set module-level test service.
        For production DI, can be called with request to set on app.state.
    """
    global _test_auth_service

    if request is not None:
        try:
            request.app.state.auth_service = service
            logger.debug("AuthService set in app.state")
        except Exception as e:
            logger.warning(f"Failed to set AuthService in app.state: {e}")
    else:
        # Set module-level test service for backward compatibility
        _test_auth_service = service
        logger.debug("AuthService set in module-level test variable")


async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    """Extract and verify JWT from Authorization header.

    Validates the token and returns AuthenticatedUser with verified claims.

    Header format: "Bearer <jwt_token>"

    Args:
        authorization: Authorization header value (legacy support)
        credentials: HTTPBearer credentials (for OpenAPI docs)
        auth_service: AuthService instance

    Returns:
        AuthenticatedUser with user_id, organization_id, email, roles, permissions

    Raises:
        HTTPException 401: Missing or invalid token
        HTTPException 403: Token expired or revoked
    """
    # Extract token from header
    token = _extract_token(authorization, credentials)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Verify token and check revocation (if Redis configured)
        user = await auth_service.extract_user_from_token_with_revocation_check(token)
        return user

    except AuthenticationError as e:
        logger.debug(f"Authentication failed: {e.message} ({e.error_code})")

        if e.error_code == "TOKEN_EXPIRED":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired. Please refresh or re-authenticate.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e.message}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except TokenRevocationError:
        logger.debug("Token has been revoked")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token has been revoked. Please re-authenticate.",
        )

    except Exception as e:
        logger.error(f"Unexpected authentication error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed. Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user_optional(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> Optional[AuthenticatedUser]:
    """Get current user if authenticated, or None if not.

    Use this for endpoints that work both authenticated and unauthenticated.

    Args:
        authorization: Authorization header value
        credentials: HTTPBearer credentials
        auth_service: AuthService instance

    Returns:
        AuthenticatedUser if authenticated, None otherwise
    """
    token = _extract_token(authorization, credentials)

    if not token:
        return None

    try:
        return await auth_service.extract_user_from_token_with_revocation_check(token)
    except (AuthenticationError, TokenRevocationError):
        return None
    except Exception as e:
        logger.warning(f"Optional authentication error: {e}")
        return None


def require_permission(permission: str) -> Callable:
    """Create dependency that checks for specific permission.

    Usage:
        @router.post("/cases", dependencies=[Depends(require_permission("cases:write"))])
        async def create_case(...):
            ...

    Args:
        permission: Permission string (e.g., "cases:write", "sessions:execute")

    Returns:
        Dependency function that validates permission

    Raises:
        HTTPException 403: User lacks required permission
    """

    async def permission_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not current_user.has_permission(permission):
            logger.debug(
                f"Permission denied: user {current_user.user_id} "
                f"lacks permission {permission}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return current_user

    return permission_checker


def require_any_permission(*permissions: str) -> Callable:
    """Create dependency that checks for any of the specified permissions.

    Usage:
        @router.get("/items", dependencies=[Depends(require_any_permission("items:read", "items:admin"))])

    Args:
        permissions: Permission strings to check

    Returns:
        Dependency function that validates any permission
    """

    async def permission_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not current_user.has_any_permission(list(permissions)):
            logger.debug(
                f"Permission denied: user {current_user.user_id} "
                f"lacks all permissions: {permissions}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing one of required permissions: {', '.join(permissions)}",
            )
        return current_user

    return permission_checker


def require_all_permissions(*permissions: str) -> Callable:
    """Create dependency that checks for all specified permissions.

    Usage:
        @router.delete("/items/{id}", dependencies=[Depends(require_all_permissions("items:read", "items:delete"))])

    Args:
        permissions: Permission strings to check

    Returns:
        Dependency function that validates all permissions
    """

    async def permission_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not current_user.has_all_permissions(list(permissions)):
            missing = [p for p in permissions if not current_user.has_permission(p)]
            logger.debug(
                f"Permission denied: user {current_user.user_id} "
                f"missing permissions: {missing}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permissions: {', '.join(missing)}",
            )
        return current_user

    return permission_checker


def require_role(role: str) -> Callable:
    """Create dependency that checks for specific role.

    Usage:
        @router.delete("/cases/{id}", dependencies=[Depends(require_role("admin"))])
        async def delete_case(...):
            ...

    Args:
        role: Role name (admin, member, viewer)

    Returns:
        Dependency function that validates role

    Raises:
        HTTPException 403: User lacks required role
    """

    async def role_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not current_user.has_role(role):
            logger.debug(
                f"Role denied: user {current_user.user_id} lacks role {role}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required role: {role}",
            )
        return current_user

    return role_checker


def require_any_role(*roles: str) -> Callable:
    """Create dependency that checks for any of the specified roles.

    Args:
        roles: Role names to check

    Returns:
        Dependency function that validates any role
    """

    async def role_checker(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not any(current_user.has_role(role) for role in roles):
            logger.debug(
                f"Role denied: user {current_user.user_id} "
                f"lacks all roles: {roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing one of required roles: {', '.join(roles)}",
            )
        return current_user

    return role_checker


async def require_admin(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require admin role.

    Shortcut dependency for admin-only endpoints.

    Usage:
        @router.delete("/org", dependencies=[Depends(require_admin)])

    Returns:
        AuthenticatedUser if admin

    Raises:
        HTTPException 403: User is not admin
    """
    if not current_user.is_admin():
        logger.debug(f"Admin required: user {current_user.user_id} is not admin")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return current_user


def _extract_token(
    authorization: Optional[str],
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """Extract JWT token from authorization header or credentials.

    Supports both:
    - Raw Authorization header: "Bearer <token>"
    - HTTPBearer credentials (for OpenAPI docs)

    Args:
        authorization: Authorization header value
        credentials: HTTPBearer credentials

    Returns:
        Token string or None
    """
    # Try HTTPBearer credentials first (cleaner)
    if credentials and credentials.credentials:
        return credentials.credentials

    # Fall back to raw header parsing
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:].strip()
            if token:
                return token
        else:
            logger.debug(
                "Invalid Authorization header format. Expected 'Bearer <token>'"
            )

    return None


# Backward compatibility aliases
get_authenticated_user = get_current_user
get_optional_user = get_current_user_optional
