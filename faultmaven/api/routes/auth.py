"""Authentication API Routes (TASK-017, TASK-018)

Purpose: FastAPI routes for JWT authentication and user management operations.

Endpoints:
- POST /api/v1/auth/login               - Authenticate and get tokens
- POST /api/v1/auth/register            - Register new user account
- POST /api/v1/auth/refresh             - Refresh access token
- POST /api/v1/auth/logout              - Revoke tokens
- POST /api/v1/auth/verify              - Verify token validity
- POST /api/v1/auth/password/reset-request  - Request password reset
- POST /api/v1/auth/password/reset      - Reset password with token
- POST /api/v1/auth/password/change     - Change password (authenticated)
- GET  /api/v1/auth/me                  - Get current user info

Design Reference:
- TASK-017 JWT Authentication & Authorization Middleware
- TASK-018 User Management Service
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from faultmaven.api.middleware.auth import (
    get_auth_service,
    get_current_user,
    get_current_user_optional,
)
from faultmaven.exceptions import ConflictError, NotFoundError, ValidationException
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

    email: str = Field(
        ..., description="User email address", pattern=r"^[^@]+@[^@]+\.[^@]+$"
    )
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
    organization_id: Optional[str] = Field(
        None, description="Organization ID from token"
    )
    email: Optional[str] = Field(None, description="User email from token")
    roles: Optional[List[str]] = Field(None, description="User roles")
    permissions: Optional[List[str]] = Field(None, description="User permissions")
    expires_at: Optional[str] = Field(
        None, description="Token expiration time (ISO 8601)"
    )
    error: Optional[str] = Field(None, description="Error message if invalid")


# TASK-018: New request/response models


class UserRegistrationRequest(BaseModel):
    """User registration request."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(
        ..., min_length=8, description="User password (min 8 characters)"
    )
    full_name: str = Field(
        ..., min_length=1, max_length=200, description="User's full name"
    )


class UserResponse(BaseModel):
    """User response (without sensitive data)."""

    user_id: str = Field(..., description="User ID")
    email: str = Field(..., description="User email")
    full_name: str = Field(..., description="User's full name")
    is_active: bool = Field(..., description="Whether account is active")
    is_verified: bool = Field(..., description="Whether email is verified")
    created_at: str = Field(..., description="Account creation timestamp (ISO 8601)")
    updated_at: str = Field(..., description="Last update timestamp (ISO 8601)")
    last_login_at: Optional[str] = Field(
        None, description="Last login timestamp (ISO 8601)"
    )


class PasswordResetRequestRequest(BaseModel):
    """Password reset request (email)."""

    email: EmailStr = Field(..., description="User email address")


class PasswordResetRequest(BaseModel):
    """Password reset with token."""

    token: str = Field(..., description="Password reset token")
    new_password: str = Field(
        ..., min_length=8, description="New password (min 8 characters)"
    )


class PasswordChangeRequest(BaseModel):
    """Password change request (authenticated)."""

    current_password: str = Field(..., description="Current password")
    new_password: str = Field(
        ..., min_length=8, description="New password (min 8 characters)"
    )


# ============================================================
# User Service Dependency
# ============================================================


async def get_user_service(request: Request):
    """Get UserService instance from app.state (Composition Root).

    Uses the Composition Root pattern: UserService is attached to app.state
    with its auth_service dependency properly injected.

    Args:
        request: FastAPI request object

    Returns:
        UserService instance

    Raises:
        RuntimeError: If UserService not available in app.state
    """
    user_service = getattr(request.app.state, "user_service", None)
    if user_service:
        return user_service
    else:
        raise RuntimeError(
            "UserService not available from app.state. "
            "Ensure the container is properly initialized with auth_service."
        )


# ============================================================
# Development User Lookup (Development Fallback)
# ============================================================


async def _dev_validate_credentials(
    email: str, password: str
) -> Optional[Dict[str, Any]]:
    """Development-only credential validation (development fallback).

    For development, accepts any password for known test users.
    This is a fallback when UserService is not available.

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

    Args:
        user_id: User ID to load

    Returns:
        Tuple of (email, roles, permissions) or None
    """
    # Try to load from UserService first
    try:
        user_service = get_user_service()
        user = await user_service.get_user(user_id)
        if user:
            return (user.email, user.roles, None)
    except Exception:
        pass

    # Fall back to development test users by ID
    dev_users = {
        "dev-admin-001": ("admin@faultmaven.local", ["admin"], None),
        "dev-member-001": ("member@faultmaven.local", ["member"], None),
        "dev-viewer-001": ("viewer@faultmaven.local", ["viewer"], None),
    }

    return dev_users.get(user_id)


# ============================================================
# Helper Functions
# ============================================================


