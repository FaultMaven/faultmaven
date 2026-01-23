"""Auth Module - API Routes

Contains all authentication and authorization API endpoints.
"""

# Don't eagerly import to avoid circular imports
# API routes will be imported by the main app router

__all__ = [
    "auth_router",
    "session_router",
    "teams_router",
    "organizations_router",
    "oauth_router",
]
