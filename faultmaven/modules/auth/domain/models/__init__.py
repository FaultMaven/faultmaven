"""Auth Module - Domain Models

Contains all authentication, authorization, user, session, and organization models.
"""

from .rbac import Role, Permission
from .user import User
from .session import Session
from .auth import AuthToken, TokenPair, TokenClaims, TokenStatus, DevUser
from .api_auth import (
    DevLoginRequest,
    UserProfile,
    AuthTokenResponse,
    LogoutResponse,
    AuthError,
    TokenValidationError,
    AuthenticationRequiredError,
    UserInfoResponse,
)
from .organization import (
    OrgPlanTier,
    AuditEventType,
    AuditCategory,
)

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