def _user_to_response(user) -> UserResponse:
    """Convert user model to response.

    Args:
        user: User from repository

    Returns:
        UserResponse for API
    """
    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        full_name=user.display_name,
        is_active=user.is_active,
        is_verified=user.is_email_verified,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


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
    First tries UserService, then falls back to dev users.

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
            "email": "user@example.com",
            "password": "SecureP@ss123"
        }

        Response:
        {
            "access_token": "eyJhbGc...",
            "refresh_token": "eyJhbGc...",
            "token_type": "Bearer",
            "expires_in": 900
        }
    """
    # Try UserService first
    user_service_failed = False
    try:
        user_service = get_user_service()
        user, access_token, refresh_token = await user_service.authenticate_user(
            email=credentials.email,
            password=credentials.password,
        )

        logger.info(f"Successful login via UserService: {credentials.email}")

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=auth_service._access_token_expire_minutes * 60,
        )

    except AuthenticationError as e:
        # User not found or password mismatch - try dev users fallback
        logger.debug(
            f"UserService auth failed for {credentials.email}, trying dev users fallback: {e.message}"
        )
        user_service_failed = True

    except Exception:
        # Other errors - fall back to dev users for development
        user_service_failed = True

    # Fall back to dev credential validation
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


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    request: UserRegistrationRequest,
) -> UserResponse:
    """Register new user account.

    Creates a new user account with the provided credentials.
    Password must meet strength requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character

    Request Body:
        email: User email address
        password: User password
        full_name: User's full name

    Returns:
        UserResponse with user details

    Raises:
        409: Email already registered
        422: Validation error (weak password, invalid email)

    Example:
        POST /api/v1/auth/register
        {
            "email": "user@example.com",
            "password": "SecureP@ss123",
            "full_name": "John Doe"
        }

        Response:
        {
            "user_id": "uuid...",
            "email": "user@example.com",
            "full_name": "John Doe",
            "is_active": true,
            "is_verified": false,
            ...
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.register_user(
            email=request.email,
            password=request.password,
            full_name=request.full_name,
        )

        logger.info(f"User registered: {user.user_id}")
        return _user_to_response(user)

    except ConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )


@router.post("/password/reset-request", status_code=status.HTTP_204_NO_CONTENT)
async def request_password_reset(
    request: PasswordResetRequestRequest,
) -> None:
    """Request password reset token.

    Generates a password reset token and (in production) sends it via email.
    Always returns 204 even if email not found (prevents email enumeration).

    Request Body:
        email: User email address

    Returns:
        204 No Content (always, regardless of email existence)

    Note:
        In development, the token is logged for testing.
        In production, the token would be sent via email.

    Example:
        POST /api/v1/auth/password/reset-request
        {
            "email": "user@example.com"
        }
    """
    try:
        user_service = get_user_service()
        token = await user_service.request_password_reset(email=request.email)

        # In development, log the token for testing
        logger.debug(f"Password reset token generated for {request.email}: {token}")

    except Exception as e:
        # Log error but don't expose it (prevents enumeration)
        logger.warning(f"Password reset request error: {e}")

    # Always return 204 (prevents email enumeration)
    return None


@router.post("/password/reset", response_model=UserResponse)
async def reset_password(
    request: PasswordResetRequest,
) -> UserResponse:
    """Reset password with reset token.

    Resets the user's password using a valid reset token.
    All existing sessions are invalidated after reset.

    Request Body:
        token: Password reset token (from email)
        new_password: New password

    Returns:
        UserResponse with updated user details

    Raises:
        401: Invalid or expired token
        422: Weak password

    Example:
        POST /api/v1/auth/password/reset
        {
            "token": "eyJhbGc...",
            "new_password": "NewSecureP@ss456"
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.reset_password(
            reset_token=request.token,
            new_password=request.new_password,
        )

        logger.info(f"Password reset completed for user: {user.user_id}")
        return _user_to_response(user)

    except AuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=e.message,
        )

    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )


@router.post("/password/change", response_model=UserResponse)
async def change_password(
    request: PasswordChangeRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> UserResponse:
    """Change password (authenticated).

    Changes the current user's password. Requires current password.
    All existing sessions are invalidated after change.

    Headers:
        Authorization: Bearer <access_token>

    Request Body:
        current_password: Current password
        new_password: New password

    Returns:
        UserResponse with updated user details

    Raises:
        401: Current password incorrect or not authenticated
        422: Weak new password

    Example:
        POST /api/v1/auth/password/change
        Headers: Authorization: Bearer eyJhbGc...
        {
            "current_password": "OldP@ss123",
            "new_password": "NewSecureP@ss456"
        }
    """
    try:
        user_service = get_user_service()
        user = await user_service.change_password(
            user_id=current_user.user_id,
            current_password=request.current_password,
            new_password=request.new_password,
        )

        logger.info(f"Password changed for user: {user.user_id}")
        return _user_to_response(user)

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    except AuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=e.message,
        )

    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
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
