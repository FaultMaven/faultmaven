"""Authentication API Routes (TASK-017)

Purpose: FastAPI routes for JWT authentication operations.

Endpoints:
- POST /api/v1/auth/login     - Authenticate and get tokens
- POST /api/v1/auth/refresh   - Refresh access token
- POST /api/v1/auth/logout    - Revoke tokens
- POST /api/v1/auth/verify    - Verify token validity

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from faultmaven.api.middleware.auth import (
    get_auth_service,
    get_current_user,
    get_current_user_optional,
)
from faultmaven.models.auth import AuthenticatedUser, TokenPair
from faultmaven.models.rbac import get_permissions_for_roles, Role
from faultmaven.services.auth_service import (
    AuthenticationError,
    AuthService,
    TokenRevocationError,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


# ============================================================
# Request/Response Models
# ============================================================


class LoginRequest(BaseModel):
    """Login request with credentials."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=1, description="User password")


class TokenResponse(BaseModel):
    """Token response returned after login or refresh."""

    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field(default="Bearer", description="Token type")
    expires_in: int = Field(..., description="Access token expiration in seconds")


class RefreshTokenRequest(BaseModel):
    """Refresh token request."""

    refresh_token: str = Field(..., description="Valid refresh token")


class LogoutRequest(BaseModel):
    """Logout request with optional refresh token."""

    refresh_token: Optional[str] = Field(
        None, description="Refresh token to revoke (optional)"
    )


class TokenVerifyRequest(BaseModel):
    """Token verification request."""

    token: str = Field(..., description="Token to verify")


class TokenVerifyResponse(BaseModel):
    """Token verification response."""

    valid: bool = Field(..., description="Whether token is valid")
    user_id: Optional[str] = Field(None, description="User ID from token")
    organization_id: Optional[str] = Field(None, description="Organization ID from token")
    email: Optional[str] = Field(None, description="User email from token")
    roles: Optional[List[str]] = Field(None, description="User roles")
    permissions: Optional[List[str]] = Field(None, description="User permissions")
    expires_at: Optional[str] = Field(None, description="Token expiration time (ISO 8601)")
    error: Optional[str] = Field(None, description="Error message if invalid")


# ============================================================
# Development User Lookup (Replace with UserService in TASK-018)
# ============================================================


async def _dev_validate_credentials(
    email: str, password: str
) -> Optional[Dict[str, Any]]:
    """Development-only credential validation.

    TODO: Replace with UserService.authenticate() in TASK-018

    For development, accepts any password for known test users.

    Args:
        email: User email
        password: User password

    Returns:
        User dict if valid, None otherwise
    """
    # Development test users
    dev_users = {
        "admin@faultmaven.local": {
            "user_id": "dev-admin-001",
            "email": "admin@faultmaven.local",
            "organization_id": "org-dev-001",
            "roles": ["admin"],
        },
        "member@faultmaven.local": {
            "user_id": "dev-member-001",
            "email": "member@faultmaven.local",
            "organization_id": "org-dev-001",
            "roles": ["member"],
        },
        "viewer@faultmaven.local": {
            "user_id": "dev-viewer-001",
            "email": "viewer@faultmaven.local",
            "organization_id": "org-dev-001",
            "roles": ["viewer"],
        },
    }

    user = dev_users.get(email)
    if user and password:  # Accept any non-empty password in dev
        return user

    return None


async def _dev_load_user(user_id: str) -> Optional[tuple]:
    """Development-only user loader for token refresh.

    TODO: Replace with UserService.get_user() in TASK-018

    Args:
        user_id: User ID to load

    Returns:
        Tuple of (email, roles, permissions) or None
    """
    # Development test users by ID
    dev_users = {
        "dev-admin-001": ("admin@faultmaven.local", ["admin"], None),
        "dev-member-001": ("member@faultmaven.local", ["member"], None),
        "dev-viewer-001": ("viewer@faultmaven.local", ["viewer"], None),
    }

    return dev_users.get(user_id)


