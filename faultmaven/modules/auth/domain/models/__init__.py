"""Auth Module - Domain Models

Contains all authentication, authorization, user, session, and organization models.
"""

from .api_auth import (
    AuthenticationRequiredError,
    AuthError,
    AuthTokenResponse,
    DevLoginRequest,
    LogoutResponse,
    TokenValidationError,
    UserInfoResponse,
    UserProfile,
)
from .auth import (
    AuthenticatedUser,
    AuthToken,
    DevUser,
    TokenClaims,
    TokenPair,
    TokenStatus,
    TokenValidationResult,
)
from .organization import AuditCategory, AuditEventType, OrgPlanTier
from .rbac import Permission, Role
from .session import Session
from .user import User

__all__ = [
    # RBAC
    "Role",
    "Permission",
    # User
    "User",
    # Session
    "Session",
    # Auth
    "AuthToken",
    "TokenPair",
    "TokenClaims",
    "TokenStatus",
    "DevUser",
    "TokenValidationResult",
    "AuthenticatedUser",
    # API Auth
    "DevLoginRequest",
    "UserProfile",
    "AuthTokenResponse",
    "LogoutResponse",
    "AuthError",
    "TokenValidationError",
    "AuthenticationRequiredError",
    "UserInfoResponse",
    # Organization
    "OrgPlanTier",
    "AuditEventType",
    "AuditCategory",
]
