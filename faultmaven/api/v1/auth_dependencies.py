"""Authentication Dependencies

Purpose: Reusable FastAPI dependencies for authentication operations

This module provides clean, reusable authentication dependencies that can be used
across all FastAPI routes. It handles token extraction, validation, and user
resolution with proper error handling and logging.

Key Dependencies:
- get_token_revocation_store: Access via app.state (Composition Root)
- get_user_store: Access via app.state (Composition Root)
- extract_bearer_token: Clean token extraction from Authorization header
- get_current_user_optional: Optional user authentication
- require_authentication: Mandatory user authentication
- require_platform_admin: Mandatory cross-tenant operator role
- get_current_user_id: Extract just the user ID for service layer

Design Principles:
- Clean separation of concerns
- Consistent error responses
- Proper logging with correlation IDs
- Composition Root pattern (services via app.state, not container.get_*)
- Easy to test and mock
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPBearer

from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.config.tenant_context import get_current_org_id
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)

# Initialize logger and security scheme
logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


# Service Dependencies (Composition Root pattern - access via app.state)
async def get_token_revocation_store(request: Request):
    """Get the deployment-wide token revocation store from app.state.

    This is the single revocation store (#767): every revoke path writes to
    it and the request-path revocation check reads from it. Handlers must use
    this shared instance — building a separate store would silently fork the
    revocation namespace again.

    Returns:
        RedisTokenRevocationStore instance

    Raises:
        HTTPException: 503 if service unavailable
    """
    store = getattr(request.app.state, "token_revocation_store", None)
    if store is None:
        logger.error("Token revocation store not available from app.state")
        raise HTTPException(
            status_code=503,
            detail="Authentication service unavailable. Please check server startup logs.",
        )
    return store


async def get_user_store(request: Request):
    """Get user store from app.state (Composition Root)

    Returns:
        User store instance (DatabaseUserStore or RedisUserStore)

    Raises:
        HTTPException: 503 if service unavailable
    """
    try:
        user_store = getattr(request.app.state, "user_store", None)
        if not user_store:
            logger.error("User store not available from app.state")
            # Try to get from container as fallback for debugging
            try:
                from faultmaven.container import container

                container_user_store = container.get_user_store()
                logger.error(
                    f"Container user_store: {type(container_user_store).__name__ if container_user_store else 'None'}"
                )
                logger.error(f"Container initialized: {container.is_initialized}")
                logger.error(
                    f"Container has user_store attr: {hasattr(container, 'user_store')}"
                )
            except Exception as e:
                logger.error(f"Failed to check container: {e}")
            raise HTTPException(
                status_code=503,
                detail="User management service unavailable. Please check server startup logs.",
            )
        return user_store
    except HTTPException:
        raise
    except AttributeError as e:
        logger.error(f"User store attribute not found in app.state: {e}")
        raise HTTPException(
            status_code=503,
            detail="User management service not initialized. Please check server startup logs.",
        )
    except Exception as e:
        logger.error(f"Failed to get user store: {e}")
        raise HTTPException(status_code=503, detail="User management service error")


# Token Extraction
async def extract_bearer_token(
    authorization: Optional[str] = Header(None, alias="Authorization")
) -> Optional[str]:
    """Extract Bearer token from Authorization header

    Args:
        authorization: Authorization header value

    Returns:
        Token string if valid Bearer token provided, None otherwise

    Notes:
        - Returns None for missing or invalid headers (no exception)
        - Expects format: "Bearer <token>"
        - Used for optional authentication scenarios
    """
    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        logger.debug(f"Invalid authorization header format (not Bearer)")
        return None

    token = authorization[7:]  # Remove "Bearer " prefix
    if not token.strip():
        logger.debug("Empty token in Bearer header")
        return None

    return token.strip()


# User Authentication Dependencies
async def get_current_user_optional(
    request: Request,
    token: Optional[str] = Depends(extract_bearer_token),
    auth_service: AuthService = Depends(get_auth_service),
) -> Optional[DevUser]:
    """Get current user from JWT token (optional - no error if missing/invalid)

    Verification and revocation are delegated to the same
    ``AuthService.verify_token_with_revocation_check`` the mandatory-auth
    middleware (``api/middleware/auth.get_current_user``) and the tenant binder
    (``api/middleware/tenant_scope``) use: signature (HS256 local / RS256
    OAuth), expiration, issuer, audience, required claims (incl. ``jti``),
    ``type == "access"``, and the Redis revocation list. A revoked-but-unexpired
    token is unauthenticated here — it must not retain the identity it would be
    denied on every mandatory-auth endpoint (issue #761).

    Args:
        request: FastAPI request object
        token: Bearer token from header (JWT format)
        auth_service: AuthService from app.state (Composition Root)

    Returns:
        DevUser if valid JWT provided, None otherwise

    Notes:
        - Does not raise exceptions for missing/invalid/revoked tokens
        - Logs validation failures at debug level
        - Used for endpoints that work both authenticated and unauthenticated
    """
    if not token:
        return None

    try:
        claims = await auth_service.verify_token_with_revocation_check(
            token, token_type="access"
        )

        # Extract user information from JWT claims
        #
        # organization_id is sourced from the request-scoped tenant contextvar, NOT
        # the raw ``organization_id`` JWT claim. This keeps ``DevUser.organization_id``
        # definitionally equal to the org PostgreSQL RLS is enforcing for this request
        # (both read ``config.tenant_context``), so this object can never be a source
        # of tenant mis-scoping. The global ``bind_request_org_context`` dependency
        # (ADR-010 P2b) runs before this path dependency and has already resolved the
        # contextvar: forced to the Standalone org under single-tenant (ignoring any
        # injected claim — the re-leak guard), or the verified claim under multi-tenant
        # (having failed the request closed if that claim was missing).
        #
        # Sourcing the raw claim here instead would silently mask a missing claim to
        # the Standalone org (via ``DevUser.__post_init__``), and let a forged org
        # diverge from the RLS-scoped org. Live readers of this field (the knowledge
        # suggestions listing and conversion-job org-stamping, which take the
        # ``DevUser`` from this dependency) were therefore mis-scoping to Standalone
        # under multi-tenant; this corrects them. The agent/sessions/admin scoping
        # paths are unaffected — they read ``AuthenticatedUser`` (api/middleware/auth)
        # and the report path reads the contextvar directly (P2c).
        user = DevUser(
            user_id=claims["sub"],
            username=claims.get("username", ""),
            email=claims.get("email", ""),
            display_name=claims.get("username", ""),  # Use username as display name
            created_at=datetime.now(timezone.utc),  # JWT doesn't include created_at
            is_dev_user=claims.get("auth_mode") == "local",  # Local mode = dev user
            is_active=True,
            roles=claims.get("roles", ["user"]),
            organization_id=get_current_org_id(),
        )

        logger.debug(
            f"User authenticated via JWT: {user.user_id} "
            f"(mode: {claims.get('auth_mode', 'unknown')})"
        )
        return user

    except TokenRevocationError:
        logger.debug("JWT rejected: token has been revoked")
        return None
    except AuthenticationError as e:
        logger.debug(f"JWT validation failed: {e.message} ({e.error_code})")
        return None
    except HTTPException:
        # Re-raise service availability errors (from get_auth_service)
        raise
    except Exception as e:
        # Log unexpected errors but don't fail the request for optional auth
        correlation_id = str(uuid.uuid4())
        logger.warning(
            f"Unexpected error in JWT validation: {e} (correlation: {correlation_id})"
        )
        return None


async def require_authentication(
    user: Optional[DevUser] = Depends(get_current_user_optional),
) -> DevUser:
    """Require authenticated user (raises 401 if not authenticated)

    Args:
        user: User from optional dependency

    Returns:
        Authenticated DevUser

    Raises:
        HTTPException: 401 if user not authenticated

    Notes:
        - Use this for endpoints that require authentication
        - Returns proper WWW-Authenticate header for OAuth2 compliance
        - Provides clear error message for missing authentication
    """
    if not user:
        correlation_id = str(uuid.uuid4())
        logger.info(
            f"Authentication required but not provided (correlation: {correlation_id})"
        )
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to access this resource.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.debug(f"Authentication successful for user: {user.user_id}")
    return user


# Service Layer Dependencies (for clean separation)
async def get_current_user_id(user: DevUser = Depends(require_authentication)) -> str:
    """Extract just the user ID for service layer operations

    Args:
        user: Authenticated user from require_authentication

    Returns:
        User ID string

    Notes:
        - Provides clean interface for service layer
        - Services receive user_id directly rather than full user object
        - Maintains separation between API and service layers
    """
    return user.user_id


async def get_current_user_id_optional(
    user: Optional[DevUser] = Depends(get_current_user_optional),
) -> Optional[str]:
    """Extract user ID for optional authentication scenarios

    Args:
        user: Optional user from get_current_user_optional

    Returns:
        User ID string if authenticated, None otherwise

    Notes:
        - For endpoints that behave differently when authenticated
        - Services can check if user_id is None to determine auth status
    """
    return user.user_id if user else None


# Health Check Dependency
async def check_auth_services_health(request: Request) -> dict:
    """Check health of authentication services

    Args:
        request: FastAPI request object

    Returns:
        Dict with service health status

    Notes:
        - Used by health check endpoints
        - Does not raise exceptions on service failures
        - Returns detailed status for monitoring
    """
    health_status = {"authentication": {"status": "unknown", "services": {}}}

    # Check token revocation store (#767: revocation is unenforceable without it)
    try:
        revocation_store = getattr(request.app.state, "token_revocation_store", None)
        health_status["authentication"]["services"]["token_revocation_store"] = {
            "status": "available" if revocation_store else "unavailable",
            "type": type(revocation_store).__name__ if revocation_store else None,
        }
    except Exception as e:
        health_status["authentication"]["services"]["token_revocation_store"] = {
            "status": "error",
            "error": str(e),
        }

    # Check user store
    try:
        user_store = getattr(request.app.state, "user_store", None)
        health_status["authentication"]["services"]["user_store"] = {
            "status": "available" if user_store else "unavailable",
            "type": type(user_store).__name__ if user_store else None,
        }
    except Exception as e:
        health_status["authentication"]["services"]["user_store"] = {
            "status": "error",
            "error": str(e),
        }

    # Determine overall status
    all_services_healthy = all(
        service.get("status") == "available"
        for service in health_status["authentication"]["services"].values()
    )

    health_status["authentication"]["status"] = (
        "healthy" if all_services_healthy else "degraded"
    )

    return health_status


# Convenience Dependencies (commonly used patterns)
async def get_authenticated_user_context(
    user: DevUser = Depends(require_authentication), correlation_id: str = None
) -> dict:
    """Get complete authenticated user context for request processing

    Args:
        user: Authenticated user
        correlation_id: Optional correlation ID for request tracing

    Returns:
        Dict with user context information

    Notes:
        - Provides rich context for request processing
        - Includes user info and request metadata
        - Useful for audit logging and tracing
    """
    if not correlation_id:
        correlation_id = str(uuid.uuid4())

    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "is_dev_user": user.is_dev_user,
        "correlation_id": correlation_id,
        "authenticated": True,
    }


async def get_optional_user_context(
    user: Optional[DevUser] = Depends(get_current_user_optional),
    correlation_id: str = None,
) -> dict:
    """Get user context for optional authentication scenarios

    Args:
        user: Optional user
        correlation_id: Optional correlation ID for request tracing

    Returns:
        Dict with user context (may indicate unauthenticated)

    Notes:
        - For endpoints that work with or without authentication
        - Always returns context dict, authenticated field indicates status
        - Correlation ID provided for all requests (auth and unauth)
    """
    if not correlation_id:
        correlation_id = str(uuid.uuid4())

    if user:
        return {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "is_dev_user": user.is_dev_user,
            "correlation_id": correlation_id,
            "authenticated": True,
        }
    else:
        return {
            "user_id": None,
            "username": None,
            "email": None,
            "is_dev_user": False,
            "correlation_id": correlation_id,
            "authenticated": False,
        }


async def require_platform_admin(
    user: DevUser = Depends(require_authentication),
) -> DevUser:
    """Require the cross-tenant operator role (``platform_admin``).

    Guards the endpoints that act on the deployment as a whole rather than on
    one organization. The org-scoped ``Role.ADMIN`` does not satisfy it.

    The ``AuthenticatedUser`` equivalent is
    ``api.middleware.auth.require_platform_admin`` — same policy, other user
    representation. Keep the two in step.

    Raises:
        HTTPException: 403 if user does not have the platform_admin role
    """
    if not user.is_platform_admin():
        logger.warning(f"Platform admin access denied for user: {user.user_id}")
        raise HTTPException(
            status_code=403, detail="Platform administrator access required"
        )
    return user


async def require_dev_user(user: DevUser = Depends(require_authentication)) -> DevUser:
    """Require authenticated development user

    Args:
        user: Authenticated user

    Returns:
        DevUser if is development user

    Raises:
        HTTPException: 403 if not a development user

    Notes:
        - For development-only endpoints
        - Remove or modify for production deployment
        - Provides additional layer of access control
    """
    if not user.is_dev_user:
        logger.warning(f"Non-dev user attempted to access dev endpoint: {user.user_id}")
        raise HTTPException(status_code=403, detail="Development user access required")

    return user
