"""No-Auth Provider (Local Deployment)

Purpose: No-op authentication provider for local/single-user deployment.

This provider always returns an authenticated user without validating tokens,
enabling local development without authentication overhead.
"""

import logging
from typing import Optional

from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class NoAuthProvider(AuthProvider):
    """No-op auth provider for local deployment (single user).
    
    Always returns an authenticated user without token validation.
    This enables local development without authentication complexity.
    """
    
    DEFAULT_USER_ID = "local-user"
    DEFAULT_EMAIL = "local@faultmaven.local"
    DEFAULT_ORG_ID = "local-org"
    DEFAULT_ROLES = ["admin"]
    DEFAULT_PERMISSIONS = ["*"]  # All permissions for local user
    
    def __init__(self, default_user_id: str = DEFAULT_USER_ID):
        """Initialize NoAuthProvider.
        
        Args:
            default_user_id: User ID to return (default: "local-user")
        """
        self.default_user_id = default_user_id
        logger.info(f"NoAuthProvider initialized (user_id: {default_user_id})")
    
    async def validate_token(self, token: str) -> Optional[AuthenticatedUser]:
        """Always returns authenticated user (no validation).
        
        Args:
            token: Ignored (no validation performed)
            
        Returns:
            AuthenticatedUser with default local user credentials
        """
        return AuthenticatedUser(
            user_id=self.default_user_id,
            organization_id=self.DEFAULT_ORG_ID,
            email=self.DEFAULT_EMAIL,
            roles=self.DEFAULT_ROLES,
            permissions=self.DEFAULT_PERMISSIONS,
            token_jti=None
        )
    
    def is_enabled(self) -> bool:
        """Authentication is disabled for local deployment."""
        return False
