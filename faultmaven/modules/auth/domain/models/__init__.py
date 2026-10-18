"""Auth Module - Domain Models

Contains all authentication, authorization, user and session models.

The organization's role vocabulary (``Role``, ``Permission``) is not
re-exported here: its one door is ``faultmaven.models.rbac`` (#1040 item 4).

Tenancy models (Organization, Team, Enterprise, their members, plan-tier and
audit enums, and the repository interfaces) are defined once in
``faultmaven.models.interfaces_user`` — import them from there.
"""

from .api_auth import (
    AuthenticationRequiredError,
    AuthError,
    AuthTokenResponse,
    DevLoginRequest,
    LogoutResponse,
    OrganizationSummary,
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
from .session import Session
from .user import User

__all__ = [
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
    "OrganizationSummary",
    "UserInfoResponse",
]
