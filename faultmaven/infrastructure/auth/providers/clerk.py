"""Clerk Provider (Cloud Deployment)

Purpose: JWT validation provider for Clerk authentication.

Validates JWT tokens issued by Clerk using secret key (HS256 algorithm).
Extracts user information from token claims.
"""

import logging
from typing import Optional

from jose import jwt

from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class ClerkProvider(AuthProvider):
    """Clerk JWT validation provider.
    
    Validates JWT tokens issued by Clerk using secret key (HS256).
    Clerk uses symmetric signing with a secret key.
    """
    
    DEFAULT_AUDIENCE = "https://clerk.faultmaven.ai"
    
    def __init__(self, secret_key: str, audience: Optional[str] = None):
        """Initialize ClerkProvider.
        
        Args:
            secret_key: Clerk secret key for JWT verification
            audience: Token audience (defaults to DEFAULT_AUDIENCE)
        """
        self.secret_key = secret_key
        self.audience = audience or self.DEFAULT_AUDIENCE
        
        logger.info("ClerkProvider initialized")
    
    async def validate_token(self, token: str) -> Optional[AuthenticatedUser]:
        """Validate Clerk JWT token.
        
        Args:
            token: JWT token string from Clerk
            
        Returns:
            AuthenticatedUser if valid, None if invalid
        """
        if not token:
            return None
        
        try:
            # Clerk uses HS256 (symmetric) with secret key
            claims = jwt.decode(
                token,
                self.secret_key,
                algorithms=["HS256"],
                audience=self.audience
            )
            
            # Extract user info from claims
            # Clerk uses 'sub' for user ID
            user_id = claims.get("sub")
            email = claims.get("email", "")
            
            # Extract roles from custom claim
            # Clerk allows custom claims via metadata
            roles = claims.get("https://faultmaven.ai/roles", [])
            if not roles:
                roles = claims.get("roles", ["user"])
            
            # Extract organization ID if present
            organization_id = claims.get("org_id", claims.get("https://faultmaven.ai/org_id", "default"))
            
            # Extract permissions if present
            permissions = claims.get("https://faultmaven.ai/permissions", [])
            if not permissions:
                permissions = []
            
            token_jti = claims.get("jti")
            
            logger.debug(f"Validated Clerk token for user: {user_id}")
            
            return AuthenticatedUser(
                user_id=user_id,
                organization_id=organization_id,
                email=email,
                roles=roles if isinstance(roles, list) else [roles],
                permissions=permissions if isinstance(permissions, list) else [permissions],
                token_jti=token_jti
            )
            
        except jwt.ExpiredSignatureError:
            logger.warning("Clerk token has expired")
            return None
        except jwt.JWTClaimsError as e:
            logger.warning(f"Clerk token claims invalid: {e}")
            return None
        except jwt.JWTError as e:
            logger.warning(f"Clerk token validation failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error validating Clerk token: {e}")
            return None
    
    def is_enabled(self) -> bool:
        """Authentication is enabled for cloud deployment."""
        return True
