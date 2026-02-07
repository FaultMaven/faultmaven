"""JWT Token Generator Service for OAuth 2.0.

This module provides JWT token generation and validation for OAuth 2.0 flows.
Implements RS256 (RSA + SHA256) for asymmetric signing and stateless validation.

Design:
- Access tokens: Short-lived (1 hour), stateless JWT tokens
- Refresh tokens: Long-lived (7 days), tracked for revocation
- Token rotation: One-time use refresh tokens (security best practice)
- Revocation tracking: Redis for cloud, in-memory for local
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import jwt

from faultmaven.modules.auth.domain.models.user import User

logger = logging.getLogger(__name__)


class IJWTTokenGenerator(ABC):
    """Interface for JWT token generation and validation.

    This abstraction allows for different signing strategies (RS256, HS256)
    and different revocation backends (Redis, PostgreSQL, in-memory).
    """

    @abstractmethod
    async def generate_access_token(self, user: User) -> str:
        """Generate short-lived access token (1 hour).

        Args:
            user: User to generate token for

        Returns:
            JWT access token string
        """
        ...

    @abstractmethod
    async def generate_refresh_token(self, user: User) -> str:
        """Generate long-lived refresh token (7 days).

        Args:
            user: User to generate token for

        Returns:
            JWT refresh token string
        """
        ...

    @abstractmethod
    async def validate_access_token(self, token: str) -> Optional[Dict]:
        """Validate access token and return payload.

        Args:
            token: JWT access token

        Returns:
            Token payload if valid, None otherwise
        """
        ...

    @abstractmethod
    async def validate_refresh_token(self, token: str) -> Optional[Dict]:
        """Validate refresh token and check revocation status.

        Args:
            token: JWT refresh token

        Returns:
            Token payload if valid and not revoked, None otherwise
        """
        ...

    @abstractmethod
    async def revoke_access_token(self, token: str) -> None:
        """Revoke access token (add to revocation list).

        Args:
            token: JWT access token to revoke
        """
        ...

    @abstractmethod
    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token (prevent future use).

        Args:
            token: JWT refresh token to revoke
        """
        ...


