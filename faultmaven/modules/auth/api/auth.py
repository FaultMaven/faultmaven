"""Authentication Routes

Purpose: FastAPI routes for authentication operations

This module provides authentication endpoints per iam-design.md.
Supports two authentication modes selected at deployment time:
- Local Mode: Simple username/password authentication for self-hosted deployments
- Cloud Mode: OAuth 2.0 + PKCE for multi-user SaaS deployments

Key Endpoints:
- GET /auth/config: Auth configuration discovery (determines client auth flow)
- POST /auth/login: Local mode login (AUTH_MODE=local only)
- POST /auth/register: Local mode registration (AUTH_MODE=local only)
- POST /auth/logout: Token revocation
- GET /auth/me: Current user profile
- GET /auth/health: Authentication system health

Security Notes:
- JWT tokens in both modes for middleware uniformity
- Automatic token expiration (configurable via JWT_ACCESS_TOKEN_EXPIRY)
- Input validation and sanitization
- Structured error responses per RFC 6749
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from faultmaven.api.v1.auth_dependencies import (
    check_auth_services_health,
    extract_bearer_token,
    get_token_manager,
    get_user_store,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.config.settings import AuthMode, get_settings
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.domain.models.api_auth import (
    AuthTokenResponse,
    DevLoginRequest,
    LogoutResponse,
    UserInfoResponse,
    UserProfile,
)
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.utils.serialization import to_json_compatible

# Initialize router and logger
router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)

# Security scheme for OpenAPI documentation
security = HTTPBearer(auto_error=False)


# =============================================================================
# Endpoint Gating (per iam-design.md)
# =============================================================================


async def require_local_mode() -> None:
    """Dependency that ensures we're in local auth mode.

    Per iam-design.md, local mode endpoints (/login, /register) should
    only be available when AUTH_MODE=local.

    Raises:
        HTTPException: 404 if not in local mode
    """
    settings = get_settings()
    if settings.auth.auth_mode != AuthMode.LOCAL:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "endpoint_not_available",
                "message": "This endpoint is only available in local auth mode",
                "hint": "Use OAuth endpoints for cloud deployments",
            },
        )


async def require_development_environment() -> None:
    """Dependency that ensures we're in development environment.

    Per iam-design.md, admin/debug endpoints should only be available
    in development environments, not in production.

    Raises:
        HTTPException: 404 if not in development
    """
    settings = get_settings()
    # Check if we're in development mode
    # The environment is typically set via ENVIRONMENT env var
    environment = getattr(settings, "environment", None)
    if environment and str(environment).lower() not in ("development", "dev", "local"):
        raise HTTPException(
            status_code=404,
            detail={
                "error": "endpoint_not_available",
                "message": "This endpoint is only available in development environment",
            },
        )


# =============================================================================
# Auth Configuration Discovery (per iam-design.md)
# =============================================================================


class OAuthConfigResponse(BaseModel):
    """OAuth configuration for cloud mode."""

    authorize_url: str
    token_url: str
    client_id: str
    scopes: list[str]


class AuthConfigResponse(BaseModel):
    """Auth configuration discovery response.

    Allows frontend to determine which authentication flow to use
    based on deployment configuration.
    """

    auth_mode: str
    login_endpoint: str | None = None
    register_endpoint: str | None = None
    supports_registration: bool
    oauth: OAuthConfigResponse | None = None


@router.get("/config", response_model=AuthConfigResponse)
@trace("auth_config")
async def get_auth_config() -> AuthConfigResponse:
    """Auth configuration discovery endpoint.

    Returns the authentication configuration for the current deployment.
    Frontend uses this to determine which auth flow to implement.

    **Local Mode Response:**
    ```json
    {
      "auth_mode": "local",
      "login_endpoint": "/api/v1/auth/login",
      "register_endpoint": "/api/v1/auth/register",
      "supports_registration": true,
      "oauth": null
    }
    ```

    **Cloud Mode Response:**
    ```json
    {
      "auth_mode": "oauth",
      "login_endpoint": null,
      "register_endpoint": null,
      "supports_registration": false,
      "oauth": {
        "authorize_url": "/auth/oauth/authorize",
        "token_url": "/auth/oauth/token",
        "client_id": "faultmaven-copilot",
        "scopes": ["openid", "profile", "email", "cases:read", "cases:write"]
      }
    }
    ```
    """
    settings = get_settings()
    auth_settings = settings.auth

    if auth_settings.auth_mode == AuthMode.LOCAL:
        return AuthConfigResponse(
            auth_mode="local",
            login_endpoint="/api/v1/auth/login",
            register_endpoint="/api/v1/auth/register",
            supports_registration=True,
            oauth=None,
        )
    else:
        # Cloud mode (OAuth)
        return AuthConfigResponse(
            auth_mode="oauth",
            login_endpoint=None,
            register_endpoint=None,
            supports_registration=False,
            oauth=OAuthConfigResponse(
                authorize_url="/auth/oauth/authorize",
                token_url="/auth/oauth/token",
                client_id="faultmaven-copilot",
                scopes=["openid", "profile", "email", "cases:read", "cases:write"],
            ),
        )


# =============================================================================
# Local Mode Authentication Endpoints (AUTH_MODE=local only)
# =============================================================================


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    status_code=200,
    dependencies=[Depends(require_local_mode)],
)
@router.post(
    "/dev-login",
    response_model=AuthTokenResponse,
    status_code=200,
    deprecated=True,
    description="Deprecated: Use /login instead",
    dependencies=[Depends(require_local_mode)],
)
@trace("auth_login")
async def local_login(
    request_body: DevLoginRequest,
    request: Request,
    response: Response,
    session_service: AuthSessionService = Depends(get_session_service),
) -> AuthTokenResponse:
    """Internal login implementation for local mode.

    Authenticates users and generates JWT tokens.

    **Important:** Users must be created before login. Use `./faultmaven.sh create-user`
    to create accounts.

    **Flow:**
    1. Validate username format
    2. Find existing user
    3. If user doesn't exist: Return 401 (user must be created first)
    4. Generate JWT access token
    5. Return token with user profile

    **Security:**
    - Users must exist before login (no auto-creation)
    - JWT tokens (not opaque tokens) for middleware uniformity
    - Input validation and sanitization
    - Proper OAuth2-compatible error responses
    """
    correlation_id = str(uuid.uuid4())

    try:
        # Get required services
        user_store = await get_user_store(request)
        token_manager = await get_token_manager(request)

        # Try to find existing user
        user = await user_store.get_user_by_username(request_body.username)

        if not user:
            # User doesn't exist - require explicit account creation
            logger.warning(
                f"Login attempt for non-existent user: {request_body.username}",
                extra={
                    "username": request_body.username,
                    "correlation_id": correlation_id,
                },
            )
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "authentication_failed",
                    "message": (
                        f"User '{request_body.username}' does not exist. "
                        "Please create an account first using './faultmaven.sh create-user' "
                        "or the /api/v1/auth/register endpoint."
                    ),
                    "username": request_body.username,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        logger.info(
            f"User login: {request_body.username} (user: {user.user_id})",
            extra={
                "user_id": user.user_id,
                "username": request_body.username,
                "correlation_id": correlation_id,
            },
        )

        # Generate JWT access token (HS256 for local mode)
        # Per iam-design.md: "Unified JWT Format: Both Local and Cloud modes use JWT tokens"
        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            HS256JWTTokenGenerator,
        )

        settings = get_settings()

        # Create HS256 JWT generator for local mode
        if not settings.security.jwt_secret_key:
            raise HTTPException(
                status_code=500,
                detail="JWT_SECRET_KEY not configured for local mode authentication",
            )

        jwt_generator = HS256JWTTokenGenerator(
            secret_key=settings.security.jwt_secret_key.get_secret_value(),
            revocation_store=token_manager,  # Use existing token_manager for revocation
            settings=settings.auth,
            issuer=settings.security.jwt_issuer,
            audience=settings.security.jwt_audience,
        )

        access_token = await jwt_generator.generate_access_token(user)

        # Create session for multi-turn conversations
        session = await session_service.create_session(
            user_id=user.user_id,
            metadata={
                "login_method": "dev_login",
                "username": user.username,
                "correlation_id": correlation_id,
            },
        )
        # Extract session_id from SessionContext tuple or object
        if isinstance(session, tuple):
            # If tuple is returned (SessionContext, bool), get the SessionContext
            session_context = session[0]
            session_id = getattr(session_context, "session_id", str(session_context))
        else:
            # If SessionContext is returned directly
            session_id = getattr(session, "session_id", str(session))

        # Build response
        user_profile = UserProfile(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            created_at=to_json_compatible(user.created_at),
            is_dev_user=user.is_dev_user,
            roles=user.roles if user.roles else ["admin"],  # Ensure roles are included
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,  # 24 hours in seconds
            session_id=session_id,
            user=user_profile,
        )

        # Set correlation ID in response headers
        response.headers["X-Correlation-Id"] = correlation_id

        logger.info(
            f"Login successful for user {user.user_id} (correlation: {correlation_id})"
        )
        return token_response

    except HTTPException:
        raise
    except ValueError as e:
        # Handle validation errors (e.g., invalid username format)
        logger.warning(
            f"Login validation error: {str(e)}",
            extra={"username": request_body.username, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": str(e),
                "username": request_body.username,
            },
        )
    except Exception as e:
        logger.error(
            f"Dev login failed: {type(e).__name__}: {str(e)}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Login failed due to an internal error. Please try again later.",
            },
        )


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=201,
    dependencies=[Depends(require_local_mode)],
)
@router.post(
    "/dev-register",
    response_model=AuthTokenResponse,
    status_code=201,
    deprecated=True,
    description="Deprecated: Use /register instead",
    dependencies=[Depends(require_local_mode)],
)
@trace("auth_register")
async def local_register(
    request_body: DevLoginRequest,
    request: Request,
    response: Response,
    session_service: AuthSessionService = Depends(get_session_service),
) -> AuthTokenResponse:
    """Local mode registration endpoint.

    Creates a new user account and generates a JWT token.
    Available only when AUTH_MODE=local.

    **Flow:**
    1. Validate username format
    2. Check if user already exists (returns 409 if exists)
    3. Create new user account
    4. Generate JWT access token
    5. Return token with user profile

    **Security:**
    - Prevents duplicate account creation
    - JWT tokens (not opaque tokens) for middleware uniformity
    - Input validation and sanitization
    - Auto-generates email and display name if not provided
    """
    correlation_id = str(uuid.uuid4())

    try:
        # Get required services
        user_store = await get_user_store(request)
        token_manager = await get_token_manager(request)

        # Check if user already exists
        existing_user = await user_store.get_user_by_username(request_body.username)
        if existing_user:
            logger.warning(
                f"Registration attempt for existing user: {request_body.username}"
            )
            raise HTTPException(
                status_code=409,
                detail=f"User with username '{request_body.username}' already exists. Please use login instead.",
            )

        # Create new user
        user = await user_store.create_user(
            username=request_body.username,
            email=request_body.email,
            display_name=request_body.display_name,
        )
        logger.info(
            f"User registration: {request_body.username} (new user: {user.user_id})"
        )

        # Generate JWT access token (HS256 for local mode)
        # Per iam-design.md: "Unified JWT Format: Both Local and Cloud modes use JWT tokens"
        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            HS256JWTTokenGenerator,
        )

        settings = get_settings()

        # Create HS256 JWT generator for local mode
        if not settings.security.jwt_secret_key:
            raise HTTPException(
                status_code=500,
                detail="JWT_SECRET_KEY not configured for local mode authentication",
            )

        jwt_generator = HS256JWTTokenGenerator(
            secret_key=settings.security.jwt_secret_key.get_secret_value(),
            revocation_store=token_manager,  # Use existing token_manager for revocation
            settings=settings.auth,
            issuer=settings.security.jwt_issuer,
            audience=settings.security.jwt_audience,
        )

        access_token = await jwt_generator.generate_access_token(user)

        # Create session for multi-turn conversations
        session = await session_service.create_session(
            user_id=user.user_id,
            metadata={
                "login_method": "dev_register",
                "username": user.username,
                "correlation_id": correlation_id,
            },
        )
        # Extract session_id from SessionContext tuple or object
        if isinstance(session, tuple):
            # If tuple is returned (SessionContext, bool), get the SessionContext
            session_context = session[0]
            session_id = getattr(session_context, "session_id", str(session_context))
        else:
            # If SessionContext is returned directly
            session_id = getattr(session, "session_id", str(session))

        # Build response
        user_profile = UserProfile(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            created_at=to_json_compatible(user.created_at),
            is_dev_user=user.is_dev_user,
            roles=user.roles if user.roles else ["admin"],  # Ensure roles are included
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=24 * 60 * 60,  # 24 hours in seconds
            session_id=session_id,
            user=user_profile,
        )

        # Set correlation ID in response headers
        response.headers["X-Correlation-Id"] = correlation_id

        logger.info(
            f"Registration successful for user {user.user_id} (correlation: {correlation_id})"
        )
        return token_response

    except HTTPException:
        raise
    except ValueError as e:
        # Handle validation errors (e.g., invalid username/email format)
        logger.warning(
            f"Registration validation error: {str(e)}",
            extra={"username": request_body.username, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": str(e),
                "username": request_body.username,
            },
        )
    except Exception as e:
        logger.error(
            f"Dev registration failed: {type(e).__name__}: {str(e)}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Registration failed due to an internal error. Please try again later.",
            },
        )


@router.get(
    "/dev-list-users",
    status_code=200,
    dependencies=[Depends(require_development_environment)],
)
@trace("auth_dev_list_users")
async def dev_list_users(
    request: Request,
) -> dict:
    """Development endpoint to list all users.

    Returns a list of all users in the system for development/debugging.
    This endpoint is only available in development environments.

    **Security**: Gated by require_development_environment dependency.
    """
    try:
        user_store = await get_user_store(request)

        # Get all users (up to 1000)
        users = await user_store.list_users(limit=1000)
        total_count = await user_store.count_users()

        # Convert to simple dict format
        users_list = []
        for user in users:
            users_list.append(
                {
                    "user_id": user.user_id,
                    "username": user.username,
                    "email": user.email,
                    "display_name": user.display_name,
                    "roles": user.roles if user.roles else [],
                    "is_active": user.is_active,
                    "created_at": (
                        user.created_at.isoformat()
                        if hasattr(user.created_at, "isoformat")
                        else str(user.created_at)
                    ),
                }
            )

        return {
            "users": users_list,
            "total": total_count,
        }

    except Exception as e:
        logger.error(
            f"Dev list users failed: {type(e).__name__}: {str(e)}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": str(e)},
        )


@router.delete(
    "/dev-delete-user/{username}",
    status_code=200,
    dependencies=[Depends(require_development_environment)],
)
@trace("auth_dev_delete_user")
async def dev_delete_user(
    username: str,
    request: Request,
) -> dict:
    """Development endpoint to delete a user by username.

    Deletes (soft delete) a user by username for development/debugging.
    This endpoint is only available in development environments.

    **Security**: Gated by require_development_environment dependency.
    """
    try:
        user_store = await get_user_store(request)

        # Find user by username
        user = await user_store.get_user_by_username(username)
        if not user:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"User '{username}' not found",
                },
            )

        # Delete the user
        success = await user_store.delete_user(user.user_id)

        if success:
            logger.info(f"Dev deleted user: {username} ({user.user_id})")
            return {
                "message": f"User '{username}' deleted successfully",
                "user_id": user.user_id,
            }
        else:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "delete_failed",
                    "message": f"Failed to delete user '{username}'",
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Dev delete user failed: {type(e).__name__}: {str(e)}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": str(e)},
        )


@router.post("/logout", response_model=LogoutResponse)
@trace("auth_logout")
async def logout(
    request: Request,
    current_user: DevUser = Depends(require_authentication),
    token: str = Depends(extract_bearer_token),
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
        token_manager = await get_token_manager(request)

        # Revoke the current token (raises exception on failure)
        await token_manager.revoke_token(token)

        logger.info(
            f"User logout: {current_user.user_id} (correlation: {correlation_id})"
        )
        return LogoutResponse(message="Logged out successfully", revoked_tokens=1)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Logout failed: {e} (correlation: {correlation_id})")
        raise HTTPException(status_code=500, detail=f"Logout failed: {str(e)}")


@router.get("/me", response_model=UserInfoResponse)
@trace("auth_get_current_user")
async def get_current_user_profile(
    request: Request,
    current_user: DevUser = Depends(require_authentication),
) -> UserInfoResponse:
    """Get current user profile

    Returns detailed information about the currently authenticated user,
    including profile data and token statistics.
    """
    correlation_id = str(uuid.uuid4())

    try:
        token_manager = await get_token_manager(request)

        # Get user's active tokens for statistics
        user_tokens = await token_manager.get_user_tokens(current_user.user_id)
        active_token_count = len([token for token in user_tokens if token.is_valid])

        # Build extended user profile
        user_info = UserInfoResponse(
            user_id=current_user.user_id,
            username=current_user.username,
            email=current_user.email,
            display_name=current_user.display_name,
            created_at=to_json_compatible(current_user.created_at),
            is_dev_user=current_user.is_dev_user,
            roles=(
                current_user.roles if current_user.roles else ["admin"]
            ),  # Ensure roles are included
            last_login=None,  # TODO: Implement last login tracking
            token_count=active_token_count,
        )

        logger.debug(
            f"User profile requested: {current_user.user_id} (correlation: {correlation_id})"
        )
        return user_info

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get user profile failed: {e} (correlation: {correlation_id})")
        raise HTTPException(
            status_code=500, detail=f"Could not retrieve user profile: {str(e)}"
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
        health_status["authentication"]["timestamp"] = to_json_compatible(
            datetime.now(UTC)
        )

        return health_status["authentication"]

    except Exception as e:
        logger.error(f"Auth health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": to_json_compatible(datetime.now(UTC)),
            "error": str(e),
        }


# Debug endpoint for development only
@router.post(
    "/dev/revoke-all-tokens",
    response_model=LogoutResponse,
    dependencies=[Depends(require_development_environment)],
)
@trace("auth_dev_revoke_all")
async def dev_revoke_all_user_tokens(
    request: Request,
    current_user: DevUser = Depends(require_authentication),
) -> LogoutResponse:
    """Development endpoint: Revoke all tokens for current user.

    This endpoint is only available in development environments.

    **Security**: Gated by require_development_environment dependency.
    """
    correlation_id = str(uuid.uuid4())

    try:
        token_manager = await get_token_manager(request)

        # Revoke all user tokens
        revoked_count = await token_manager.revoke_user_tokens(current_user.user_id)

        logger.info(
            f"Dev: Revoked all tokens for user {current_user.user_id}, count: {revoked_count} (correlation: {correlation_id})"
        )

        return LogoutResponse(
            message=f"Revoked all {revoked_count} tokens for user",
            revoked_tokens=revoked_count,
        )

    except Exception as e:
        logger.error(
            f"Dev token revocation failed: {e} (correlation: {correlation_id})"
        )
        raise HTTPException(
            status_code=500, detail=f"Token revocation failed: {str(e)}"
        )
