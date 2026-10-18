"""Auth Module - Vertical Slice

Contains authentication, authorization, user management, and session
management. (Team/organization *management* UIs are the hosted admin
composed module, ADR-010 D4; the core ships membership resolution and the
repository substrate.)

Public API:
    From domain.models:
        - User, Session
        - AuthToken, TokenPair, TokenClaims, DevUser
        - DevLoginRequest, UserProfile, AuthTokenResponse

    From domain.services (import directly to avoid circular imports):
        - AuthService, AuthSessionService, UserService

Structure:
- api/: API routes for auth endpoints
- domain/: Domain models and services
- infrastructure/: Persistence layer (repositories, stores)
"""

# Domain models (already eagerly imported in domain/models/__init__.py)
from faultmaven.modules.auth.domain.models import (  # User & Session; Auth; API Auth
    AuthToken,
    AuthTokenResponse,
    DevLoginRequest,
    DevUser,
    LogoutResponse,
    Session,
    TokenClaims,
    TokenPair,
    TokenStatus,
    User,
    UserProfile,
)

# Domain services - import directly to avoid circular imports:
# from faultmaven.modules.auth.domain.services.auth_service import AuthService
# from faultmaven.modules.auth.domain.services.auth_session_service import AuthSessionService

__all__ = [
    # Models - User & Session
    "User",
    "Session",
    # Models - Auth
    "AuthToken",
    "TokenPair",
    "TokenClaims",
    "TokenStatus",
    "DevUser",
    # Models - API Auth
    "DevLoginRequest",
    "UserProfile",
    "AuthTokenResponse",
    "LogoutResponse",
]