class RS256JWTTokenGenerator(IJWTTokenGenerator):
    """JWT token generator using RS256 (RSA + SHA256).

    Uses asymmetric signing for stateless token validation:
    - Private key for signing (Dashboard only)
    - Public key for validation (All services can validate)

    Token Structure:
    - Access Token: {sub: user_id, username, exp, iat, jti, type: access}
    - Refresh Token: {sub: user_id, exp, iat, jti, type: refresh}
    """

    def __init__(
        self,
        private_key: str,
        public_key: str,
        revocation_store,  # ITokenRevocationStore
        settings,  # AuthSettings from config
    ):
        """Initialize JWT token generator.

        Args:
            private_key: RSA private key (PEM format) for signing
            public_key: RSA public key (PEM format) for validation
            revocation_store: Token revocation tracking storage
            settings: Authentication configuration
        """
        self.private_key = private_key
        self.public_key = public_key
        self.revocation_store = revocation_store
        self.settings = settings

    async def generate_access_token(self, user: User) -> str:
        """Generate RS256-signed access token.

        Token Claims:
        - sub: user_id (subject)
        - username: user's username
        - exp: expiration timestamp
        - iat: issued at timestamp
        - jti: JWT ID (for revocation tracking)
        - type: "access" (token type discriminator)

        Args:
            user: User to generate token for

        Returns:
            JWT access token string
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(
            minutes=self.settings.jwt_access_token_expire_minutes
        )

        # Generate unique JWT ID for revocation tracking
        import uuid

        jti = str(uuid.uuid4())

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "username": user.username,
            "exp": expires_at,  # Expiration time
            "iat": now,  # Issued at
            "jti": jti,  # JWT ID (unique identifier)
            "type": "access",  # Token type
        }

        token = jwt.encode(
            payload,
            self.private_key,
            algorithm="RS256",
        )

        logger.info(
            "JWT access token generated",
            extra={
                "user_id": user.user_id,
                "username": user.username,
                "jti": jti,
                "expires_in_minutes": self.settings.jwt_access_token_expire_minutes,
            },
        )
        return token

    async def generate_refresh_token(self, user: User) -> str:
        """Generate RS256-signed refresh token.

        Token Claims:
        - sub: user_id (subject)
        - exp: expiration timestamp (7 days)
        - iat: issued at timestamp
        - jti: JWT ID (for revocation tracking)
        - type: "refresh" (token type discriminator)

        Args:
            user: User to generate token for

        Returns:
            JWT refresh token string
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self.settings.jwt_refresh_token_expire_days)

        # Generate unique JWT ID for revocation tracking
        import uuid

        jti = str(uuid.uuid4())

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "exp": expires_at,  # Expiration time
            "iat": now,  # Issued at
            "jti": jti,  # JWT ID (unique identifier)
            "type": "refresh",  # Token type
        }

        token = jwt.encode(
            payload,
            self.private_key,
            algorithm="RS256",
        )

        logger.info(
            "JWT refresh token generated",
            extra={
                "user_id": user.user_id,
                "jti": jti,
                "expires_in_days": self.settings.jwt_refresh_token_expire_days,
            },
        )
        return token

    async def validate_access_token(self, token: str) -> Optional[Dict]:
        """Validate access token using public key.

        Verification:
        1. Signature verification (RS256)
        2. Expiration check
        3. Token type check (must be "access")
        4. Revocation check (if jti present)

        Args:
            token: JWT access token

        Returns:
            Token payload if valid, None otherwise
        """
        try:
            # Decode and verify token
            payload = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                options={"verify_exp": True},
            )

            # Verify token type
            if payload.get("type") != "access":
                logger.warning(
                    "JWT validation failed: invalid token type",
                    extra={
                        "expected_type": "access",
                        "actual_type": payload.get("type"),
                        "user_id": payload.get("sub"),
                    },
                )
                return None

            # Check revocation status
            jti = payload.get("jti")
            if jti:
                is_revoked = await self.revocation_store.is_revoked(jti)
                if is_revoked:
                    logger.info(
                        "JWT validation failed: token revoked",
                        extra={
                            "jti": jti,
                            "user_id": payload.get("sub"),
                        },
                    )
                    return None

            logger.debug(
                "JWT access token validated",
                extra={
                    "user_id": payload.get("sub"),
                    "username": payload.get("username"),
                    "jti": jti,
                },
            )
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT validation failed: token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(
                "JWT validation failed: invalid token", extra={"error": str(e)}
            )
            return None
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return None

    async def validate_refresh_token(self, token: str) -> Optional[Dict]:
        """Validate refresh token using public key.

        Verification:
        1. Signature verification (RS256)
        2. Expiration check
        3. Token type check (must be "refresh")
        4. Revocation check (CRITICAL for refresh tokens)

        Args:
            token: JWT refresh token

        Returns:
            Token payload if valid and not revoked, None otherwise
        """
        try:
            # Decode and verify token
            payload = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                options={"verify_exp": True},
            )

            # Verify token type
            if payload.get("type") != "refresh":
                logger.warning(
                    "JWT validation failed: invalid token type",
                    extra={
                        "expected_type": "refresh",
                        "actual_type": payload.get("type"),
                        "user_id": payload.get("sub"),
                    },
                )
                return None

            # Check revocation status (CRITICAL for refresh tokens)
            jti = payload.get("jti")
            if not jti:
                logger.warning(
                    "JWT validation failed: refresh token missing jti",
                    extra={"user_id": payload.get("sub")},
                )
                return None

            is_revoked = await self.revocation_store.is_revoked(jti)
            if is_revoked:
                logger.info(
                    "JWT validation failed: refresh token revoked",
                    extra={
                        "jti": jti,
                        "user_id": payload.get("sub"),
                    },
                )
                return None

            logger.debug(
                "JWT refresh token validated",
                extra={
                    "user_id": payload.get("sub"),
                    "jti": jti,
                },
            )
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT validation failed: refresh token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(
                "JWT validation failed: invalid refresh token", extra={"error": str(e)}
            )
            return None
        except Exception as e:
            logger.error("JWT validation error", extra={"error": str(e)}, exc_info=True)
            return None

    async def revoke_access_token(self, token: str) -> None:
        """Revoke access token by adding jti to revocation list.

        Args:
            token: JWT access token to revoke
        """
        try:
            # Decode without verification to get jti
            payload = jwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )

            jti = payload.get("jti")
            if not jti:
                logger.warning(
                    "JWT revocation skipped: token missing jti",
                    extra={"user_id": payload.get("sub")},
                )
                return

            # Calculate remaining TTL for revocation entry
            exp = payload.get("exp")
            user_id = payload.get("sub")
            if exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                if ttl > 0:
                    await self.revocation_store.add_revoked_token(jti, ttl)
                    logger.info(
                        "JWT access token revoked",
                        extra={
                            "jti": jti,
                            "user_id": user_id,
                            "ttl_seconds": ttl,
                        },
                    )
            else:
                # No expiration, revoke with default TTL
                default_ttl = self.settings.jwt_access_token_expire_minutes * 60
                await self.revocation_store.add_revoked_token(jti, default_ttl)
                logger.info(
                    "JWT access token revoked",
                    extra={
                        "jti": jti,
                        "user_id": user_id,
                        "ttl_seconds": default_ttl,
                    },
                )

        except Exception as e:
            logger.error(
                "JWT revocation failed", extra={"error": str(e)}, exc_info=True
            )

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token by adding jti to revocation list.

        Args:
            token: JWT refresh token to revoke
        """
        try:
            # Decode without verification to get jti
            payload = jwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )

            jti = payload.get("jti")
            if not jti:
                logger.warning(
                    "JWT revocation skipped: refresh token missing jti",
                    extra={"user_id": payload.get("sub")},
                )
                return

            # Calculate remaining TTL for revocation entry
            exp = payload.get("exp")
            user_id = payload.get("sub")
            if exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                if ttl > 0:
                    await self.revocation_store.add_revoked_token(jti, ttl)
                    logger.info(
                        "JWT refresh token revoked",
                        extra={
                            "jti": jti,
                            "user_id": user_id,
                            "ttl_seconds": ttl,
                        },
                    )
            else:
                # No expiration, revoke with default TTL
                default_ttl = self.settings.jwt_refresh_token_expire_days * 86400
                await self.revocation_store.add_revoked_token(jti, default_ttl)
                logger.info(
                    "JWT refresh token revoked",
                    extra={
                        "jti": jti,
                        "user_id": user_id,
                        "ttl_seconds": default_ttl,
                    },
                )

        except Exception as e:
            logger.error(
                "JWT revocation failed", extra={"error": str(e)}, exc_info=True
            )


class HS256JWTTokenGenerator(IJWTTokenGenerator):
    """JWT token generator using HS256 (HMAC + SHA256).

    Uses symmetric signing for local development and single-user deployments:
    - Same secret key for signing and validation
    - Simpler key management (single JWT_SECRET_KEY environment variable)
    - Suitable for local/self-hosted deployments

    Token Structure (identical to RS256):
    - Access Token: {sub: user_id, username, email, roles, scopes, exp, iat, jti, type: access}
    - Refresh Token: {sub: user_id, exp, iat, jti, type: refresh}
    """

    def __init__(
        self,
        secret_key: str,
        revocation_store,  # ITokenRevocationStore
        settings,  # AuthSettings from config
        issuer: str = "faultmaven",
        audience: str = "faultmaven-api",
    ):
        """Initialize JWT token generator.

        Args:
            secret_key: Secret key for HS256 signing/validation
            revocation_store: Token revocation tracking storage
            settings: Authentication configuration
            issuer: JWT issuer (iss claim)
            audience: JWT audience (aud claim)
        """
        self.secret_key = secret_key
        self.revocation_store = revocation_store
        self.settings = settings
        self.issuer = issuer
        self.audience = audience

    async def generate_access_token(self, user: User) -> str:
        """Generate HS256-signed access token.

        Token Claims (per iam-design.md):
        - sub: user_id (subject)
        - username: user's username
        - email: user's email
        - roles: user roles list
        - scopes: OAuth scopes (for compatibility)
        - exp: expiration timestamp
        - iat: issued at timestamp
        - iss: "faultmaven" (issuer)
        - aud: "faultmaven-api" (audience)
        - jti: JWT ID (for revocation tracking)
        - type: "access" (token type discriminator)
        - auth_mode: "local" (authentication mode)

        Args:
            user: User to generate token for

        Returns:
            JWT access token string
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(
            minutes=self.settings.jwt_access_token_expire_minutes
        )

        # Generate unique JWT ID for revocation tracking
        import uuid

        jti = str(uuid.uuid4())

        # Build payload matching iam-design.md spec
        # Determine organization_id (use user's org or default for local mode)
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        org_id = (
            getattr(user, "organization_id", None)
            or SingleTenantProvider.DEFAULT_ORG_ID
        )

        # Log when using default org_id (helps debugging)
        if not getattr(user, "organization_id", None):
            logger.debug(
                "Using default org_id for user without organization",
                extra={"user_id": user.user_id, "org_id": org_id},
            )

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "username": user.username,
            "email": user.email if hasattr(user, "email") else "",
            "org_id": org_id,  # Organization ID (required for all modes)
            "roles": user.roles if hasattr(user, "roles") else ["user"],
            "scopes": [
                "openid",
                "profile",
                "email",
                "cases:read",
                "cases:write",
                "knowledge:read",
            ],
            "exp": expires_at,  # Expiration time
            "iat": now,  # Issued at
            "iss": self.issuer,  # Issuer
            "aud": self.audience,  # Audience
            "jti": jti,  # JWT ID (unique identifier)
            "type": "access",  # Token type
            "auth_mode": "local",  # Authentication mode
        }

        token = jwt.encode(
            payload,
            self.secret_key,
            algorithm="HS256",
        )

        logger.info(
            "JWT access token generated (HS256)",
            extra={
                "user_id": user.user_id,
                "username": user.username,
                "jti": jti,
                "expires_in_minutes": self.settings.jwt_access_token_expire_minutes,
                "auth_mode": "local",
            },
        )
        return token

    async def generate_refresh_token(self, user: User) -> str:
        """Generate HS256-signed refresh token.

        Token Claims:
        - sub: user_id (subject)
        - exp: expiration timestamp (7 days)
        - iat: issued at timestamp
        - iss: "faultmaven" (issuer)
        - aud: "faultmaven-api" (audience)
        - jti: JWT ID (for revocation tracking)
        - type: "refresh" (token type discriminator)

        Args:
            user: User to generate token for

        Returns:
            JWT refresh token string
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self.settings.jwt_refresh_token_expire_days)

        # Generate unique JWT ID for revocation tracking
        import uuid

        jti = str(uuid.uuid4())

        payload = {
            "sub": user.user_id,  # Subject (user ID)
            "exp": expires_at,  # Expiration time
            "iat": now,  # Issued at
            "iss": "faultmaven",  # Issuer
            "aud": "faultmaven-api",  # Audience
            "jti": jti,  # JWT ID (unique identifier)
            "type": "refresh",  # Token type
        }

        token = jwt.encode(
            payload,
            self.secret_key,
            algorithm="HS256",
        )

        logger.info(
            "JWT refresh token generated (HS256)",
            extra={
                "user_id": user.user_id,
                "jti": jti,
                "expires_in_days": self.settings.jwt_refresh_token_expire_days,
            },
        )
        return token

    async def validate_access_token(self, token: str) -> Optional[Dict]:
        """Validate access token using secret key.

        Verification:
        1. Signature verification (HS256)
        2. Expiration check
        3. Issuer/Audience check
        4. Token type check (must be "access")
        5. Revocation check (if jti present)

        Args:
            token: JWT access token

        Returns:
            Token payload if valid, None otherwise
        """
        try:
            # Decode and verify token
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=["HS256"],
                audience="faultmaven-api",
                issuer="faultmaven",
                options={"verify_exp": True},
            )

            # Verify token type
            if payload.get("type") != "access":
                logger.warning(
                    "JWT validation failed: invalid token type",
                    extra={
                        "expected_type": "access",
                        "actual_type": payload.get("type"),
                        "user_id": payload.get("sub"),
                    },
                )
                return None

            # Check revocation status
            jti = payload.get("jti")
            if jti:
                is_revoked = await self.revocation_store.is_revoked(jti)
                if is_revoked:
                    logger.info(
                        "JWT validation failed: token revoked",
                        extra={
                            "jti": jti,
                            "user_id": payload.get("sub"),
                        },
                    )
                    return None

            logger.debug(
                "JWT access token validated (HS256)",
                extra={
                    "user_id": payload.get("sub"),
                    "jti": jti,
                },
            )
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT validation failed: access token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(
                "JWT validation failed: invalid access token", extra={"error": str(e)}
            )
            return None
        except Exception as e:
            logger.error("JWT validation error", extra={"error": str(e)}, exc_info=True)
            return None

    async def validate_refresh_token(self, token: str) -> Optional[Dict]:
        """Validate refresh token and check revocation status.

        Args:
            token: JWT refresh token

        Returns:
            Token payload if valid and not revoked, None otherwise
        """
        try:
            # Decode and verify token
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=["HS256"],
                audience="faultmaven-api",
                issuer="faultmaven",
                options={"verify_exp": True},
            )

            # Verify token type
            if payload.get("type") != "refresh":
                logger.warning(
                    "JWT validation failed: invalid token type",
                    extra={
                        "expected_type": "refresh",
                        "actual_type": payload.get("type"),
                        "user_id": payload.get("sub"),
                    },
                )
                return None

            # Check revocation status
            jti = payload.get("jti")
            if jti:
                is_revoked = await self.revocation_store.is_revoked(jti)
                if is_revoked:
                    logger.info(
                        "JWT validation failed: refresh token revoked",
                        extra={
                            "jti": jti,
                            "user_id": payload.get("sub"),
                        },
                    )
                    return None

            logger.debug(
                "JWT refresh token validated (HS256)",
                extra={
                    "user_id": payload.get("sub"),
                    "jti": jti,
                },
            )
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("JWT validation failed: refresh token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(
                "JWT validation failed: invalid refresh token", extra={"error": str(e)}
            )
            return None
        except Exception as e:
            logger.error("JWT validation error", extra={"error": str(e)}, exc_info=True)
            return None

    async def revoke_access_token(self, token: str) -> None:
        """Revoke access token by adding jti to revocation list.

        Args:
            token: JWT access token to revoke
        """
        try:
            # Decode without verification to get jti
            payload = jwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )

            jti = payload.get("jti")
            if not jti:
                logger.warning(
                    "JWT revocation skipped: token missing jti",
                    extra={"user_id": payload.get("sub")},
                )
                return

            # Calculate remaining TTL for revocation entry
            exp = payload.get("exp")
            user_id = payload.get("sub")
            if exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                if ttl > 0:
                    await self.revocation_store.add_revoked_token(jti, ttl)
                    logger.info(
                        "JWT access token revoked (HS256)",
                        extra={
                            "jti": jti,
                            "user_id": user_id,
                            "ttl_seconds": ttl,
                        },
                    )
            else:
                # No expiration, revoke with default TTL
                default_ttl = self.settings.jwt_access_token_expire_minutes * 60
                await self.revocation_store.add_revoked_token(jti, default_ttl)
                logger.info(
                    "JWT access token revoked (HS256)",
                    extra={
                        "jti": jti,
                        "user_id": user_id,
                        "ttl_seconds": default_ttl,
                    },
                )

        except Exception as e:
            logger.error(
                "JWT revocation failed", extra={"error": str(e)}, exc_info=True
            )

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke refresh token by adding jti to revocation list.

        Args:
            token: JWT refresh token to revoke
        """
        try:
            # Decode without verification to get jti
            payload = jwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )

            jti = payload.get("jti")
            if not jti:
                logger.warning(
                    "JWT revocation skipped: token missing jti",
                    extra={"user_id": payload.get("sub")},
                )
                return

            # Calculate remaining TTL for revocation entry
            exp = payload.get("exp")
            user_id = payload.get("sub")
            if exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
                if ttl > 0:
                    await self.revocation_store.add_revoked_token(jti, ttl)
                    logger.info(
                        "JWT refresh token revoked (HS256)",
                        extra={
                            "jti": jti,
                            "user_id": user_id,
                            "ttl_seconds": ttl,
                        },
                    )
            else:
                # No expiration, revoke with default TTL
                default_ttl = self.settings.jwt_refresh_token_expire_days * 86400
                await self.revocation_store.add_revoked_token(jti, default_ttl)
                logger.info(
                    "JWT refresh token revoked (HS256)",
                    extra={
                        "jti": jti,
                        "user_id": user_id,
                        "ttl_seconds": default_ttl,
                    },
                )

        except Exception as e:
            logger.error(
                "JWT revocation failed", extra={"error": str(e)}, exc_info=True
            )


class ITokenRevocationStore(ABC):
    """Interface for token revocation tracking.

    Stores revoked token JTIs (JWT IDs) with TTL matching token expiration.
    After token expires, revocation entry can be removed (no longer needed).
    """

    @abstractmethod
    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        """Add token JTI to revocation list.

        Args:
            jti: JWT ID to revoke
            ttl: Time to live in seconds (matches token expiration)
        """
        ...

    @abstractmethod
    async def is_revoked(self, jti: str) -> bool:
        """Check if token JTI is revoked.

        Args:
            jti: JWT ID to check

        Returns:
            True if revoked, False otherwise
        """
        ...

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Clean up expired revocation entries.

        Returns:
            Count of entries cleaned up
        """
        ...