# ============================================================
# Authentication Endpoints
# ============================================================


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Authenticate user and return JWT tokens.

    Validates credentials and returns access and refresh tokens.

    Request Body:
        email: User email address
        password: User password

    Returns:
        TokenResponse with access_token, refresh_token, token_type, expires_in

    Raises:
        401: Invalid credentials
        422: Validation error

    Example:
        POST /api/v1/auth/login
        {
            "email": "admin@faultmaven.local",
            "password": "password123"
        }

        Response:
        {
            "access_token": "eyJhbGc...",
            "refresh_token": "eyJhbGc...",
            "token_type": "Bearer",
            "expires_in": 900
        }
    """
    # Validate credentials
    user = await _dev_validate_credentials(credentials.email, credentials.password)

    if not user:
        logger.info(f"Failed login attempt for: {credentials.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Generate tokens
    token_pair = auth_service.generate_token_pair(
        user_id=user["user_id"],
        organization_id=user["organization_id"],
        email=user["email"],
        roles=user["roles"],
    )

    logger.info(f"Successful login: {credentials.email}")

    return TokenResponse(
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        token_type=token_pair.token_type,
        expires_in=token_pair.expires_in,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Exchange refresh token for new access token.

    Performs token rotation: old refresh token is revoked.

    Request Body:
        refresh_token: Valid refresh token

    Returns:
        TokenResponse with new access_token and refresh_token

    Raises:
        401: Invalid or expired refresh token
        403: Refresh token has been revoked

    Example:
        POST /api/v1/auth/refresh
        {
            "refresh_token": "eyJhbGc..."
        }
    """
    try:
        new_access, new_refresh = await auth_service.refresh_access_token(
            refresh_token=request.refresh_token,
            user_loader=_dev_load_user,
        )

        return TokenResponse(
            access_token=new_access,
            refresh_token=new_refresh,
            token_type="Bearer",
            expires_in=auth_service._access_token_expire_minutes * 60,
        )

    except AuthenticationError as e:
        logger.debug(f"Refresh failed: {e.message}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid refresh token: {e.message}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except TokenRevocationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Refresh token has been revoked. Please re-authenticate.",
        )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: LogoutRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Revoke user tokens (logout).

    Revokes the access token from the Authorization header.
    Optionally revokes the refresh token if provided.

    Headers:
        Authorization: Bearer <access_token>

    Request Body:
        refresh_token: Optional refresh token to revoke

    Returns:
        204 No Content on success

    Example:
        POST /api/v1/auth/logout
        Headers: Authorization: Bearer eyJhbGc...
        Body: {"refresh_token": "eyJhbGc..."}
    """
    # Revoke access token
    if current_user.token_jti:
        try:
            # Get expiration from token claims
            if authorization and authorization.startswith("Bearer "):
                token = authorization[7:]
                claims = auth_service.verify_token(token, token_type="access")
                exp = claims.get("exp", 0)
                await auth_service.revoke_token(current_user.token_jti, exp)
                logger.info(f"Access token revoked for user: {current_user.user_id}")
        except Exception as e:
            logger.warning(f"Failed to revoke access token: {e}")

    # Revoke refresh token if provided
    if request.refresh_token:
        try:
            claims = auth_service.verify_token(
                request.refresh_token, token_type="refresh"
            )
            jti = claims.get("jti")
            exp = claims.get("exp", 0)
            if jti:
                await auth_service.revoke_token(jti, exp)
                logger.info(f"Refresh token revoked for user: {current_user.user_id}")
        except AuthenticationError:
            # Invalid refresh token, ignore (maybe already expired)
            pass
        except Exception as e:
            logger.warning(f"Failed to revoke refresh token: {e}")


@router.post("/verify", response_model=TokenVerifyResponse)
async def verify_token(
    request: TokenVerifyRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenVerifyResponse:
    """Verify token validity (introspection endpoint).

    Checks if a token is valid and returns its claims.

    Request Body:
        token: Token to verify

    Returns:
        TokenVerifyResponse with validity status and token claims

    Example:
        POST /api/v1/auth/verify
        {
            "token": "eyJhbGc..."
        }

        Response (valid):
        {
            "valid": true,
            "user_id": "user-123",
            "organization_id": "org-456",
            "roles": ["admin"],
            "expires_at": "2025-12-30T10:00:00Z"
        }

        Response (invalid):
        {
            "valid": false,
            "error": "Token has expired"
        }
    """
    try:
        # Try to verify as access token first, then refresh token
        try:
            claims = await auth_service.verify_token_with_revocation_check(
                request.token, token_type="access"
            )
            token_type = "access"
        except AuthenticationError:
            claims = await auth_service.verify_token_with_revocation_check(
                request.token, token_type="refresh"
            )
            token_type = "refresh"

        # Format expiration time
        exp_timestamp = claims.get("exp", 0)
        expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc).isoformat()

        return TokenVerifyResponse(
            valid=True,
            user_id=claims.get("sub"),
            organization_id=claims.get("org_id"),
            email=claims.get("email"),
            roles=claims.get("roles", []),
            permissions=claims.get("permissions", []),
            expires_at=expires_at,
        )

    except AuthenticationError as e:
        return TokenVerifyResponse(
            valid=False,
            error=e.message,
        )

    except TokenRevocationError:
        return TokenVerifyResponse(
            valid=False,
            error="Token has been revoked",
        )


@router.get("/me", response_model=Dict[str, Any])
async def get_current_user_info(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Get current authenticated user info.

    Returns the authenticated user's information from the JWT token.

    Headers:
        Authorization: Bearer <access_token>

    Returns:
        User information including user_id, organization_id, email, roles, permissions

    Example:
        GET /api/v1/auth/me
        Headers: Authorization: Bearer eyJhbGc...

        Response:
        {
            "user_id": "user-123",
            "organization_id": "org-456",
            "email": "user@example.com",
            "roles": ["admin"],
            "permissions": ["cases:read", "cases:write", ...]
        }
    """
    return current_user.to_dict()
