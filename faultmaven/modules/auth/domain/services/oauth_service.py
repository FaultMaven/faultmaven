"""OAuth 2.0 + PKCE Service Implementation.

Implements the IOAuthService contract for Dashboard-centric authentication flow.
"""

import hashlib
import logging
import secrets
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from faultmaven.modules.auth.contracts import (
    IOAuthService,
    IOAuthCodeRepository,
    OAuthAuthorizationDTO,
    OAuthCodeDTO,
    OAuthTokenDTO,
)
from faultmaven.models.exceptions import (
    InvalidRequestError,
    InvalidGrantError,
)
from faultmaven.modules.auth.infrastructure.metrics import oauth_metrics

logger = logging.getLogger(__name__)


class OAuthServiceImpl(IOAuthService):
    """OAuth 2.0 + PKCE service implementation.

    Implements OAuth 2.0 Authorization Code Flow with PKCE for browser extensions.
    Dashboard acts as IdP, issues authorization codes, Extension exchanges for tokens.

    Design: Deployment-agnostic via dependency injection
    - code_repository: Redis, PostgreSQL, or in-memory
    - Configuration-driven behavior (settings injected)
    """

    def __init__(
        self,
        code_repository: IOAuthCodeRepository,
        user_repository,  # IUserRepository
        token_generator,  # IJWTTokenGenerator
        settings,  # AuthSettings from config
    ):
        """Initialize OAuth service with dependencies.

        Args:
            code_repository: Storage for authorization codes
            user_repository: User data access
            token_generator: JWT token generation
            settings: Authentication configuration
        """
        self.code_repository = code_repository
        self.user_repository = user_repository
        self.token_generator = token_generator
        self.settings = settings

    async def create_authorization_code(
        self,
        user_id: str,
        request: OAuthAuthorizationDTO,
    ) -> str:
        """Generate authorization code for OAuth flow.

        Args:
            user_id: Authenticated user's ID from Dashboard session
            request: OAuth authorization request (includes PKCE challenge)

        Returns:
            Authorization code (short-lived, single-use, 10 minutes)

        Raises:
            InvalidRequestError: If request parameters invalid
        """
        logger.info(
            "OAuth authorization code requested",
            extra={
                "user_id": user_id,
                "client_id": request.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": request.scope,
            },
        )

        # Validate client_id
        if request.client_id not in self.settings.oauth_allowed_clients:
            logger.warning(
                "OAuth authorization failed: invalid client",
                extra={
                    "user_id": user_id,
                    "client_id": request.client_id,
                    "error": "INVALID_CLIENT",
                },
            )
            # Record metrics
            oauth_metrics.record_authorization_request(
                client_id=request.client_id, success=False, error_code="INVALID_CLIENT"
            )
            oauth_metrics.record_invalid_client(request.client_id)
            raise InvalidRequestError(
                f"Unknown client_id: {request.client_id}",
                error_code="INVALID_CLIENT",
            )

        # Validate redirect_uri
        if not self._is_redirect_uri_allowed(request.redirect_uri):
            logger.warning(
                "OAuth authorization failed: invalid redirect URI",
                extra={
                    "user_id": user_id,
                    "client_id": request.client_id,
                    "redirect_uri": request.redirect_uri,
                    "error": "INVALID_REDIRECT_URI",
                },
            )
            # Record metrics
            oauth_metrics.record_authorization_request(
                client_id=request.client_id,
                success=False,
                error_code="INVALID_REDIRECT_URI",
            )
            oauth_metrics.record_redirect_uri_mismatch(request.client_id)
            raise InvalidRequestError(
                f"Invalid redirect_uri: {request.redirect_uri}",
                error_code="INVALID_REDIRECT_URI",
            )

        # Validate code_challenge_method
        if request.code_challenge_method != "S256":
            logger.warning(
                "OAuth authorization failed: unsupported challenge method",
                extra={
                    "user_id": user_id,
                    "client_id": request.client_id,
                    "challenge_method": request.code_challenge_method,
                    "error": "INVALID_CODE_CHALLENGE_METHOD",
                },
            )
            # Record metrics
            oauth_metrics.record_authorization_request(
                client_id=request.client_id,
                success=False,
                error_code="INVALID_CODE_CHALLENGE_METHOD",
            )
            raise InvalidRequestError(
                f"Unsupported code_challenge_method: {request.code_challenge_method}. "
                "Only S256 is supported.",
                error_code="INVALID_CODE_CHALLENGE_METHOD",
            )

        # Generate cryptographically secure authorization code
        code = self._generate_code()

        # Calculate expiry time
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.oauth_code_expiry_seconds
        )

        # Store authorization code with PKCE challenge
        code_data = OAuthCodeDTO(
            code=code,
            user_id=user_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            expires_at=expires_at,
            used=False,
        )

        await self.code_repository.save_code(code_data)

        logger.info(
            "OAuth authorization code generated",
            extra={
                "user_id": user_id,
                "client_id": request.client_id,
                "code_length": len(code),
                "expires_in_seconds": self.settings.oauth_code_expiry_seconds,
            },
        )

        # Record successful authorization metrics
        oauth_metrics.record_authorization_request(
            client_id=request.client_id, success=True
        )

        return code

    async def exchange_code_for_token(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenDTO:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from authorization endpoint
            code_verifier: PKCE code verifier (proves client owns code_challenge)
            redirect_uri: Must match original redirect_uri

        Returns:
            Access token and refresh token with user information

        Raises:
            InvalidGrantError: If code invalid, expired, or already used
            PKCEVerificationError: If code_verifier doesn't match code_challenge
        """
        start_time = time.time()

        logger.info(
            "OAuth token exchange requested",
            extra={
                "code_prefix": code[:8] if code else None,
                "redirect_uri": redirect_uri,
            },
        )

        # Retrieve authorization code data
        code_data = await self.code_repository.get_code(code)

        if not code_data:
            logger.warning(
                "OAuth token exchange failed: invalid or expired code",
                extra={
                    "code_prefix": code[:8] if code else None,
                    "error": "INVALID_AUTHORIZATION_CODE",
                },
            )
            # Record metric
            duration_seconds = time.time() - start_time
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=duration_seconds,
                success=False,
                error_code="INVALID_AUTHORIZATION_CODE",
            )
            raise InvalidGrantError(
                "Invalid or expired authorization code",
                error_code="INVALID_AUTHORIZATION_CODE",
            )

        # Check if code already used (replay attack prevention)
        if code_data.used:
            logger.warning(
                "OAuth token exchange failed: code replay attack detected",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "error": "CODE_ALREADY_USED",
                },
            )
            # Record metrics
            duration_seconds = time.time() - start_time
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=duration_seconds,
                success=False,
                error_code="CODE_ALREADY_USED",
            )
            oauth_metrics.record_code_replay_attempt("unknown")
            raise InvalidGrantError(
                "Authorization code has already been used",
                error_code="CODE_ALREADY_USED",
            )

        # Check if code expired
        if datetime.now(timezone.utc) > code_data.expires_at:
            logger.warning(
                "OAuth token exchange failed: code expired",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "expires_at": code_data.expires_at.isoformat(),
                    "error": "CODE_EXPIRED",
                },
            )
            # Record metrics
            duration_seconds = time.time() - start_time
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=duration_seconds,
                success=False,
                error_code="CODE_EXPIRED",
            )
            oauth_metrics.record_code_expired("unknown")
            raise InvalidGrantError(
                "Authorization code expired",
                error_code="CODE_EXPIRED",
            )

        # Validate redirect_uri matches
        if redirect_uri != code_data.redirect_uri:
            logger.warning(
                "OAuth token exchange failed: redirect URI mismatch",
                extra={
                    "user_id": code_data.user_id,
                    "expected_uri": code_data.redirect_uri,
                    "received_uri": redirect_uri,
                    "error": "REDIRECT_URI_MISMATCH",
                },
            )
            # Record metrics
            duration_seconds = time.time() - start_time
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=duration_seconds,
                success=False,
                error_code="REDIRECT_URI_MISMATCH",
            )
            raise InvalidGrantError(
                "Redirect URI mismatch",
                error_code="REDIRECT_URI_MISMATCH",
            )

        # Verify PKCE code_verifier
        if not self._verify_pkce(code_verifier, code_data.code_challenge):
            logger.warning(
                "OAuth token exchange failed: PKCE verification failed",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "error": "PKCE_VERIFICATION_FAILED",
                },
            )
            # Record metrics
            duration_seconds = time.time() - start_time
            oauth_metrics.record_token_exchange(
                grant_type="authorization_code",
                client_id="unknown",
                duration_seconds=duration_seconds,
                success=False,
                error_code="PKCE_VERIFICATION_FAILED",
            )
            oauth_metrics.record_pkce_failure("unknown")
            raise InvalidGrantError(
                "PKCE verification failed",
                error_code="PKCE_VERIFICATION_FAILED",
            )

        # Mark code as used (single-use)
        await self.code_repository.mark_code_used(code)

        # Get user data
        user = await self.user_repository.get(code_data.user_id)
        if not user:
            logger.error(
                "OAuth token exchange failed: user not found",
                extra={
                    "user_id": code_data.user_id,
                    "code_prefix": code[:8],
                    "error": "USER_NOT_FOUND",
                },
            )
            raise InvalidGrantError(
                "User not found",
                error_code="USER_NOT_FOUND",
            )

        # Generate access token and refresh token
        access_token = await self.token_generator.generate_access_token(user)
        refresh_token = await self.token_generator.generate_refresh_token(user)

        logger.info(
            "OAuth tokens issued",
            extra={
                "user_id": user.user_id,
                "username": user.username,
                "access_token_expires_in": self.settings.jwt_access_token_expire_minutes
                * 60,
                "refresh_token_expires_in": self.settings.jwt_refresh_token_expire_days
                * 86400,
            },
        )

        # Record success metrics
        duration_seconds = time.time() - start_time
        oauth_metrics.record_token_exchange(
            grant_type="authorization_code",
            client_id="unknown",  # Could extract from settings if needed
            duration_seconds=duration_seconds,
            success=True,
        )

        # Return tokens with user info
        return OAuthTokenDTO(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=self.settings.jwt_access_token_expire_minutes * 60,
            refresh_expires_in=self.settings.jwt_refresh_token_expire_days * 86400,
            user_id=user.user_id,
            username=user.username,
        )

    async def refresh_access_token(
        self,
        refresh_token: str,
        client_id: str,
    ) -> OAuthTokenDTO:
        """Refresh access token using refresh token.

        Args:
            refresh_token: Valid refresh token
            client_id: OAuth client ID

        Returns:
            New access token and rotated refresh token

        Raises:
            InvalidGrantError: If refresh token invalid, expired, or revoked
        """
        logger.info(
            "OAuth token refresh requested",
            extra={
                "client_id": client_id,
                "token_rotation": self.settings.jwt_rotate_refresh_tokens,
            },
        )

        # Validate refresh token
        payload = await self.token_generator.validate_refresh_token(refresh_token)

        if not payload:
            logger.warning(
                "OAuth token refresh failed: invalid refresh token",
                extra={"client_id": client_id, "error": "INVALID_REFRESH_TOKEN"},
            )
            # Record metrics
            oauth_metrics.record_token_refresh(
                client_id=client_id, success=False, error_code="INVALID_REFRESH_TOKEN"
            )
            raise InvalidGrantError(
                "Invalid or expired refresh token",
                error_code="INVALID_REFRESH_TOKEN",
            )

        user_id = payload.get("sub")
        if not user_id:
            logger.warning(
                "OAuth token refresh failed: invalid token payload",
                extra={"client_id": client_id, "error": "INVALID_TOKEN_PAYLOAD"},
            )
            raise InvalidGrantError(
                "Invalid refresh token payload",
                error_code="INVALID_TOKEN_PAYLOAD",
            )

        # Get user data
        user = await self.user_repository.get(user_id)
        if not user:
            logger.error(
                "OAuth token refresh failed: user not found",
                extra={
                    "user_id": user_id,
                    "client_id": client_id,
                    "error": "USER_NOT_FOUND",
                },
            )
            raise InvalidGrantError(
                "User not found",
                error_code="USER_NOT_FOUND",
            )

        # Generate new access token
        new_access_token = await self.token_generator.generate_access_token(user)

        # Rotate refresh token (security best practice)
        new_refresh_token = refresh_token
        if self.settings.jwt_rotate_refresh_tokens:
            # Revoke old refresh token
            await self.token_generator.revoke_refresh_token(refresh_token)
            # Generate new refresh token
            new_refresh_token = await self.token_generator.generate_refresh_token(user)
            logger.debug(
                "OAuth refresh token rotated",
                extra={
                    "user_id": user.user_id,
                    "client_id": client_id,
                },
            )

        logger.info(
            "OAuth tokens refreshed",
            extra={
                "user_id": user.user_id,
                "username": user.username,
                "client_id": client_id,
                "token_rotated": self.settings.jwt_rotate_refresh_tokens,
            },
        )

        # Record success metrics
        oauth_metrics.record_token_refresh(client_id=client_id, success=True)

        return OAuthTokenDTO(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            token_type="Bearer",
            expires_in=self.settings.jwt_access_token_expire_minutes * 60,
            refresh_expires_in=self.settings.jwt_refresh_token_expire_days * 86400,
            user_id=user.user_id,
            username=user.username,
        )

    async def validate_token(self, token: str) -> Optional[str]:
        """Validate access token and return user_id.

        Args:
            token: Access token from Authorization header

        Returns:
            user_id if token valid, None otherwise
        """
        payload = await self.token_generator.validate_access_token(token)

        if not payload:
            return None

        return payload.get("sub")  # 'sub' claim contains user_id

    async def revoke_token(self, token: str) -> None:
        """Revoke access token (logout).

        Args:
            token: Access token to revoke
        """
        logger.info(
            "OAuth access token revocation requested",
            extra={"token_prefix": token[:16] if token else None},
        )
        await self.token_generator.revoke_access_token(token)
        logger.info(
            "OAuth access token revoked",
            extra={"token_prefix": token[:16] if token else None},
        )
        # Record metrics
        oauth_metrics.record_token_revocation("access_token")

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        """Revoke refresh token (prevents future token refresh).

        Args:
            refresh_token: Refresh token to revoke
        """
        logger.info(
            "OAuth refresh token revocation requested",
            extra={"token_prefix": refresh_token[:16] if refresh_token else None},
        )
        await self.token_generator.revoke_refresh_token(refresh_token)
        logger.info(
            "OAuth refresh token revoked",
            extra={"token_prefix": refresh_token[:16] if refresh_token else None},
        )
        # Record metrics
        oauth_metrics.record_token_revocation("refresh_token")

    # ============================================================
    # Private Helper Methods
    # ============================================================

    def _generate_code(self) -> str:
        """Generate cryptographically secure authorization code.

        Returns:
            Base64url-encoded random string (43 characters)
        """
        random_bytes = secrets.token_bytes(32)
        code = base64.urlsafe_b64encode(random_bytes).decode("utf-8").rstrip("=")
        return code

    def _verify_pkce(self, code_verifier: str, code_challenge: str) -> bool:
        """Verify PKCE code_verifier matches code_challenge.

        Args:
            code_verifier: Code verifier from client
            code_challenge: Stored code challenge (SHA256 of verifier)

        Returns:
            True if verification succeeds, False otherwise
        """
        # Compute SHA256 hash of code_verifier
        verifier_bytes = code_verifier.encode("utf-8")
        computed_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest())
            .decode("utf-8")
            .rstrip("=")
        )

        # Compare with stored code_challenge (constant-time comparison)
        return secrets.compare_digest(computed_challenge, code_challenge)

    def _is_redirect_uri_allowed(self, redirect_uri: str) -> bool:
        """Check if redirect_uri matches allowed patterns and HTTPS requirement.

        Args:
            redirect_uri: Redirect URI from request

        Returns:
            True if allowed, False otherwise
        """
        import re
        from urllib.parse import urlparse

        # HTTPS enforcement (production security)
        if self.settings.oauth_require_https_redirect:
            parsed = urlparse(redirect_uri)

            # Allow chrome-extension:// and moz-extension:// (browser extensions)
            # Require https:// for web redirects
            if parsed.scheme not in ["chrome-extension", "moz-extension", "https"]:
                logger.warning(
                    f"Redirect URI rejected (HTTPS required): {redirect_uri}",
                    extra={
                        "security_event": "oauth_insecure_redirect",
                        "redirect_uri": redirect_uri,
                        "scheme": parsed.scheme,
                    },
                )
                return False

        # Pattern matching (client ID validation)
        for pattern in self.settings.oauth_redirect_uri_patterns:
            if re.match(pattern, redirect_uri):
                return True

        return False
