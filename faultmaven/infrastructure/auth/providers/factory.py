"""Authentication Provider Factory

Purpose: Factory function to create appropriate AuthProvider based on configuration.

Selects provider implementation based on AUTH_PROVIDER environment variable:
- "no-auth" or "none": NoAuthProvider (local deployment)
- "auth0": Auth0Provider (cloud deployment)
- "clerk": ClerkProvider (cloud deployment)

Design Reference: Deployment Agnostic Architecture
"""

import logging
from typing import Optional

from faultmaven.config.settings import get_settings

from faultmaven.infrastructure.auth.providers.auth0 import Auth0Provider
from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.infrastructure.auth.providers.clerk import ClerkProvider
from faultmaven.infrastructure.auth.providers.no_auth import NoAuthProvider

logger = logging.getLogger(__name__)


def create_auth_provider() -> AuthProvider:
    """Factory function to create appropriate AuthProvider based on settings.
    
    Selects provider implementation based on AUTH_PROVIDER environment variable:
    - "no-auth" or "none": NoAuthProvider (local deployment)
    - "auth0": Auth0Provider (cloud deployment)
    - "clerk": ClerkProvider (cloud deployment)
    
    Returns:
        AuthProvider: NoAuthProvider, Auth0Provider, or ClerkProvider instance
        
    Raises:
        ValueError: If AUTH_PROVIDER is set to an unknown value
        
    Environment Variables:
        AUTH_PROVIDER: "no-auth" | "auth0" | "clerk" (default: "no-auth")
        AUTH0_DOMAIN: Auth0 domain (required if AUTH_PROVIDER=auth0)
        AUTH0_AUDIENCE: Auth0 API audience (required if AUTH_PROVIDER=auth0)
        CLERK_SECRET_KEY: Clerk secret key (required if AUTH_PROVIDER=clerk)
        
    Design Notes:
        - Defaults to no-auth for local development ease
        - Cloud providers require explicit configuration for production safety
        - Provider instance is singleton-scoped via DI container
    """
    settings = get_settings()
    auth_provider = settings.security.auth_provider.lower()
    
    if auth_provider in ("none", "no-auth", ""):
        logger.info(
            "Creating NoAuthProvider (local/development mode) [AUTH_PROVIDER=no-auth]"
        )
        return NoAuthProvider()
    
    elif auth_provider == "auth0":
        logger.info(
            "Creating Auth0Provider (cloud/enterprise mode) [AUTH_PROVIDER=auth0]"
        )
        
        # Validate required settings
        if not settings.security.auth0_domain:
            raise ValueError(
                "AUTH0_DOMAIN is required when AUTH_PROVIDER=auth0. "
                "Set AUTH0_DOMAIN environment variable."
            )
        if not settings.security.auth0_audience:
            raise ValueError(
                "AUTH0_AUDIENCE is required when AUTH_PROVIDER=auth0. "
                "Set AUTH0_AUDIENCE environment variable."
            )
        
        return Auth0Provider(
            domain=settings.security.auth0_domain,
            audience=settings.security.auth0_audience,
            issuer=settings.security.auth0_issuer
        )
    
    elif auth_provider == "clerk":
        logger.info(
            "Creating ClerkProvider (cloud/enterprise mode) [AUTH_PROVIDER=clerk]"
        )
        
        # Validate required settings
        if not settings.security.clerk_secret_key:
            raise ValueError(
                "CLERK_SECRET_KEY is required when AUTH_PROVIDER=clerk. "
                "Set CLERK_SECRET_KEY environment variable."
            )
        
        return ClerkProvider(
            secret_key=settings.security.clerk_secret_key.get_secret_value(),
            audience=settings.security.clerk_audience
        )
    
    else:
        raise ValueError(
            f"Unknown AUTH_PROVIDER: {auth_provider}. "
            "Valid values: 'no-auth', 'auth0', 'clerk'"
        )
