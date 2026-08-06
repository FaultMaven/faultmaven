"""JWT Authentication Service (TASK-017)

Purpose: Verify, revoke and read JWTs on the request path.

This service does **not** mint tokens. It used to carry a second, independent
signing surface that took a subject id and an ``organization_id`` string and
signed them verbatim — bypassing ``resolve_organization_claim``, the org-claim
guard every real mint funnels through (#850). That surface reached no route and
was removed in #853; ``IJWTTokenGenerator`` (``jwt_token_generator``) is the one
mint path.

What remains here:
- Token verification with signature, expiration, issuer, audience validation
- Token revocation via the deployment-wide revocation store (#767)
- Extraction of the request-path identity from verified claims

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import jwt

from faultmaven.config.settings import get_settings

if TYPE_CHECKING:
    from faultmaven.modules.auth.domain.services.jwt_token_generator import (
        ITokenRevocationStore,
    )

from faultmaven.exceptions import ServiceError
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
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


class PartialKeyConfigurationError(RuntimeError):
    """Raised when a deployment configures one half of the RSA key pair.

    Half a key pair is an operator error, not a state to recover from. The only
    recovery available here — generating a development pair — would discard the
    half that *was* configured, so recovering is strictly worse than refusing:
    the deployment would come up looking healthy and reject every genuinely
    minted token.

    A ``RuntimeError`` deliberately. ``compose_application`` re-raises
    ``RuntimeError`` in every deployment mode while swallowing other exception
    types into a degraded boot, and a security-configuration refusal must not be
    the thing that degrades.
    """


class AuthService:
    """JWT Authentication Service — the request-path side of JWT handling.

    Handles:
    - Token verification (signature, expiry, issuer, audience, type, jti)
    - Token revocation, per token and per user
    - Extraction of the request-path identity from verified claims

    It mints nothing. Tokens are minted by ``IJWTTokenGenerator``
    (``jwt_token_generator``), which signs from a user object, refuses a
    deactivated account and resolves the organization claim. ``AuthService``
    carried a parallel mint until #853; it reached no route and was removed.

    Under ``AUTH_MODE=oauth`` verification is RS256 against the configured public
    key; under ``AUTH_MODE=local`` it is HS256 against the shared secret.
    ``AuthMode`` admits only those two values, so the RSA-presence branch at the
    end of ``_algorithm`` is unreachable and decides nothing.

    **Both RSA keys must stay configured, even though nothing here signs.**
    ``_load_keys`` runs on every construction and loads the private key as well
    as the public one, and it refuses a half-configured pair rather than filling
    the gap: ``_generate_dev_keys`` replaces **both** halves, so fabricating
    would discard the half that *was* configured. Dropping ``JWT_PRIVATE_KEY``
    from an ``AUTH_MODE=oauth`` deployment — on the reasonable-sounding grounds
    that a verify-only service has no use for it — therefore raises
    ``PartialKeyConfigurationError`` at construction instead of silently
    replacing the configured public key and 401-ing every genuinely minted
    token. Configure both halves, or neither.

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
        """Load the RSA key pair from configuration, or refuse.

        Each key can be provided via:
        1. A constructor argument
        2. A direct string in the environment (JWT_PRIVATE_KEY, JWT_PUBLIC_KEY)
        3. A file path (JWT_PRIVATE_KEY_PATH, JWT_PUBLIC_KEY_PATH)

        Exactly three outcomes, and the middle one is the point:

        * **Nothing requested** — no constructor argument, no string, no path,
          for either half. This selects development keys, deliberately, and is
          the genuine local path.
        * **Something requested but the pair is incomplete** — raise
          ``PartialKeyConfigurationError``. Never fabricate.
        * **Both halves resolved** — use them verbatim.

        "Requested" is measured on what the deployment *declared*, not on what
        resolved. A key file that is named but missing from disk is a configured
        key that failed, not an unconfigured one; collapsing those two is what
        let a typo in ``JWT_PUBLIC_KEY_PATH`` fall through to fabrication.

        This refusal exists because generating a development pair overwrites
        **both** halves. Before #853's follow-up, ``AUTH_MODE=oauth`` with a
        configured public key and a missing private key silently replaced the
        configured public key with a random one, and every token minted by the
        real signer then failed verification — a 401 storm whose only signal was
        one log line. ``_check_cloud`` in ``config.deployment_coherence`` already
        refuses this at boot, but only when ``DEPLOYMENT_MODE=cloud``; the check
        here holds for every mode and does not depend on that gate having run.
        """
        security = self._settings.security

        # Measured BEFORE loading, so a declared-but-unreadable source still
        # counts as requested.
        private_requested = bool(
            self._private_key
            or security.jwt_private_key
            or security.jwt_private_key_path
        )
        public_requested = bool(
            self._public_key or security.jwt_public_key or security.jwt_public_key_path
        )

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

        # Nothing was asked for: development keys are the deliberate selection.
        if not private_requested and not public_requested:
            self._generate_dev_keys()
            return

        missing = []
        if not self._private_key:
            missing.append("private (JWT_PRIVATE_KEY / JWT_PRIVATE_KEY_PATH)")
        if not self._public_key:
            missing.append("public (JWT_PUBLIC_KEY / JWT_PUBLIC_KEY_PATH)")

        if missing:
            raise PartialKeyConfigurationError(
                "JWT RSA key configuration is incomplete: no usable "
                f"{' and '.join(missing)} key. Refusing to generate development "
                "keys, because generation replaces BOTH halves — the configured "
                "half would be discarded and every genuinely minted token would "
                "then fail verification. Configure both halves of the pair, or "
                "neither (which selects development keys deliberately). A named "
                "key file that does not exist counts as configured-and-broken, "
                "not as unconfigured."
            )

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
    def signing_private_key(self) -> Optional[str]:
        """The RSA private key this deployment signs with, as resolved here.

        Public because key *resolution* has one authority and this is it:
        ``_load_keys`` is the only code that reads both the direct-value and
        the file-path spellings, refuses a half-configured pair, and selects
        development keys when nothing at all was declared. A second resolver
        elsewhere produces a second dev pair in the same process, and then
        tokens minted by one signer fail verification by the other — the 401
        storm ``_load_keys`` documents. The token generators are built FROM
        this pair rather than resolving their own (#959).

        Exposed as a read-only property, not by callers reaching for
        ``_private_key``: ending cross-service private-attribute reads is what
        this change is for.
        """
        return self._private_key

    @property
    def verification_public_key(self) -> Optional[str]:
        """The RSA public key matching :attr:`signing_private_key`."""
        return self._public_key

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
        """Longest lifetime any token this deployment mints could have.

        A per-user watermark that expired before the tokens it revokes would
        resurrect them, so this must bound EVERY token type, not just the
        obvious one.

        Expiry has a single source (``settings.auth``, #888) and every minting
        path — the HS256/local generator and the RS256/cloud generator, which
        since #853 are the only ones — takes its lifetimes from it. So "the
        watermark outlives
        every mintable token" is structural rather than a reconciliation across
        configuration: reading that one source covers every mint path, and the
        field bounds are the only mintable range there is.

        Access-token expiry is still folded in, because nothing ties
        ``JWT_ACCESS_TOKEN_EXPIRY_MINUTES`` to the refresh expiry: at its schema
        maximum (1 day) it exceeds the shortest permitted refresh lifetime and
        must be covered exactly like the refresh case.

        Attributes are read directly rather than via ``getattr`` defaults, and a
        non-positive result raises: a missing or mis-wired settings half must
        fail loudly here rather than silently under-cover.
        """
        refresh_days = self._settings.auth.jwt_refresh_token_expire_days
        access_minutes = self._settings.auth.jwt_access_token_expire_minutes
        seconds = max(int(refresh_days) * 86400, int(access_minutes) * 60)
        if seconds <= 0:
            # Unreachable from a real settings object: expiry has one
            # declaration and both its fields are bounded ``ge=1``. Getting here
            # therefore means this service was handed something that is not the
            # source the generators mint from — so any TTL derived here would
            # bound nothing, and a non-positive one is rejected by SETEX
            # outright. Defaulting would restore the exact under-coverage #769
            # fixed, so name the mis-wiring instead.
            raise RuntimeError(
                "Token expiry is mis-wired: settings.auth reports a non-positive "
                f"longest token lifetime (refresh_days={refresh_days!r}, "
                f"access_minutes={access_minutes!r}). The revocation watermark "
                "cannot be bounded, so no revocation may be recorded against it."
            )
        return seconds

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
        # revokes it and spring back to life. There is one expiry source
        # (`settings.auth`, the `JWT_*_EXPIRY_*` knobs) and every generator is
        # constructed from it, so covering that source covers every mint path.
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

    async def get_revocation_reason(self, claims: Dict[str, Any]) -> Optional[str]:
        """Why these claims are revoked, or None if they are not.

        The same rule as the request path — one rule governs every token type
        (``revocation_reason``) — but a store *error* propagates here instead of
        reading as "not revoked". For callers where proceeding on an unknown
        revocation state is worse than refusing, such as password reset, which
        is account-takeover-grade.

        A missing store raises for the same reason, and matches
        ``revoke_token``/``revoke_user_tokens``: without one, no answer about
        revocation is available at all. Returning "not revoked" there would be
        the fail-open this method exists to avoid.

        Args:
            claims: Verified token claims (``jti``, ``sub`` and ``iat`` are read)

        Returns:
            ``"token_revoked"``, ``"user_revoked"``, or None.

        Raises:
            ServiceError: No revocation store is configured.
            Exception: Store read failures propagate.
        """
        if self._revocation_store is None:
            raise ServiceError("Revocation state unavailable: no revocation store")
        return await revocation_reason(self._revocation_store, claims)

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
