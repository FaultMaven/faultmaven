"""Authentication Provider Base Interface

Purpose: Abstract base class for authentication providers following the
deployment-agnostic architecture pattern.

This interface enables the same application code to work with different
authentication backends (no-auth, Auth0, Clerk) without deployment-specific
branching in business logic.
"""

from abc import ABC, abstractmethod
from typing import Optional

from faultmaven.modules.auth.domain.models import AuthenticatedUser


class AuthProvider(ABC):
    """Abstract base class for authentication providers.
    
    Enables deployment-neutral authentication by abstracting token validation.
    Business logic depends on this interface, not concrete implementations.
    
    Implementations:
    - NoAuthProvider: Returns default user (local deployment)
    - Auth0Provider: Validates Auth0 JWTs (cloud deployment)
    - ClerkProvider: Validates Clerk JWTs (cloud deployment)
    
    Design Pattern:
        This follows the Strategy pattern, allowing authentication backend
        to be determined at runtime via dependency injection rather than
        compile-time conditional logic.
    """
    
    @abstractmethod
    async def validate_token(self, token: str) -> Optional[AuthenticatedUser]:
        """Validate token and return authenticated user.
        
        Args:
            token: JWT token string (may be empty for no-auth provider)
            
        Returns:
            AuthenticatedUser if valid, None if invalid
            
        Raises:
            AuthenticationError: If token validation fails (for enabled providers)
        """
        pass
    
    @abstractmethod
    def is_enabled(self) -> bool:
        """Check if authentication is enabled.
        
        Returns:
            True if authentication is required, False if disabled (no-auth mode)
        """
        pass
