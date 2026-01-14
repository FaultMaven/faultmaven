"""Authentication Provider Abstraction

This package provides a deployment-agnostic authentication provider abstraction
that supports multiple authentication backends:
- NoAuthProvider: No authentication (local deployment)
- Auth0Provider: Auth0 JWT validation (cloud deployment)
- ClerkProvider: Clerk JWT validation (cloud deployment)

The provider is selected via AUTH_PROVIDER environment variable and wired
into the DI container at startup.
"""

from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.infrastructure.auth.providers.factory import create_auth_provider

__all__ = [
    "AuthProvider",
    "create_auth_provider",
]
