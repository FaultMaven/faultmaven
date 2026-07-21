"""OAuth 2.0 Authentication Routes.

This module implements OAuth 2.0 Authorization Code Flow with PKCE for
Dashboard-centric authentication where the Dashboard acts as IdP for
the Browser Extension.

Key Endpoints:
- GET /auth/oauth/authorize: Authorization code generation
- POST /auth/oauth/token: Token exchange and refresh
- POST /auth/oauth/revoke: Token revocation

OAuth Flow:
1. Extension redirects user to Dashboard authorization page
2. User authenticates in Dashboard (dev-login or existing session)
3. Dashboard generates authorization code with PKCE challenge
4. Extension exchanges code + verifier for access/refresh tokens
5. Extension uses access token for API requests
6. Extension silently refreshes tokens before expiry

Security:
- PKCE (SHA256) prevents authorization code interception
- Single-use authorization codes (10 minute expiry)
- Short-lived access tokens (1 hour)
- Long-lived refresh tokens (7 days) with rotation
- Constant-time comparison for PKCE verification
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.models.exceptions import InvalidGrantError, InvalidRequestError
from faultmaven.modules.auth.api.rate_limiting import (
    require_oauth_rate_limit_authorize,
    require_oauth_rate_limit_revoke,
    require_oauth_rate_limit_token,
)
from faultmaven.modules.auth.contracts import (
    IOAuthService,
    OAuthAuthorizationDTO,
    OAuthTokenDTO,
)
from faultmaven.modules.auth.domain.models.auth import DevUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


# ============================================================
# Request/Response Models
# ============================================================


class AuthorizationRequest(BaseModel):
    """OAuth authorization request parameters.

    Follows OAuth 2.0 Authorization Code Flow with PKCE (RFC 7636).
    """

    response_type: Literal["code"] = Field(
        description="Must be 'code' (authorization code flow)"
    )
    client_id: str = Field(description="OAuth client ID (e.g., 'faultmaven-copilot')")
    redirect_uri: str = Field(
        description="Extension callback URI (must match allowed patterns)"
    )
    state: str = Field(description="Client state for CSRF protection")
    code_challenge: str = Field(
        description="PKCE code challenge (SHA256 hash of verifier)"
    )
    code_challenge_method: Optional[Literal["S256"]] = Field(
        default="S256", description="PKCE challenge method (only S256 supported)"
    )
    scope: Optional[str] = Field(
        default="openid profile email", description="OAuth scopes requested"
    )


class AuthorizationConsentRequest(BaseModel):
    """Authorization consent information for Dashboard UI.

    Returned when user needs to approve/deny the authorization request.
    """

    client_id: str = Field(description="OAuth client ID requesting access")
    client_name: str = Field(description="Human-readable client name")
    redirect_uri: str = Field(description="Callback URI after authorization")
    scope: str = Field(description="Requested scopes (space-separated)")
    state: str = Field(description="Client state (for CSRF protection)")
    user_id: str = Field(description="Authenticated user ID")
    username: str = Field(description="Authenticated username")


class AuthorizationApprovalRequest(BaseModel):
    """User's approval/denial of authorization request (POST body).

    Submitted by Dashboard after user reviews consent screen.
    """

    approved: bool = Field(description="True if user approved, False if denied")
    code_challenge: str = Field(description="PKCE code challenge")
    code_challenge_method: Optional[Literal["S256"]] = Field(default="S256")
    client_id: str = Field(description="OAuth client ID")
    redirect_uri: str = Field(description="Callback URI")
    scope: str = Field(description="Requested scopes")
    state: str = Field(description="Client state")


class AuthorizationResponse(BaseModel):
    """OAuth authorization response (redirect parameters).

    Extension receives this as query parameters after redirect.
    """

    code: str = Field(description="Authorization code (10 minute expiry)")
    state: str = Field(description="Client state (echoed back for verification)")


class TokenRequest(BaseModel):
    """OAuth token request (authorization_code or refresh_token grant).

    Two grant types supported:
    1. authorization_code: Exchange code for tokens
    2. refresh_token: Refresh access token using refresh token
    """

    grant_type: Literal["authorization_code", "refresh_token"] = Field(
        description="Grant type: 'authorization_code' or 'refresh_token'"
    )

    # For authorization_code grant
    code: Optional[str] = Field(
        default=None,
        description="Authorization code (required for authorization_code grant)",
    )
    redirect_uri: Optional[str] = Field(
        default=None,
        description="Redirect URI (required for authorization_code grant, must match)",
    )
    code_verifier: Optional[str] = Field(
        default=None,
        description="PKCE code verifier (required for authorization_code grant)",
    )

    # For refresh_token grant
    refresh_token: Optional[str] = Field(
        default=None, description="Refresh token (required for refresh_token grant)"
    )

    # Common parameters
    client_id: str = Field(description="OAuth client ID")


class TokenResponse(BaseModel):
    """OAuth token response (access token + refresh token).

    Returned for both authorization_code and refresh_token grants.
    """

    access_token: str = Field(description="JWT access token (1 hour expiry)")
    refresh_token: str = Field(description="JWT refresh token (7 days expiry)")
    token_type: str = Field(default="Bearer", description="Token type (always Bearer)")
    expires_in: int = Field(description="Access token expiry in seconds (3600)")
    refresh_expires_in: int = Field(
        description="Refresh token expiry in seconds (604800)"
    )
    user_id: str = Field(description="User ID")
    username: str = Field(description="Username")


class RevokeRequest(BaseModel):
    """OAuth token revocation request.

    Supports revoking both access tokens and refresh tokens.
    """

    token: str = Field(description="Token to revoke (access or refresh)")
    token_type_hint: Optional[Literal["access_token", "refresh_token"]] = Field(
        default=None, description="Hint about token type (optional)"
    )
    client_id: str = Field(description="OAuth client ID")


# ============================================================
# Dependency Injection
# ============================================================


async def get_oauth_service(request: Request) -> IOAuthService:
    """Get OAuth service from app.state (Composition Root pattern).

    This dependency accesses the OAuth service via request.app.state,
    following the Composition Root principle (no Service Locator pattern).

    Returns:
        OAuth service instance

    Raises:
        HTTPException: If OAuth service not configured
    """
    # Use dependency injection from app.state (Composition Root)
    from faultmaven.api.v1.dependencies import get_oauth_service as get_oauth_dep

    oauth_service = await get_oauth_dep(request)
    if oauth_service is None:
        raise HTTPException(
            status_code=503, detail="OAuth authentication not configured"
        )
    return oauth_service


# ============================================================
# OAuth 2.0 Endpoints
# ============================================================


@router.get(
    "/authorize",
    dependencies=[Depends(require_oauth_rate_limit_authorize)],
)
async def get_authorization_request(
    response_type: str = Query(..., description="Must be 'code'"),
    client_id: str = Query(..., description="OAuth client ID"),
    redirect_uri: str = Query(..., description="Extension callback URI"),
    state: str = Query(..., description="Client state for CSRF protection"),
    code_challenge: str = Query(..., description="PKCE code challenge"),
    code_challenge_method: str = Query(default="S256", description="PKCE method"),
    scope: str = Query(default="openid profile email", description="OAuth scopes"),
    user: DevUser = Depends(require_authentication),
    oauth_service: IOAuthService = Depends(get_oauth_service),
    request: Request = None,
):
    """OAuth 2.0 Authorization Endpoint (GET) - Consent Request.

    Returns authorization request details for user consent screen.
    If oauth_require_consent=false, auto-approves and returns code immediately.

    Production Flow (oauth_require_consent=true):
    1. User authenticates in Dashboard (dev-login or existing session)
    2. Extension redirects to GET /authorize with PKCE challenge
    3. Dashboard displays consent screen with AuthorizationConsentRequest
    4. User approves/denies → Dashboard POSTs to /authorize
    5. Backend generates code and redirects to Extension

    Dev/Test Flow (oauth_require_consent=false):
    1-2. Same as production
    3. Backend auto-approves and returns AuthorizationResponse immediately
    4. Dashboard redirects to Extension with code

    Args:
        response_type: Must be "code" (authorization code flow)
        client_id: OAuth client ID (validated against allowed clients)
        redirect_uri: Extension callback URI (validated against allowed patterns)
        state: Client state for CSRF protection (echoed back)
        code_challenge: PKCE code challenge (SHA256 of code_verifier)
        code_challenge_method: PKCE method (only "S256" supported)
        scope: OAuth scopes requested
        user: Authenticated user from Dashboard session
        oauth_service: OAuth service dependency
        request: FastAPI request object (for accessing settings)

    Returns:
        - AuthorizationConsentRequest (if consent required) - for Dashboard UI
        - AuthorizationResponse (if auto-approve) - immediate authorization code

    Raises:
        HTTPException: 400 if request invalid, 401 if user not authenticated
    """
    try:
        # Validate response_type
        if response_type != "code":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported response_type: {response_type}. Only 'code' supported.",
            )

        # Get settings to check if consent is required
        from faultmaven.config.settings import FaultMavenSettings

        settings = FaultMavenSettings()

        # If consent required, return consent request for Dashboard UI
        if settings.auth.oauth_require_consent:
            logger.info(
                f"Authorization consent required for user {user.user_id}, client {client_id}"
            )

            # Map client_id to human-readable name
            client_names = {
                "faultmaven-copilot": "FaultMaven Copilot Browser Extension",
                "faultmaven-cli": "FaultMaven CLI Tool",
            }
            client_name = client_names.get(client_id, client_id)

            return AuthorizationConsentRequest(
                client_id=client_id,
                client_name=client_name,
                redirect_uri=redirect_uri,
                scope=scope,
                state=state,
                user_id=user.user_id,
                username=user.username,
            )

        # Auto-approve mode (dev/test only)
        logger.warning(
            f"AUTO-APPROVE enabled for user {user.user_id}, client {client_id} (dev/test only)"
        )

        # Create authorization request DTO
        auth_request = OAuthAuthorizationDTO(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
        )

        # Generate authorization code using authenticated user's ID
        code = await oauth_service.create_authorization_code(user.user_id, auth_request)

        logger.info(
            f"Generated authorization code (auto-approved) for user {user.user_id}, client {client_id}"
        )

        return AuthorizationResponse(code=code, state=state)

    except InvalidRequestError as e:
        logger.warning(f"Invalid authorization request: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # Re-raise HTTPException (don't convert to 500)
        raise
    except Exception as e:
        logger.error(f"Authorization error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/authorize",
    response_model=AuthorizationResponse,
    dependencies=[Depends(require_oauth_rate_limit_authorize)],
)
async def post_authorization_approval(
    approval: AuthorizationApprovalRequest = Body(...),
    user: DevUser = Depends(require_authentication),
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> AuthorizationResponse:
    """OAuth 2.0 Authorization Endpoint (POST) - User Approval.

    Handles user's approval/denial of authorization request after consent screen.
    Generates authorization code if approved, returns error if denied.

    Flow:
    1. User reviews consent screen (GET /authorize returned AuthorizationConsentRequest)
    2. Dashboard submits user's decision (approved/denied) to this endpoint
    3. If approved: Generate authorization code and return for redirect
    4. If denied: Return error

    Args:
        approval: User's approval decision and authorization parameters
        user: Authenticated user from Dashboard session
        oauth_service: OAuth service dependency

    Returns:
        AuthorizationResponse with authorization code and state

    Raises:
        HTTPException: 400 if denied or invalid, 401 if user not authenticated
    """
    try:
        # Check if user denied the request
        if not approval.approved:
            logger.info(
                f"User {user.user_id} denied authorization for client {approval.client_id}"
            )
            raise HTTPException(
                status_code=400, detail="User denied authorization request"
            )

        # Create authorization request DTO
        auth_request = OAuthAuthorizationDTO(
            client_id=approval.client_id,
            redirect_uri=approval.redirect_uri,
            state=approval.state,
            code_challenge=approval.code_challenge,
            code_challenge_method=approval.code_challenge_method,
            scope=approval.scope,
        )

        # Generate authorization code using authenticated user's ID
        code = await oauth_service.create_authorization_code(user.user_id, auth_request)

        logger.info(
            f"Generated authorization code (user approved) for user {user.user_id}, client {approval.client_id}"
        )

        return AuthorizationResponse(code=code, state=approval.state)

    except InvalidRequestError as e:
        logger.warning(f"Invalid authorization approval: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authorization approval error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/token",
    response_model=TokenResponse,
    dependencies=[Depends(require_oauth_rate_limit_token)],
)
async def token(
    token_request: TokenRequest = Body(...),
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> TokenResponse:
    """OAuth 2.0 Token Endpoint.

    Handles two grant types:
    1. authorization_code: Exchange authorization code for access/refresh tokens
    2. refresh_token: Refresh access token using refresh token

    Authorization Code Flow:
    1. Extension calls /authorize and gets authorization code
    2. Extension exchanges code + verifier for tokens
    3. Extension stores tokens securely
    4. Extension uses access token for API requests

    Refresh Token Flow:
    1. Extension detects access token expiring soon (< 5 minutes)
    2. Extension exchanges refresh token for new access token
    3. Extension gets new access token + rotated refresh token
    4. Extension updates stored tokens

    Args:
        token_request: Token request (authorization_code or refresh_token)
        oauth_service: OAuth service dependency

    Returns:
        Access token, refresh token, and user information

    Raises:
        HTTPException: 400 if request invalid, 401 if grant invalid
    """
    try:
        if token_request.grant_type == "authorization_code":
            # Validate required parameters
            if not token_request.code:
                raise HTTPException(
                    status_code=400, detail="Missing required parameter: code"
                )
            if not token_request.redirect_uri:
                raise HTTPException(
                    status_code=400, detail="Missing required parameter: redirect_uri"
                )
            if not token_request.code_verifier:
                raise HTTPException(
                    status_code=400, detail="Missing required parameter: code_verifier"
                )

            # Exchange authorization code for tokens
            token_dto = await oauth_service.exchange_code_for_token(
                code=token_request.code,
                code_verifier=token_request.code_verifier,
                redirect_uri=token_request.redirect_uri,
            )

            logger.info(
                f"Exchanged authorization code for tokens (user: {token_dto.user_id})"
            )

        elif token_request.grant_type == "refresh_token":
            # Validate required parameters
            if not token_request.refresh_token:
                raise HTTPException(
                    status_code=400, detail="Missing required parameter: refresh_token"
                )

            # Refresh access token
            token_dto = await oauth_service.refresh_access_token(
                refresh_token=token_request.refresh_token,
                client_id=token_request.client_id,
            )

            logger.info(f"Refreshed access token (user: {token_dto.user_id})")

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported grant_type: {token_request.grant_type}",
            )

        # Convert DTO to response model
        return TokenResponse(
            access_token=token_dto.access_token,
            refresh_token=token_dto.refresh_token,
            token_type=token_dto.token_type,
            expires_in=token_dto.expires_in,
            refresh_expires_in=token_dto.refresh_expires_in,
            user_id=token_dto.user_id,
            username=token_dto.username,
        )

    except InvalidGrantError as e:
        logger.warning(f"Invalid grant: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/revoke",
    status_code=200,
    dependencies=[Depends(require_oauth_rate_limit_revoke)],
)
async def revoke(
    revoke_request: RevokeRequest = Body(...),
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> dict:
    """OAuth 2.0 Token Revocation Endpoint.

    Revokes access tokens or refresh tokens (for logout).

    When to revoke:
    - User logs out: Revoke both access and refresh tokens
    - Security event: Revoke all tokens for user
    - Token rotation: Old refresh token revoked automatically

    Args:
        revoke_request: Token revocation request
        oauth_service: OAuth service dependency

    Returns:
        Success response (200 OK, no body per RFC 7009)

    Note: Returns 200 even if token doesn't exist (per RFC 7009)
    """
    try:
        # Determine token type
        token_type = revoke_request.token_type_hint or "access_token"

        if token_type == "refresh_token":
            await oauth_service.revoke_refresh_token(revoke_request.token)
            logger.info(f"Revoked refresh token for client {revoke_request.client_id}")
        else:
            await oauth_service.revoke_token(revoke_request.token)
            logger.info(f"Revoked access token for client {revoke_request.client_id}")

        # Per RFC 7009, return 200 OK even if token invalid/not found
        return {}

    except Exception as e:
        logger.error(f"Token revocation error: {e}")
        # Invalid/unknown tokens are already treated as success inside the
        # generator (RFC 7009), so an exception here means the revocation was
        # NOT recorded (e.g. store outage). Surface it (RFC 7009 §2.2.1
        # permits 503) so the client retries instead of believing the token
        # is dead.
        raise HTTPException(
            status_code=503,
            detail="Token revocation temporarily unavailable. Please retry.",
        )
