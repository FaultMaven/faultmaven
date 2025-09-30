"""Authentication Dependencies

Purpose: Reusable FastAPI dependencies for authentication operations

This module provides clean, reusable authentication dependencies that can be used
across all FastAPI routes. It handles token extraction, validation, and user
resolution with proper error handling and logging.

Key Dependencies:
- get_token_manager: DI container access for token operations
- get_user_store: DI container access for user operations
- extract_bearer_token: Clean token extraction from Authorization header
- get_current_user_optional: Optional user authentication
- require_authentication: Mandatory user authentication
- get_current_user_id: Extract just the user ID for service layer

Design Principles:
- Clean separation of concerns
- Consistent error responses
- Proper logging with correlation IDs
- Interface-based dependency injection
- Easy to test and mock
"""

import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Depends, Header
from fastapi.security import HTTPBearer

from faultmaven.models.auth import DevUser
from faultmaven.container import container

# Initialize logger and security scheme
logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


# Container Service Dependencies
async def get_token_manager():
    """Get token manager from DI container

    Returns:
        DevTokenManager instance

    Raises:
        HTTPException: 503 if service unavailable
    """
    try:
        token_manager = container.get_token_manager()
        if not token_manager:
            logger.error("Token manager not available from container")
            raise HTTPException(
                status_code=503,
                detail="Authentication service unavailable"
            )
        return token_manager
    except Exception as e:
        logger.error(f"Failed to get token manager: {e}")
        raise HTTPException(
            status_code=503,
            detail="Authentication service error"
        )


async def get_user_store():
    """Get user store from DI container

    Returns:
        DevUserStore instance

    Raises:
        HTTPException: 503 if service unavailable
    """
    try:
        user_store = container.get_user_store()
        if not user_store:
            logger.error("User store not available from container")
            raise HTTPException(
                status_code=503,
                detail="User management service unavailable"
            )
        return user_store
    except Exception as e:
        logger.error(f"Failed to get user store: {e}")
        raise HTTPException(
            status_code=503,
            detail="User management service error"
        )


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
    token: Optional[str] = Depends(extract_bearer_token)
) -> Optional[DevUser]:
    """Get current user from token (optional - no error if missing/invalid)

    Args:
        token: Bearer token from header

    Returns:
        DevUser if valid token provided, None otherwise

    Notes:
        - Does not raise exceptions for missing/invalid tokens
        - Logs validation failures at debug level
        - Used for endpoints that work both authenticated and unauthenticated
    """
    if not token:
        return None

    try:
        token_manager = await get_token_manager()
        validation_result = await token_manager.validate_token(token)

        if validation_result.is_valid and validation_result.user:
            logger.debug(f"User authenticated: {validation_result.user.user_id}")
            return validation_result.user
        else:
            # Log at debug level - not an error for optional auth
            logger.debug(f"Token validation failed: {validation_result.error_message}")
            return None

    except HTTPException:
        # Re-raise service availability errors
        raise
    except Exception as e:
        # Log unexpected errors but don't fail the request for optional auth
        correlation_id = str(uuid.uuid4())
        logger.warning(f"Unexpected error in optional auth: {e} (correlation: {correlation_id})")
        return None



async def require_authentication(
    user: Optional[DevUser] = Depends(get_current_user_optional)
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
        logger.info(f"Authentication required but not provided (correlation: {correlation_id})")
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to access this resource.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    logger.debug(f"Authentication successful for user: {user.user_id}")
    return user


# Service Layer Dependencies (for clean separation)
async def get_current_user_id(
    user: DevUser = Depends(require_authentication)
) -> str:
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
    user: Optional[DevUser] = Depends(get_current_user_optional)
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
async def check_auth_services_health() -> dict:
    """Check health of authentication services

    Returns:
        Dict with service health status

    Notes:
        - Used by health check endpoints
        - Does not raise exceptions on service failures
        - Returns detailed status for monitoring
    """
    health_status = {
        "authentication": {
            "status": "unknown",
            "services": {}
        }
    }

    # Check token manager
    try:
        token_manager = container.get_token_manager()
        health_status["authentication"]["services"]["token_manager"] = {
            "status": "available" if token_manager else "unavailable",
            "type": "DevTokenManager" if token_manager else None
        }
    except Exception as e:
        health_status["authentication"]["services"]["token_manager"] = {
            "status": "error",
            "error": str(e)
        }

    # Check user store
    try:
        user_store = container.get_user_store()
        health_status["authentication"]["services"]["user_store"] = {
            "status": "available" if user_store else "unavailable",
            "type": "DevUserStore" if user_store else None
        }
    except Exception as e:
        health_status["authentication"]["services"]["user_store"] = {
            "status": "error",
            "error": str(e)
        }

    # Determine overall status
    all_services_healthy = all(
        service.get("status") == "available"
        for service in health_status["authentication"]["services"].values()
    )

    health_status["authentication"]["status"] = "healthy" if all_services_healthy else "degraded"

    return health_status


# Convenience Dependencies (commonly used patterns)
async def get_authenticated_user_context(
    user: DevUser = Depends(require_authentication),
    correlation_id: str = None
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
        "authenticated": True
    }


async def get_optional_user_context(
    user: Optional[DevUser] = Depends(get_current_user_optional),
    correlation_id: str = None
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
            "authenticated": True
        }
    else:
        return {
            "user_id": None,
            "username": None,
            "email": None,
            "is_dev_user": False,
            "correlation_id": correlation_id,
            "authenticated": False
        }


# Development Utilities (remove in production)
async def require_dev_user(
    user: DevUser = Depends(require_authentication)
) -> DevUser:
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
        raise HTTPException(
            status_code=403,
            detail="Development user access required"
        )

    return user