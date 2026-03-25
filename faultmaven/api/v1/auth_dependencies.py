"""Authentication Dependencies

Purpose: Reusable FastAPI dependencies for authentication operations

This module provides clean, reusable authentication dependencies that can be used
across all FastAPI routes. It handles token extraction, validation, and user
resolution with proper error handling and logging.

Key Dependencies:
- get_token_manager: Access via app.state (Composition Root)
- get_user_store: Access via app.state (Composition Root)
- extract_bearer_token: Clean token extraction from Authorization header
- get_current_user_optional: Optional user authentication
- require_authentication: Mandatory user authentication
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

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPBearer

from faultmaven.config.settings import get_settings
from faultmaven.models.auth import DevUser

# Initialize logger and security scheme
logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


# JWT Helper Functions
def get_verification_key() -> str:
    """Get JWT verification key based on auth mode.

    Returns the appropriate key for JWT validation:
    - Local mode (AUTH_MODE=local): Returns JWT_SECRET_KEY for HS256 validation
    - OAuth mode (AUTH_MODE=oauth): Returns public key for RS256 validation

    Returns:
        Verification key string

    Raises:
        RuntimeError: If required key is not configured

    Notes:
        - Per iam-design.md: "Unified JWT Format: Both Local and Cloud modes
          use JWT tokens with identical structure"
        - Algorithm is implicitly determined by AUTH_MODE:
          * local → HS256 (symmetric)
          * oauth → RS256 (asymmetric)
    """
    settings = get_settings()

    # Determine algorithm from auth mode per iam-design.md
    # local → HS256, oauth → RS256
    if settings.auth.auth_mode == "local":
        # Local mode: HS256 symmetric key
        if not settings.security.jwt_secret_key:
            raise RuntimeError(
                "JWT_SECRET_KEY not configured for local mode authentication. "
                "Set JWT_SECRET_KEY environment variable."
            )
        return settings.security.jwt_secret_key.get_secret_value()

    elif settings.auth.auth_mode == "oauth":
        # OAuth mode: RS256 public key for verification
        if settings.security.jwt_public_key:
            return settings.security.jwt_public_key
        elif settings.security.jwt_public_key_path:
            try:
                with open(settings.security.jwt_public_key_path, "r") as f:
                    return f.read()
            except FileNotFoundError:
                raise RuntimeError(
                    f"JWT public key file not found: {settings.security.jwt_public_key_path}"
                )
        else:
            raise RuntimeError(
                "JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_PATH not configured for OAuth mode. "
                "Set JWT_PUBLIC_KEY or JWT_PUBLIC_KEY_PATH environment variable."
            )

    else:
        raise RuntimeError(
            f"Unsupported auth mode: {settings.auth.auth_mode}. "
            "Only 'local' and 'oauth' are supported."
        )


# Service Dependencies (Composition Root pattern - access via app.state)
async def get_token_manager(request: Request):
    """Get token manager from app.state (Composition Root)

    Returns:
        Token manager instance (RedisTokenManager)

    Raises:
        HTTPException: 503 if service unavailable
    """
    try:
        token_manager = getattr(request.app.state, "token_manager", None)
        if not token_manager:
            logger.error("Token manager not available from app.state")
            raise HTTPException(
                status_code=503,
                detail="Authentication service unavailable. Please check server startup logs.",
            )
        return token_manager
    except HTTPException:
        raise
    except AttributeError as e:
        logger.error(f"Token manager attribute not found in app.state: {e}")
        raise HTTPException(
            status_code=503,
            detail="Authentication service not initialized. Please check server startup logs.",
        )
    except Exception as e:
        logger.error(f"Failed to get token manager: {e}")
        raise HTTPException(status_code=503, detail="Authentication service error")


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
    request: Request, token: Optional[str] = Depends(extract_bearer_token)
) -> Optional[DevUser]:
    """Get current user from JWT token (optional - no error if missing/invalid)

    Implements unified JWT validation per iam-design.md:
    - HS256 for local mode (symmetric key validation)
    - RS256 for OAuth mode (public key validation)

    Args:
        request: FastAPI request object
        token: Bearer token from header (JWT format)

    Returns:
        DevUser if valid JWT provided, None otherwise

    Notes:
        - Does not raise exceptions for missing/invalid tokens
        - Logs validation failures at debug level
        - Used for endpoints that work both authenticated and unauthenticated
        - Validates JWT structure, signature, expiration, issuer, and audience
    """
    if not token:
        return None

    try:
        settings = get_settings()

        # Determine algorithm from auth mode (per iam-design.md)
        algorithm = "HS256" if settings.auth.auth_mode == "local" else "RS256"

        # Validate JWT token using unified verification key
        claims = jwt.decode(
            token,
            key=get_verification_key(),
            algorithms=[algorithm],
            audience=settings.security.jwt_audience,
            issuer=settings.security.jwt_issuer,
        )

        # Extract user information from JWT claims
        user = DevUser(
            user_id=claims["sub"],
            username=claims.get("username", ""),
            email=claims.get("email", ""),
            display_name=claims.get("username", ""),  # Use username as display name
            created_at=datetime.now(timezone.utc),  # JWT doesn't include created_at
            is_dev_user=claims.get("auth_mode") == "local",  # Local mode = dev user
            is_active=True,
            roles=claims.get("roles", ["user"]),
        )

        logger.debug(
            f"User authenticated via JWT: {user.user_id} "
            f"(mode: {claims.get('auth_mode', 'unknown')})"
        )
        return user

    except jwt.ExpiredSignatureError:
        logger.debug("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"JWT validation failed: {e}")
        return None
    except HTTPException:
        # Re-raise service availability errors (from get_verification_key)
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

    # Check token manager
    try:
        token_manager = getattr(request.app.state, "token_manager", None)
        health_status["authentication"]["services"]["token_manager"] = {
            "status": "available" if token_manager else "unavailable",
            "type": type(token_manager).__name__ if token_manager else None,
        }
    except Exception as e:
        health_status["authentication"]["services"]["token_manager"] = {
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


# Development Utilities (remove in production)
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


def resolve_accessible_scopes(user: Optional[DevUser] = None) -> dict:
    """Resolve accessible knowledge base scopes for a user to use in ChromaDB queries"""
    conditions = [{"scope": "global"}]

    if user:
        conditions.append({"$and": [{"scope": "personal"}, {"owner_id": user.user_id}]})

        team_id = getattr(user, "organization_id", None) or getattr(
            user, "team_id", None
        )
        if team_id:
            conditions.append({"$and": [{"scope": "team"}, {"team_id": team_id}]})

    return {"$or": conditions}
