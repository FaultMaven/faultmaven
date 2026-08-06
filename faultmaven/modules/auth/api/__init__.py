"""Auth Module - API Routes

Contains all authentication and authorization API endpoints.
"""

# Don't eagerly import to avoid circular imports.
# Routers are imported by the main app router from these submodules.

__all__ = [
    "auth",
    "oauth",
    "rate_limiting",
    "session",
    "sso",
    "teams",
]
