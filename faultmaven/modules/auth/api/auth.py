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
- Automatic token expiration (configurable via JWT_ACCESS_TOKEN_EXPIRY_MINUTES)
- Input validation and sanitization
- Structured error responses per RFC 6749
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import jwt as jwt_lib
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ValidationError

from faultmaven.api.v1.auth_dependencies import (
    check_auth_services_health,
    extract_bearer_token,
    get_token_revocation_store,
    get_user_store,
    require_authentication,
    require_platform_admin,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.config.settings import AuthMode, get_settings
from faultmaven.container import container
from faultmaven.exceptions import FaultMavenException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.auth.domain.models.api_auth import (
    AuthenticationRequiredError,
    AuthError,
    AuthTokenResponse,
    DevLoginRequest,
    LogoutResponse,
    RevokeUserTokensResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
    TokenValidationError,
    UserInfoResponse,
    UserProfile,
)
from faultmaven.modules.auth.domain.models.auth import DevUser, TokenStatus
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    capture_state_read_at,
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


def _build_local_jwt_generator(revocation_store):
    """Construct the HS256 JWT generator used for local-mode tokens.

    Shared by /login and /refresh so both mint and validate tokens with the
    same secret, issuer, audience, and revocation store — a refresh token must
    verify under exactly the configuration that issued it.

    Args:
        revocation_store: The deployment-wide revocation store from
            ``get_token_revocation_store`` (#767). The request-path check in
            AuthService reads the same instance, so tokens revoked through
            this generator are rejected on every API endpoint.
    """
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        SigningKeyUnavailableError,
        build_hs256_token_generator,
    )

    try:
        return build_hs256_token_generator(get_settings(), revocation_store)
    except SigningKeyUnavailableError as e:
        # The plumbing — secret, lifetimes, issuer, audience — is the generator
        # module's, so this path and the container wire identical generators
        # (#959). Only the protocol translation stays here, with a static
        # detail: the caught text goes to the log, never to the response.
        logger.error("Local JWT generator unavailable: %s", e)
        raise HTTPException(
            status_code=500,
            detail="JWT_SECRET_KEY not configured for local mode authentication",
        ) from e


# =============================================================================
# Auth Configuration Discovery (per iam-design.md)
# =============================================================================


class OAuthConfigResponse(BaseModel):
    """OAuth configuration for cloud mode."""

    authorize_url: str
    token_url: str
    client_id: str
    scopes: List[str]
    # Hosted SSO login entry point for HUMAN sign-in (ADR-015 D3). Distinct from
    # authorize_url, which is the copilot OAuth-PKCE machine flow and needs
    # client_id/redirect_uri/PKCE params. None until WorkOS is configured, so
    # the dashboard shows an honest "not configured" state instead of a broken
    # redirect.
    hosted_login_url: Optional[str] = None


