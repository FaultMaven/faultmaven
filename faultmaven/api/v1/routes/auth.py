"""Authentication Routes

Purpose: FastAPI routes for authentication operations

This module provides authentication endpoints for the development environment,
including login, logout, and user profile operations. The endpoints follow
OAuth2/JWT conventions for future production compatibility.

Key Endpoints:
- POST /auth/dev-login: Development login with username
- POST /auth/logout: Token revocation
- GET /auth/me: Current user profile
- GET /auth/health: Authentication system health

Security Notes:
- All tokens are stored as SHA-256 hashes
- Automatic token expiration after 24 hours
- Input validation and sanitization
- Structured error responses
"""

import uuid
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, Response
from fastapi.security import HTTPBearer
from pydantic import ValidationError

from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api_auth import (
    DevLoginRequest,
    AuthTokenResponse,
    LogoutResponse,
    UserProfile,
    UserInfoResponse,
    AuthError,
    TokenValidationError,
    AuthenticationRequiredError
)
from faultmaven.models.auth import DevUser, TokenStatus
from faultmaven.container import container

# Initialize router and logger
router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)

# Security scheme for OpenAPI documentation
security = HTTPBearer(auto_error=False)


# Import clean authentication dependencies
from faultmaven.api.v1.auth_dependencies import (
    get_token_manager,
    get_user_store,
    require_authentication,
    extract_bearer_token,
    check_auth_services_health
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.services.domain.session_service import SessionService


# Authentication endpoints

@router.post("/dev-login", response_model=AuthTokenResponse, status_code=201)
@trace("auth_dev_login")
async def dev_login(
    request: DevLoginRequest,
    response: Response,
    session_service: SessionService = Depends(get_session_service)
) -> AuthTokenResponse:
    """Development login endpoint

    Authenticates existing users and generates authentication tokens.
    This endpoint is designed for development environments and will be replaced
    with production OAuth2/OIDC integration later.

    **Flow:**
    1. Validate username format
    2. Find existing user (returns 401 if user doesn't exist)
    3. Generate authentication token
    4. Return token with user profile

    **Security:**
    - Only authenticates existing users (no account creation)
    - Tokens expire after 24 hours
    - Input validation and sanitization
    - Proper OAuth2 error responses
    """
    correlation_id = str(uuid.uuid4())

    try:
        # Get required services
        user_store = await get_user_store()
        token_manager = await get_token_manager()

        # Try to find existing user
        user = await user_store.get_user_by_username(request.username)

        if user:
            logger.info(f"User login: {request.username} (existing user: {user.user_id})")
        else:
            # User doesn't exist - return authentication error
            logger.warning(f"Login attempt for non-existent user: {request.username}")
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials. User does not exist.",
                headers={"WWW-Authenticate": "Bearer"}
            )

        # Generate authentication token
        access_token = await token_manager.create_token(user)

        # Create session for multi-turn conversations
        session = await session_service.create_session(
            user_id=user.user_id,
            metadata={
                "login_method": "dev_login",
                "username": user.username,
                "correlation_id": correlation_id
            }
        )
        # Extract session_id from SessionContext tuple or object
        if isinstance(session, tuple):
            # If tuple is returned (SessionContext, bool), get the SessionContext
            session_context = session[0]
            session_id = getattr(session_context, 'session_id', str(session_context))
        else:
            # If SessionContext is returned directly
            session_id = getattr(session, 'session_id', str(session))

        # Build response
        user_profile = UserProfile(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at.isoformat(),
            is_dev_user=user.is_dev_user
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,  # 24 hours in seconds
            session_id=session_id,
            user=user_profile
        )

        # Set correlation ID in response headers
        response.headers["X-Correlation-Id"] = correlation_id

        logger.info(f"Login successful for user {user.user_id} (correlation: {correlation_id})")
        return token_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dev login failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )


