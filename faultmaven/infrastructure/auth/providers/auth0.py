"""Auth0 Provider (Cloud Deployment)

Purpose: JWT validation provider for Auth0 authentication.

Validates JWT tokens issued by Auth0 using JWKS (JSON Web Key Set) endpoint.
Extracts user information from token claims.
"""

import logging
from typing import Optional

import httpx
from jose import jwt, jwk
from jose.utils import base64url_decode

from faultmaven.infrastructure.auth.providers.base import AuthProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser

logger = logging.getLogger(__name__)


class Auth0Provider(AuthProvider):
    """Auth0 JWT validation provider.
    
    Validates JWT tokens issued by Auth0 using JWKS endpoint.
    Supports RS256 algorithm with automatic key rotation.
    """
    
    def __init__(
        self,
        domain: str,
        audience: str,
        issuer: Optional[str] = None
    ):
        """Initialize Auth0Provider.
        
        Args:
            domain: Auth0 domain (e.g., 'your-tenant.auth0.com')
            audience: Auth0 API audience/identifier
            issuer: Token issuer (defaults to https://{domain}/)
        """
        self.domain = domain
        self.audience = audience
        self.issuer = issuer or f"https://{domain}/"
        self.jwks_url = f"https://{domain}/.well-known/jwks.json"
        self._jwks_cache: Optional[dict] = None
        
        logger.info(
            f"Auth0Provider initialized (domain: {domain}, audience: {audience})"
        )
    
    async def _get_jwks(self) -> dict:
        """Fetch JWKS from Auth0.
        
        Returns:
            JWKS dictionary with signing keys
            
        Raises:
            Exception: If JWKS fetch fails
        """
        if self._jwks_cache:
            return self._jwks_cache
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.jwks_url, timeout=5.0)
                response.raise_for_status()
                self._jwks_cache = response.json()
                logger.debug(f"Fetched JWKS from {self.jwks_url}")
                return self._jwks_cache
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {self.jwks_url}: {e}")
            raise
    
    async def validate_token(self, token: str) -> Optional[AuthenticatedUser]:
        """Validate Auth0 JWT token.
        
        Args:
            token: JWT token string from Auth0
            
        Returns:
            AuthenticatedUser if valid, None if invalid
        """
        if not token:
            return None
        
        try:
            # Get JWKS
            jwks = await self._get_jwks()
            
            # Decode header to get key ID
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            
            if not kid:
                logger.warning("Token missing 'kid' in header")
                return None
            
            # Find matching key
            key = None
            for jwk_key in jwks.get("keys", []):
                if jwk_key.get("kid") == kid:
                    key = jwk.construct(jwk_key)
                    break
            
            if not key:
                logger.warning(f"No matching key found for kid: {kid}")
                return None
            
            # Verify and decode token
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer
            )
            
            # Extract user info from claims
            # Auth0 uses 'sub' for user ID, may include custom claims for roles
            user_id = claims.get("sub")
            email = claims.get("email", "")
            
            # Extract roles from custom claim (Auth0 allows custom claims)
            # Default claim namespace: https://faultmaven.ai/roles
            roles = claims.get("https://faultmaven.ai/roles", [])
            if not roles:
                # Fallback to standard claims
                roles = claims.get("roles", ["user"])
            
            # Extract organization ID if present
            organization_id = claims.get("org_id", claims.get("https://faultmaven.ai/org_id", "default"))
            
            # Extract permissions if present
            permissions = claims.get("https://faultmaven.ai/permissions", [])
            if not permissions:
                # Derive from roles (would need role-to-permission mapping)
                permissions = []
            
            token_jti = claims.get("jti")
            
            logger.debug(f"Validated Auth0 token for user: {user_id}")
            
            return AuthenticatedUser(
                user_id=user_id,
                organization_id=organization_id,
                email=email,
                roles=roles if isinstance(roles, list) else [roles],
                permissions=permissions if isinstance(permissions, list) else [permissions],
                token_jti=token_jti
            )
            
        except jwt.ExpiredSignatureError:
            logger.warning("Auth0 token has expired")
            return None
        except jwt.JWTClaimsError as e:
            logger.warning(f"Auth0 token claims invalid: {e}")
            return None
        except jwt.JWTError as e:
            logger.warning(f"Auth0 token validation failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error validating Auth0 token: {e}")
            return None
    
    def is_enabled(self) -> bool:
        """Authentication is enabled for cloud deployment."""
        return True