class AuthConfigResponse(BaseModel):
    """Auth configuration discovery response.

    Allows frontend to determine which authentication flow to use
    based on deployment configuration.
    """

    auth_mode: str
    login_endpoint: Optional[str] = None
    register_endpoint: Optional[str] = None
    supports_registration: bool
    oauth: Optional[OAuthConfigResponse] = None


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
        "scopes": ["openid", "profile", "email", "cases:read", "cases:write"],
        "hosted_login_url": "/api/v1/auth/sso/login"
      }
    }
    ```

    `hosted_login_url` is the human sign-in entry point (hosted SSO login,
    ADR-015). It is null unless SSO is configured; `authorize_url` remains the
    copilot OAuth-PKCE machine flow.
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
                # Advertised only when the SSO router is actually mounted
                # (same sso_configured gate as main.py), and as a relative
                # path — the dashboard resolves it against its configured API
                # origin.
                hosted_login_url=(
                    "/api/v1/auth/sso/login" if auth_settings.sso_configured else None
                ),
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

    # #831: before the first read of any state the tokens derive from
    # (the user row).
    state_read_at = capture_state_read_at()

    try:
        # Get required services
        user_store = await get_user_store(request)
        revocation_store = await get_token_revocation_store(request)

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

        # Generate JWT tokens (HS256 for local mode)
        # Per iam-design.md: "Unified JWT Format: Both Local and Cloud modes use JWT tokens"
        settings = get_settings()
        jwt_generator = _build_local_jwt_generator(revocation_store)

        access_token = await jwt_generator.generate_access_token(
            user, state_read_at=state_read_at
        )
        # Refresh token lets the client mint a new access token via
        # POST /auth/refresh instead of being forced to re-login when the
        # short-lived access token expires.
        refresh_token = await jwt_generator.generate_refresh_token(
            user, state_read_at=state_read_at
        )

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
            roles=user.roles if user.roles else ["user"],  # Least privilege when absent
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.auth.jwt_access_token_expire_minutes * 60,
            refresh_token=refresh_token,
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
    except FaultMavenException:
        # Typed service exceptions (ValidationException, ConflictError,
        # NotFoundError, etc.) propagate to FastAPI's global handlers which
        # map them to 422/409/404. See api/exception_handlers.py. Without
        # this pass-through, the blanket `except Exception` below would
        # swallow them and re-wrap as 500.
        raise
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

    # #831: before the first store read — see local_login.
    state_read_at = capture_state_read_at()

    try:
        # Get required services
        user_store = await get_user_store(request)
        revocation_store = await get_token_revocation_store(request)

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

        # Generate JWT tokens (HS256 for local mode)
        # Per iam-design.md: "Unified JWT Format: Both Local and Cloud modes use JWT tokens"
        settings = get_settings()
        jwt_generator = _build_local_jwt_generator(revocation_store)

        access_token = await jwt_generator.generate_access_token(
            user, state_read_at=state_read_at
        )
        refresh_token = await jwt_generator.generate_refresh_token(
            user, state_read_at=state_read_at
        )

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
            roles=user.roles if user.roles else ["user"],  # Least privilege when absent
        )

        token_response = AuthTokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=settings.auth.jwt_access_token_expire_minutes * 60,
            refresh_token=refresh_token,
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
    except FaultMavenException:
        # Typed service exceptions (ValidationException, ConflictError,
        # NotFoundError) propagate to FastAPI's global handlers — see
        # dev_login above and api/exception_handlers.py for the dispatch.
        raise
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


@router.post(
    "/refresh",
    response_model=TokenRefreshResponse,
    status_code=200,
)
@trace("auth_refresh")
async def refresh_tokens(
    request_body: TokenRefreshRequest,
    request: Request,
    response: Response,
) -> TokenRefreshResponse:
    """Exchange a refresh token for a new access token (both auth modes).

    The short-lived access token cannot be extended (it is a stateless JWT),
    so an active client must mint a new one before expiry rather than being
    forced to re-login. Refresh tokens rotate: the presented token is revoked
    and a fresh one is returned alongside the new access token.

    Mode-aware (ADR-015 D6): local mode validates and mints with the HS256
    generator; oauth/cloud mode uses the RS256 generator that issued the
    tokens (dev-login-, OAuth-, and SSO-minted refresh tokens alike), with
    identical rotation and revocation semantics.

    **Flow:**
    1. Validate the refresh token (signature, expiry, type, revocation)
    2. Load the current user (rejects deactivated/deleted accounts)
    3. Mint a new access + refresh token pair
    4. Revoke the old refresh token (rotation)
    """
    correlation_id = str(uuid.uuid4())

    # #831: before the refresh-token validation, not just the user load —
    # the presented token's still-unrevoked status is itself state this
    # handler reads.
    state_read_at = capture_state_read_at()

    try:
        user_store = await get_user_store(request)
        settings = get_settings()
        if settings.auth.auth_mode == AuthMode.LOCAL:
            revocation_store = await get_token_revocation_store(request)
            jwt_generator = _build_local_jwt_generator(revocation_store)
        else:
            # Composition root attaches the RS256 generator (with its Redis
            # revocation store) under oauth mode; a refresh token must verify
            # under exactly the configuration that issued it.
            jwt_generator = getattr(request.app.state, "jwt_token_generator", None)
            if jwt_generator is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "service_unavailable",
                        "message": "Token service unavailable. Please check server startup logs.",
                    },
                )

        # 1. Validate the presented refresh token (None => invalid/expired/revoked)
        claims = await jwt_generator.validate_refresh_token(request_body.refresh_token)
        if not claims:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "invalid_refresh_token",
                    "message": "Refresh token is invalid, expired, or revoked. Please log in again.",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 2. Load the current user — a token for a since-deleted/deactivated
        #    account must not be refreshable.
        user = await user_store.get_user(claims["sub"])
        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            account_may_hold_credentials,
        )

        if not user or not account_may_hold_credentials(user):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "user_unavailable",
                    "message": "User account no longer exists or is inactive. Please log in again.",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 2b. Re-attach the validated refresh token's organization claim before
        #     minting (#869). The user store's model has no organization column
        #     — under multi-tenant it is the token chain that carries tenancy,
        #     so without this the first refresh after an SSO login would mint an
        #     org-less pair and every subsequent request would fail closed at
        #     bind_request_org_context. Written with setattr because the store
        #     may return either the repository model or a DevUser dataclass
        #     (whose __post_init__ stamps the Standalone sentinel); under
        #     single-tenant resolve_organization_claim restores the sentinel
        #     anyway, so this is a no-op there.
        setattr(user, "organization_id", claims.get("organization_id") or None)

        # 3. Mint a fresh pair.
        new_access_token = await jwt_generator.generate_access_token(
            user, state_read_at=state_read_at
        )
        new_refresh_token = await jwt_generator.generate_refresh_token(
            user, state_read_at=state_read_at
        )

        # 4. Rotation: revoke the old refresh token so a leaked/replayed token
        #    cannot be reused after a successful refresh.
        await jwt_generator.revoke_refresh_token(request_body.refresh_token)

        response.headers["X-Correlation-Id"] = correlation_id
        # Token response: no-store per RFC 6749 §5.1 (same convention as the
        # SSO exchange endpoint — the body carries fresh credentials).
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        logger.info(
            f"Token refreshed for user {user.user_id} (correlation: {correlation_id})"
        )
        return TokenRefreshResponse(
            access_token=new_access_token,
            token_type="bearer",
            expires_in=settings.auth.jwt_access_token_expire_minutes * 60,
            refresh_token=new_refresh_token,
        )

    except HTTPException:
        raise
    except FaultMavenException:
        # Same pass-through login and register already have. Without it the
        # blanket handler below re-wraps typed service exceptions as 500 —
        # including InactiveAccountError, whose whole point is that a caller
        # which does not translate it still answers 403 via the global
        # handlers. Latent today (step 2 refuses a deactivated account before
        # step 3 mints); the asymmetry is the hazard, since the next reordering
        # here would surface as an opaque 500.
        raise
    except Exception as e:
        logger.error(
            f"Token refresh failed: {type(e).__name__}: {str(e)}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Token refresh failed due to an internal error. Please try again later.",
            },
        )


@router.get("/users", status_code=200)
@trace("auth_list_users")
async def list_users(
    request: Request,
    _: DevUser = Depends(require_platform_admin),
) -> dict:
    """List all users. Admin only."""
    try:
        user_store = await get_user_store(request)
        users = await user_store.list_users(limit=1000)
        total_count = await user_store.count_users()

        users_list = [
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
            for user in users
        ]

        # The listing is capped at 1000 with no pagination parameters, while
        # `total` counts every user. Say so rather than letting a caller read
        # a short list as the whole population.
        return {
            "users": users_list,
            "total": total_count,
            "truncated": len(users_list) < total_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"List users failed: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "Failed to list users"},
        )


@router.delete("/users/{username}", status_code=200)
@trace("auth_delete_user")
async def delete_user(
    username: str,
    request: Request,
    _: DevUser = Depends(require_platform_admin),
) -> dict:
    """Delete a user by username. Admin only."""
    try:
        user_store = await get_user_store(request)

        user = await user_store.get_user_by_username(username)
        if not user:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"User '{username}' not found",
                },
            )

        # Revoke BEFORE deleting, and refuse to delete if revocation fails.
        #
        # Deletion removes the account but not the credentials already issued
        # from it: refresh stops working (the user is gone), while outstanding
        # ACCESS tokens keep authenticating for the rest of their TTL because
        # the request path never reloads the user. The weaker operation —
        # deactivation — already writes a revocation watermark, so deleting a
        # compromised account left it usable longer than merely disabling it.
        #
        # Order matters: revoking first means a failure leaves the account
        # intact and the operator able to retry. Deleting first and then failing
        # to revoke would strand live tokens for an account that can no longer
        # be looked up to revoke.
        auth_service = getattr(request.app.state, "auth_service", None)
        if auth_service is None:
            raise HTTPException(
                status_code=503,
                detail="Authentication service unavailable; refusing to delete "
                "a user without revoking their outstanding tokens.",
            )
        # Raises ServiceError on store failure, which surfaces as a 500 below —
        # deliberately, so the delete does not proceed.
        await auth_service.revoke_user_tokens(user.user_id)

        success = await user_store.delete_user(user.user_id)

        if success:
            logger.info(f"Deleted user: {username} ({user.user_id})")
            return {
                "message": f"User '{username}' deleted successfully",
                "user_id": user.user_id,
            }

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
        logger.error(f"Delete user failed: {type(e).__name__}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to delete user",
            },
        )


@router.post("/logout", response_model=LogoutResponse)
@trace("auth_logout")
async def logout(
    request: Request,
    current_user: DevUser = Depends(require_authentication),
    token: str = Depends(extract_bearer_token),
) -> LogoutResponse:
    """Logout current user

    Revokes the current access token in the deployment-wide revocation store
    (#767), so the request-path revocation check rejects it on every endpoint
    for the remainder of its lifetime. Works in both auth modes — the store
    is keyed by jti, independent of the signing algorithm.

    **Flow:**
    1. Validate current authentication
    2. Revoke the current token
    3. Return confirmation
    """
    correlation_id = str(uuid.uuid4())

    try:
        auth_service = getattr(request.app.state, "auth_service", None)
        if auth_service is None:
            raise HTTPException(
                status_code=503,
                detail="Authentication service unavailable. Please check server startup logs.",
            )

        # The bearer token was already verified by require_authentication;
        # decode without re-verifying just to read jti + exp for revocation.
        claims = jwt_lib.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
        jti = claims.get("jti")
        exp = claims.get("exp")
        if not jti:
            # Cannot revoke a token that carries no revocation handle. Our
            # generators always mint a jti, so this indicates a foreign token.
            logger.warning(
                f"Logout without jti claim: {current_user.user_id} "
                f"(correlation: {correlation_id})"
            )
            return LogoutResponse(
                message="Logged out (token carries no jti; nothing to revoke)",
                revoked_tokens=0,
            )

        # Raises ServiceError on store failure — logout must not claim
        # success while the token remains usable. AuthService writes the
        # same deployment-wide store the request-path check reads (#767).
        await auth_service.revoke_token(jti, int(exp) if exp else 0)

        logger.info(
            f"User logout: {current_user.user_id} (correlation: {correlation_id})"
        )
        return LogoutResponse(message="Logged out successfully", revoked_tokens=1)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Logout failed: {e} (correlation: {correlation_id})", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Logout failed")


@router.get("/me", response_model=UserInfoResponse)
@trace("auth_get_current_user")
async def get_current_user_profile(
    current_user: DevUser = Depends(require_authentication),
) -> UserInfoResponse:
    """Get current user profile

    Returns detailed information about the currently authenticated user.
    """
    correlation_id = str(uuid.uuid4())

    try:
        # Build extended user profile
        user_info = UserInfoResponse(
            user_id=current_user.user_id,
            username=current_user.username,
            email=current_user.email,
            display_name=current_user.display_name,
            created_at=to_json_compatible(current_user.created_at),
            is_dev_user=current_user.is_dev_user,
            roles=(
                current_user.roles if current_user.roles else ["user"]
            ),  # Ensure roles are included; least privilege when absent
            last_login=None,  # TODO: Implement last login tracking
        )

        logger.debug(
            f"User profile requested: {current_user.user_id} (correlation: {correlation_id})"
        )
        return user_info

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Get user profile failed: {e} (correlation: {correlation_id})",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Could not retrieve user profile")


class AvailableScopesResponse(BaseModel):
    """Knowledge-base scopes the calling user may publish to."""

    scopes: List[str]


@router.get("/me/available-scopes", response_model=AvailableScopesResponse)
@trace("auth_available_scopes")
async def get_available_scopes(
    request: Request,
    current_user: DevUser = Depends(require_authentication),
) -> AvailableScopesResponse:
    """Return the scopes the calling user can target for team collaboration.

    ``personal`` (author-only) is always available.

    ``team`` is reported only when the deployment is team-enabled
    (``team_service`` is wired — a Cloud collaboration feature, unwired in
    standalone) AND the caller actually belongs to at least one Team, since a
    user with no Team has nothing to target.

    ``global`` is the platform tier, so it is reported only to a
    ``platform_admin``: every route that publishes at global scope requires
    that role (``POST /knowledge/documents`` unconditionally; the conversion
    routes for ``scope == "global"``). Reporting it to everyone made the
    dashboard offer a target the backend then refused, which is the drift this
    endpoint exists to prevent — its whole point is to reflect the caller's
    real capability rather than a hardcoded assumption.

    Scopes are returned narrowest-to-widest: ``personal | team | global``.
    """
    scopes = ["personal"]

    team_service = getattr(request.app.state, "team_service", None)
    if team_service:
        team_ids = await team_service.list_all_user_team_ids(current_user.user_id)
        if team_ids:
            scopes.append("team")

    if current_user.is_platform_admin():
        scopes.append("global")

    return AvailableScopesResponse(scopes=scopes)


@router.get(
    "/health",
    responses={
        503: {
            "description": (
                "The health check itself failed. The body keeps the same shape "
                "as a successful report — `status` plus a static `error` — so a "
                "caller reading the body is unaffected by the status code."
            )
        }
    },
)
@trace("auth_health_check")
async def auth_health_check(request: Request):
    """Authentication system health check

    Returns the status of authentication services including token management
    and user storage systems.
    """
    # `request` is what the probe reads: check_auth_services_health resolves the
    # revocation store and user store off `request.app.state` (39807ebe moved
    # them there when the Service Locator went away) and cannot be called
    # without it. Kept out of the docstring deliberately — FastAPI publishes
    # that verbatim as this operation's public OpenAPI description.
    try:
        # Use clean health check dependency
        health_status = await check_auth_services_health(request)

        # Add timestamp
        health_status["authentication"]["timestamp"] = to_json_compatible(
            datetime.now(timezone.utc)
        )

        return health_status["authentication"]

    except Exception as e:
        logger.error(f"Auth health check failed: {e}", exc_info=True)
        # 503, not 200. This arm reports that the probe itself failed, and a
        # 200 makes that indistinguishable from a healthy service to anything
        # gating on the status code — which is how a TypeError here went
        # unnoticed for seven months. The body is unchanged, so a caller that
        # reads `status`/`error` rather than the code is unaffected.
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "error": "Auth health check failed",
            },
        )


@router.post("/users/{user_id}/revoke-tokens", response_model=RevokeUserTokensResponse)
@trace("auth_revoke_user_tokens")
async def revoke_user_tokens(
    user_id: str,
    request: Request,
    _: DevUser = Depends(require_platform_admin),
    user_store=Depends(get_user_store),
) -> RevokeUserTokensResponse:
    """Revoke all tokens for a user. Platform admin only.

    Writes a per-user revocation watermark to the shared revocation store; the
    request path and both token generators then reject every token for this
    user issued at or before that instant (#769). A store write failure is a
    500 — an admin must never get a revocation confirmation while the user's
    tokens keep authenticating.

    Unknown ``user_id`` is a 404 rather than a vacuous success. The watermark
    write would succeed for any string, so an admin who pastes a username (or
    mistypes an id) while containing a compromised account would otherwise get
    a "revoked" confirmation while the real account kept authenticating —
    the same false-confirmation failure this endpoint was fixed for.

    The revocation happens BEFORE that lookup and is never conditional on it.
    ``DatabaseUserStore.get_user`` swallows its exceptions and returns None, so
    an auth-DB outage is indistinguishable from a genuinely absent user (see
    #703, where exactly that DB froze). Gating on it would let a DB blip answer
    "user not found" to an admin containing a live compromise, having revoked
    nothing. Revocation only needs Redis, so it runs on Redis alone; the lookup
    only shapes the response. A watermark written for an id that turns out not
    to exist is inert and expires on its own.
    """
    try:
        auth_service = getattr(request.app.state, "auth_service", None)
        if auth_service is None:
            raise HTTPException(
                status_code=503,
                detail="Authentication service unavailable. Please check server startup logs.",
            )

        # Raises ServiceError on store failure, which surfaces as a 500 below.
        revoked_before = await auth_service.revoke_user_tokens(user_id)

        if await user_store.get_user(user_id) is None:
            logger.warning(
                "Revoke-tokens called for an unresolvable user_id; watermark "
                "written anyway (the lookup cannot distinguish absent from "
                "store failure)",
                extra={"user_id": user_id},
            )
            # Say that the revocation landed. A bare "user not found" would be
            # actively misleading when the cause is a lookup failure rather
            # than a bad id: the admin would assume nothing happened and that
            # they still need to act, which is the false-signal problem this
            # endpoint was fixed for, inverted.
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Revocation recorded for '{user_id}', but no such user "
                    "could be resolved — verify the user ID (or, if the user "
                    "does exist, the user store is failing)."
                ),
            )

        logger.info(
            "Revoked all tokens for user",
            extra={"user_id": user_id, "revoked_before": revoked_before.isoformat()},
        )
        return RevokeUserTokensResponse(
            message="All tokens revoked for user",
            revoked_before=revoked_before.isoformat(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token revocation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Token revocation failed")