@router.post("/dev-register", response_model=AuthTokenResponse, status_code=201)
@trace("auth_dev_register")
async def dev_register(
    request: DevLoginRequest,
    response: Response,
    session_service: SessionService = Depends(get_session_service)
) -> AuthTokenResponse:
    """Development registration endpoint

    Creates a new user account and generates an authentication token.
    This endpoint is designed for development environments and will be replaced
    with production registration flows later.

    **Flow:**
    1. Validate username format
    2. Check if user already exists (returns 409 if exists)
    3. Create new user account
    4. Generate authentication token
    5. Return token with user profile

    **Security:**
    - Prevents duplicate account creation
    - Tokens expire after 24 hours
    - Input validation and sanitization
    - Auto-generates email and display name if not provided
    """
    correlation_id = str(uuid.uuid4())

    try:
        # Get required services
        user_store = await get_user_store()
        token_manager = await get_token_manager()

        # Check if user already exists
        existing_user = await user_store.get_user_by_username(request.username)
        if existing_user:
            logger.warning(f"Registration attempt for existing user: {request.username}")
            raise HTTPException(
                status_code=409,
                detail=f"User with username '{request.username}' already exists. Please use login instead."
            )

        # Create new user
        user = await user_store.create_user(
            username=request.username,
            email=request.email,
            display_name=request.display_name
        )
        logger.info(f"User registration: {request.username} (new user: {user.user_id})")

        # Generate authentication token
        access_token = await token_manager.create_token(user)

        # Create session for multi-turn conversations
        session = await session_service.create_session(
            user_id=user.user_id,
            metadata={
                "login_method": "dev_register",
                "username": user.username,
                "correlation_id": correlation_id
            }
        )
        # Extract session_id from SessionContext tuple or object
        if isinstance(session, tuple):
            # If tuple is returned (SessionContext, bool), get the SessionContext
            session_context = session[0]
            session_id = getattr(session_context, 'session_id', str(session_context))
        else:
            # If SessionContext is returned directly
            session_id = getattr(session, 'session_id', str(session))

        # Build response
        user_profile = UserProfile(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at.isoformat(),
            is_dev_user=user.is_dev_user
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,  # 24 hours in seconds
            session_id=session_id,
            user=user_profile
        )

        # Set correlation ID in response headers
        response.headers["X-Correlation-Id"] = correlation_id

        logger.info(f"Registration successful for user {user.user_id} (correlation: {correlation_id})")
        return token_response

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Registration validation failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=400,
            detail=f"Registration failed: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Dev registration failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500,
            detail=f"Registration failed: {str(e)}"
        )


@router.post("/logout", response_model=LogoutResponse)
@trace("auth_logout")
async def logout(
    current_user: DevUser = Depends(require_authentication),
    token: str = Depends(extract_bearer_token)
) -> LogoutResponse:
    """Logout current user

    Revokes the current authentication token. The user will need to login
    again to access protected resources.

    **Flow:**
    1. Validate current authentication
    2. Revoke the current token
    3. Return confirmation
    """
    correlation_id = str(uuid.uuid4())

    try:
        token_manager = await get_token_manager()

        # Revoke the current token
        success = await token_manager.revoke_token(token)

        if success:
            logger.info(f"User logout: {current_user.user_id} (correlation: {correlation_id})")
            return LogoutResponse(
                message="Logged out successfully",
                revoked_tokens=1
            )
        else:
            logger.warning(f"Token revocation failed for user {current_user.user_id}")
            raise HTTPException(
                status_code=500,
                detail="Logout failed: Could not revoke token"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Logout failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500,
            detail=f"Logout failed: {str(e)}"
        )


@router.get("/me", response_model=UserInfoResponse)
@trace("auth_get_current_user")
async def get_current_user_profile(
    current_user: DevUser = Depends(require_authentication)
) -> UserInfoResponse:
    """Get current user profile

    Returns detailed information about the currently authenticated user,
    including profile data and token statistics.
    """
    correlation_id = str(uuid.uuid4())

    try:
        token_manager = await get_token_manager()

        # Get user's active tokens for statistics
        user_tokens = await token_manager.get_user_tokens(current_user.user_id)
        active_token_count = len([token for token in user_tokens if token.is_valid])

        # Build extended user profile
        user_info = UserInfoResponse(
            user_id=current_user.user_id,
            username=current_user.username,
            email=current_user.email,
            display_name=current_user.display_name,
            created_at=current_user.created_at.isoformat(),
            is_dev_user=current_user.is_dev_user,
            last_login=None,  # TODO: Implement last login tracking
            token_count=active_token_count
        )

        logger.debug(f"User profile requested: {current_user.user_id} (correlation: {correlation_id})")
        return user_info

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get user profile failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve user profile: {str(e)}"
        )


@router.get("/health")
@trace("auth_health_check")
async def auth_health_check():
    """Authentication system health check

    Returns the status of authentication services including token management
    and user storage systems.
    """
    try:
        # Use clean health check dependency
        health_status = await check_auth_services_health()

        # Add timestamp
        health_status["authentication"]["timestamp"] = datetime.utcnow().isoformat()

        return health_status["authentication"]

    except Exception as e:
        logger.error(f"Auth health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e)
        }


# Optional: Debug endpoint for development (remove in production)
@router.post("/dev/revoke-all-tokens", response_model=LogoutResponse)
@trace("auth_dev_revoke_all")
async def dev_revoke_all_user_tokens(
    current_user: DevUser = Depends(require_authentication)
) -> LogoutResponse:
    """Development endpoint: Revoke all tokens for current user

    **WARNING:** This endpoint is for development use only.
    It will be removed in production builds.
    """
    correlation_id = str(uuid.uuid4())

    try:
        token_manager = await get_token_manager()

        # Revoke all user tokens
        revoked_count = await token_manager.revoke_user_tokens(current_user.user_id)

        logger.info(f"Dev: Revoked all tokens for user {current_user.user_id}, count: {revoked_count} (correlation: {correlation_id})")

        return LogoutResponse(
            message=f"Revoked all {revoked_count} tokens for user",
            revoked_tokens=revoked_count
        )

    except Exception as e:
        logger.error(f"Dev token revocation failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500,
            detail=f"Token revocation failed: {str(e)}"
        )