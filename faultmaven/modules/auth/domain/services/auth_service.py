"""JWT Authentication Service (TASK-017)

Purpose: Handle JWT token generation, verification, refresh, and revocation.

This module provides production-ready JWT authentication using RS256 algorithm:
- Access token generation (15 minutes expiry)
- Refresh token generation (7 days expiry)
- Token verification with signature, expiration, issuer, audience validation
- Token revocation via the deployment-wide revocation store (#767)

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import jwt

from faultmaven.config.settings import get_settings

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        ITokenRevocationStore,
    )

from faultmaven.exceptions import AuthorizationError, ServiceError, ValidationException
from faultmaven.modules.auth.domain.models.auth import (
    AuthenticatedUser,
    TokenClaims,
    TokenPair,
)
from faultmaven.modules.auth.domain.models.rbac import get_permissions_for_roles
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    revocation_reason,
)

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Raised when authentication fails (invalid token, expired, wrong format)."""

    def __init__(self, message: str, error_code: str = "AUTH_ERROR"):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class TokenRevocationError(Exception):
    """Raised when token has been revoked."""

    def __init__(self, message: str = "Token has been revoked"):
        self.message = message
        super().__init__(message)


class AuthService:
    """JWT Authentication Service.

    Handles all JWT token operations including:
    - Token generation (access and refresh)
    - Token verification
    - Token revocation

    Uses RS256 (RSA with SHA-256) for asymmetric signing:
    - Private key: Used for token generation
    - Public key: Used for token verification

    Token revocation is tracked via the deployment-wide revocation store
    (one key prefix, shared by every revoke path — #767) with TTL matching
    token expiry.
    """

    def __init__(
        self,
        revocation_store: Optional["ITokenRevocationStore"] = None,
        private_key: Optional[str] = None,
        public_key: Optional[str] = None,
    ):
        """Initialize AuthService.

        Args:
            revocation_store: Deployment-wide token revocation store. Must be
                the same instance the token generators write to, or revoked
                tokens keep working on the request path.
            private_key: RSA private key for signing (optional, loaded from config)
            public_key: RSA public key for verification (optional, loaded from config)
        """
        self._settings = get_settings()
        self._revocation_store = revocation_store
        self._private_key = private_key
        self._public_key = public_key

        # Load keys from configuration if not provided
        self._load_keys()

    def _load_keys(self) -> None:
        """Load RSA keys from configuration.

        Keys can be provided via:
        1. Direct string in environment (JWT_PRIVATE_KEY, JWT_PUBLIC_KEY)
        2. File path (JWT_PRIVATE_KEY_PATH, JWT_PUBLIC_KEY_PATH)
        3. Generated for development (if neither is set)
        """
        security = self._settings.security

        # Load private key
        if not self._private_key:
            if security.jwt_private_key:
                self._private_key = security.jwt_private_key.get_secret_value()
            elif security.jwt_private_key_path:
                key_path = Path(security.jwt_private_key_path)
                if key_path.exists():
                    self._private_key = key_path.read_text()
                else:
                    logger.warning(f"Private key file not found: {key_path}")

        # Load public key
        if not self._public_key:
            if security.jwt_public_key:
                self._public_key = security.jwt_public_key
            elif security.jwt_public_key_path:
                key_path = Path(security.jwt_public_key_path)
                if key_path.exists():
                    self._public_key = key_path.read_text()
                else:
                    logger.warning(f"Public key file not found: {key_path}")

        # Generate development keys if none are configured
        if not self._private_key or not self._public_key:
            self._generate_dev_keys()

    def _generate_dev_keys(self) -> None:
        """Generate RSA key pair for development.

        Uses cryptography library to generate 2048-bit RSA keys.
        These should NOT be used in production - use proper key management.
        """
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa

            # Generate private key
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
                backend=default_backend(),
            )

            # Serialize private key
            self._private_key = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")

            # Serialize public key
            public_key = private_key.public_key()
            self._public_key = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")

            logger.warning(
                "Generated development RSA keys. "
                "Configure JWT_PRIVATE_KEY_PATH and JWT_PUBLIC_KEY_PATH for production."
            )

        except ImportError:
            # Fall back to HS256 with secret key for development
            logger.warning(
                "cryptography library not installed. "
                "Using HS256 with fallback secret for development."
            )
            self._private_key = None
            self._public_key = None

    @property
    def _algorithm(self) -> str:
        """Get JWT algorithm based on AUTH_MODE.

        Algorithm selection follows iam-design.md:
        - AUTH_MODE=local → HS256 (symmetric key with JWT_SECRET_KEY)
        - AUTH_MODE=oauth → RS256 (asymmetric key with RSA keys)

        This ensures consistent algorithm selection across token generation
        and validation, preventing "alg value is not allowed" errors.
        """
        # Respect AUTH_MODE setting for consistent token handling
        if self._settings.auth.auth_mode == "local":
            return "HS256"
        elif self._settings.auth.auth_mode == "oauth":
            return "RS256"

        # Legacy fallback: Check if RSA keys are available
        if self._private_key and self._public_key:
            return "RS256"

        # Final fallback to configured algorithm
        return self._settings.security.jwt_algorithm

    @property
    def _issuer(self) -> str:
        """Get JWT issuer from settings."""
        return self._settings.security.jwt_issuer

    @property
    def _audience(self) -> str:
        """Get JWT audience from settings."""
        return self._settings.security.jwt_audience

    @property
    def _access_token_expire_minutes(self) -> int:
        """Get access token expiration in minutes."""
        return self._settings.security.jwt_access_token_expire_minutes

    @property
    def _refresh_token_expire_days(self) -> int:
        """Get refresh token expiration in days."""
        return self._settings.security.jwt_refresh_token_expire_days

    def generate_access_token(
        self,
        user_id: str,
        organization_id: str,
        email: str,
        roles: List[str],
        permissions: Optional[List[str]] = None,
    ) -> str:
        """Generate JWT access token.

        Args:
            user_id: User UUID
            organization_id: Organization UUID
            email: User email
            roles: User roles in organization (admin, member, viewer)
            permissions: Granular permissions (auto-derived from roles if None)

        Returns:
            Signed JWT access token (valid for configured minutes)

        Raises:
            ServiceError: If token generation fails
        """
        if permissions is None:
            permissions = [p.value for p in get_permissions_for_roles(roles)]

        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=self._access_token_expire_minutes)

        claims = TokenClaims(
            sub=user_id,
            organization_id=organization_id,
            email=email,
            roles=roles,
            permissions=permissions,
            iss=self._issuer,
            aud=self._audience,
            iat=int(now.timestamp()),
            exp=int(expire.timestamp()),
            jti=str(uuid.uuid4()),
            token_type="access",
        )

        return self._encode_token(claims.to_dict())

    def generate_refresh_token(
        self,
        user_id: str,
        organization_id: str,
    ) -> str:
        """Generate JWT refresh token.

        Refresh tokens are long-lived and used to obtain new access tokens.
        They contain minimal claims for security.

        Args:
            user_id: User UUID
            organization_id: Organization UUID

        Returns:
            Signed JWT refresh token (valid for configured days)

        Raises:
            ServiceError: If token generation fails
        """
        now = datetime.now(timezone.utc)
        expire = now + timedelta(days=self._refresh_token_expire_days)

        claims = {
            "sub": user_id,
            "organization_id": organization_id,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp()),
            "jti": str(uuid.uuid4()),
            "type": "refresh",
        }

        return self._encode_token(claims)

    def generate_token_pair(
        self,
        user_id: str,
        organization_id: str,
        email: str,
        roles: List[str],
        permissions: Optional[List[str]] = None,
    ) -> TokenPair:
        """Generate both access and refresh tokens.

        Args:
            user_id: User UUID
            organization_id: Organization UUID
            email: User email
            roles: User roles in organization
            permissions: Granular permissions (auto-derived from roles if None)

        Returns:
            TokenPair with access and refresh tokens
        """
        access_token = self.generate_access_token(
            user_id=user_id,
            organization_id=organization_id,
            email=email,
            roles=roles,
            permissions=permissions,
        )

        refresh_token = self.generate_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
        )

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=self._access_token_expire_minutes * 60,
        )

    def verify_token(
        self,
        token: str,
        token_type: str = "access",
    ) -> Dict[str, Any]:
        """Verify and decode JWT token.

        Performs full validation:
        - Signature verification (RS256 with public key)
        - Expiration check (exp claim)
        - Issuer check (iss claim)
        - Audience check (aud claim)
        - Token type check (access vs refresh)

        Args:
            token: JWT token string
            token_type: Expected token type ("access" or "refresh")

        Returns:
            Decoded token claims

        Raises:
            AuthenticationError: Invalid token, expired, wrong type, etc.
        """
        try:
            # Determine signing key based on algorithm
            if self._algorithm == "RS256" and self._public_key:
                key = self._public_key
            elif self._settings.security.jwt_secret_key:
                # Fallback to HS256 with secret key
                key = self._settings.security.jwt_secret_key.get_secret_value()
            else:
                raise AuthenticationError(
                    "No JWT signing key configured",
                    error_code="CONFIG_ERROR",
                )

            # Decode and verify token
            claims = jwt.decode(
                token,
                key,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "iss", "aud", "exp", "iat", "jti"],
                },
            )

            # Verify token type
            actual_type = claims.get("type", "access")
            if actual_type != token_type:
                raise AuthenticationError(
                    f"Invalid token type. Expected {token_type}, got {actual_type}",
                    error_code="INVALID_TOKEN_TYPE",
                )

            return claims

        except jwt.ExpiredSignatureError:
            raise AuthenticationError(
                "Token has expired",
                error_code="TOKEN_EXPIRED",
            )
        except jwt.InvalidIssuerError:
            raise AuthenticationError(
                "Invalid token issuer",
                error_code="INVALID_ISSUER",
            )
        except jwt.InvalidAudienceError:
            raise AuthenticationError(
                "Invalid token audience",
                error_code="INVALID_AUDIENCE",
            )
        except jwt.DecodeError as e:
            raise AuthenticationError(
                f"Token decode error: {e}",
                error_code="DECODE_ERROR",
            )
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(
                f"Invalid token: {e}",
                error_code="INVALID_TOKEN",
            )

    async def verify_token_with_revocation_check(
        self,
        token: str,
        token_type: str = "access",
    ) -> Dict[str, Any]:
        """Verify token and check revocation status.

        Extends verify_token with Redis revocation list check.

        Args:
            token: JWT token string
            token_type: Expected token type

        Returns:
            Decoded token claims

        Raises:
            AuthenticationError: Invalid or expired token
            TokenRevocationError: Token has been revoked
        """
        # First, verify the token
        claims = self.verify_token(token, token_type)

        # Check revocation status (per-token jti and per-user watermark)
        if await self._is_revoked(claims):
            raise TokenRevocationError()

        return claims

    async def revoke_token(
        self,
        token_jti: str,
        expiration: int,
    ) -> None:
        """Revoke a token by adding jti to the revocation store.

        The store applies a TTL matching token expiration so revoked tokens
        are automatically cleaned up.

        Args:
            token_jti: JWT ID (jti claim)
            expiration: Token expiration timestamp (Unix) for TTL calculation

        Raises:
            ServiceError: The revocation could not be recorded. Callers must
                not report a revocation as successful when this raises.
        """
        if self._revocation_store is None:
            raise ServiceError("Token revocation failed: no revocation store")
        try:
            # Calculate TTL from expiration
            now = int(datetime.now(timezone.utc).timestamp())
            ttl = max(expiration - now, 0)

            if ttl > 0:
                await self._revocation_store.add_revoked_token(token_jti, ttl)
                logger.debug(f"Token revoked: {token_jti} (TTL: {ttl}s)")
        except Exception as e:
            logger.error(f"Failed to revoke token: {e}")
            raise ServiceError(f"Token revocation failed: {e}")

    def _longest_token_lifetime_seconds(self) -> int:
        """Longest refresh-token lifetime any mint path could have used.

        The refresh expiry is declared in BOTH settings halves under the same
        name: ``settings.auth``'s carries the ``JWT_REFRESH_TOKEN_EXPIRY``
        validation alias and is the half the token generators are built with,
        while ``settings.security``'s has no alias and never moves off its
        default. A per-user watermark that expired before the tokens it
        revokes would resurrect them, so take whichever is larger instead of
        betting on one half.
        """
        days = max(
            getattr(self._settings.auth, "jwt_refresh_token_expire_days", 0) or 0,
            getattr(self._settings.security, "jwt_refresh_token_expire_days", 0) or 0,
        )
        if days <= 0:
            # A non-positive TTL is rejected by SETEX, which would turn every
            # revocation into a store error. Fall back to the schema default
            # rather than letting misconfiguration disable revocation.
            logger.warning(
                "No positive refresh-token expiry configured; "
                "defaulting the revocation watermark TTL to 7 days"
            )
            days = 7
        return int(days) * 86400

    async def revoke_user_tokens(
        self,
        user_id: str,
    ) -> datetime:
        """Revoke every outstanding token for a user (#769).

        Writes a revocation watermark to the shared store; the request path
        and both generators then reject any token for this user whose ``iat``
        is at or before that instant. Nothing indexes issued JTIs, so there is
        no token count to report — and no need for one: the watermark covers
        every token from every mint path, which a JTI index could not
        guarantee.

        Args:
            user_id: User ID to revoke tokens for

        Returns:
            The revocation instant. Tokens issued at or before it are invalid.

        Raises:
            ServiceError: The revocation could not be recorded. Callers must
                not report a revocation as successful when this raises.
        """
        if self._revocation_store is None:
            raise ServiceError("Token revocation failed: no revocation store")

        revoked_at = datetime.now(timezone.utc)
        # The watermark must outlive the longest-lived token that could still
        # be presented, or a long refresh token would outlive the entry that
        # revokes it and spring back to life.
        #
        # Take the MAX across both settings halves rather than trusting one.
        # `settings.auth.jwt_refresh_token_expire_days` is the operator knob
        # (`JWT_REFRESH_TOKEN_EXPIRY`) and is what the token generators are
        # constructed with, while `settings.security`'s same-named field has no
        # env alias and is always its default. Reading only the latter would
        # cap the watermark at 7 days while tokens lived for 30.
        ttl = self._longest_token_lifetime_seconds()
        try:
            await self._revocation_store.revoke_user_tokens_before(
                user_id, int(revoked_at.timestamp()), ttl
            )
        except Exception as e:
            logger.error(f"Failed to revoke user tokens: {e}")
            raise ServiceError(f"Token revocation failed: {e}")

        logger.info(
            "All tokens revoked for user",
            extra={"user_id": user_id, "revoked_before": revoked_at.isoformat()},
        )
        return revoked_at

    async def _is_revoked(self, claims: Dict[str, Any]) -> bool:
        """Check whether a token's claims are revoked.

        Fail-open by design: if the store is unavailable, the request-path
        check treats the token as not revoked rather than rejecting all
        traffic. Access tokens are short-lived (<30 min), which bounds the
        exposure; the error is logged for monitoring. Refresh-token
        validation in the generators fails CLOSED (store error => invalid),
        so a store outage cannot mint new credentials from a revoked token.

        Args:
            claims: Verified token claims (``jti``, ``sub`` and ``iat`` are
                read; the composite rule lives in ``revocation_reason``)

        Returns:
            True if the token is revoked
        """
        if self._revocation_store is None:
            return False
        try:
            reason = await revocation_reason(self._revocation_store, claims)
            return reason is not None
        except Exception as e:
            logger.error(f"Failed to check token revocation: {e}")
            # Fail open for availability, but log for monitoring
            return False

    def _encode_token(self, claims: Dict[str, Any]) -> str:
        """Encode claims into JWT token.

        Args:
            claims: Token claims dictionary

        Returns:
            Encoded JWT token string

        Raises:
            ServiceError: If encoding fails
        """
        try:
            # Determine signing key based on algorithm
            if self._algorithm == "RS256" and self._private_key:
                key = self._private_key
            elif self._settings.security.jwt_secret_key:
                # Fallback to HS256 with secret key
                key = self._settings.security.jwt_secret_key.get_secret_value()
            else:
                raise ServiceError("No JWT signing key configured")

            return jwt.encode(
                claims,
                key,
                algorithm=self._algorithm,
            )

        except Exception as e:
            logger.error(f"Failed to encode token: {e}")
            raise ServiceError(f"Token generation failed: {e}")

    def extract_user_from_token(self, token: str) -> AuthenticatedUser:
        """Extract AuthenticatedUser from a valid access token.

        Convenience method that verifies token and returns AuthenticatedUser.

        Args:
            token: JWT access token

        Returns:
            AuthenticatedUser instance

        Raises:
            AuthenticationError: Invalid or expired token
        """
        claims = self.verify_token(token, token_type="access")
        return AuthenticatedUser.from_jwt_claims(claims)

    async def extract_user_from_token_with_revocation_check(
        self,
        token: str,
    ) -> AuthenticatedUser:
        """Extract AuthenticatedUser with revocation check.

        Args:
            token: JWT access token

        Returns:
            AuthenticatedUser instance

        Raises:
            AuthenticationError: Invalid or expired token
            TokenRevocationError: Token has been revoked
        """
        claims = await self.verify_token_with_revocation_check(
            token,
            token_type="access",
        )
        return AuthenticatedUser.from_jwt_claims(claims)
